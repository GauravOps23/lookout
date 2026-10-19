"""Sensei tools (spec 9.3-9.4). Read-only; every call runs as the requesting user through the view engine, so
authorization, caching and per-source failure reporting are exactly those of the UI."""
import json
import re
import uuid
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz.access import levels_for
from app.authz.acl import Level
from app.errors import AppError
from app.models import Resource, ResourceAcl, User, View, ViewAcl
from app.views import engine
from app.views.base import variables_json_schema

SECRET_COL = re.compile(r"pass(word|wd)?|secret|token|api_?key|authorization|credential|private", re.I)
MAX_CELL = 200
OPS = ("=", "!=", "<", ">", "<=", ">=", "contains")


class ToolError(Exception):
    """Returned to the model as the tool result; never aborts the turn."""


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


_ID = {"type": "string", "description": "View id from list_views"}
TOOLS = [
    {"name": "list_resources", "description": "List servers, databases and APIs you can see (no secrets).",
     "parameters": _obj({"tag": {"type": "string"}, "type": {"type": "string", "enum": ["ssh", "oracle", "api"]}})},
    {"name": "list_views", "description": "List saved views with the parameters each one accepts.",
     "parameters": _obj({"type": {"type": "string"}})},
    {"name": "run_view", "description": "Run a saved view (cached result if fresh). Ask the user for any missing "
     "required parameter instead of guessing.",
     "parameters": _obj({"view_id": _ID, "params": {"type": "object"}}, ["view_id"])},
    {"name": "get_view_summary", "description": "Row count, column statistics and top rows of a view's latest result.",
     "parameters": _obj({"view_id": _ID}, ["view_id"])},
    {"name": "query_view_table", "description": "Filter and sort a view's result in memory (never SQL).",
     "parameters": _obj({"view_id": _ID, "params": {"type": "object"},
                         "filters": {"type": "array", "items": _obj({"column": {"type": "string"},
                                     "op": {"type": "string", "enum": list(OPS)}, "value": {}}, ["column", "op", "value"])},
                         "order_by": {"type": "string", "description": "column name, prefix with - for descending"},
                         "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, ["view_id"])},
]


def wrap(obj) -> str:
    """Tool output is data, never instructions (spec 9.4)."""
    body = json.dumps(obj, default=str, ensure_ascii=False).replace("</tool_result", "<\\/tool_result")
    return f'<tool_result untrusted="true">{body}</tool_result>'


# ---- reduce ----------------------------------------------------------------------------------
def _cell(v):
    return v[:MAX_CELL] + "…" if isinstance(v, str) and len(v) > MAX_CELL else v


def _stats(col: dict, values: list) -> dict | None:
    vals = [v for v in values if v is not None]
    if col["type"] == "number":
        nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        return {"min": min(nums), "max": max(nums), "sum": sum(nums)} if nums else None
    if col["type"] == "string":
        top = Counter(_cell(str(v)) for v in vals).most_common(3)
        return {"distinct": len(set(vals)), "top": [{"value": k, "count": n} for k, n in top]}
    return None


def reduce(res: dict, max_rows: int, max_chars: int) -> dict:
    """Top rows, column stats, truncation note and a character cap; secret-looking columns are dropped."""
    keep = [i for i, c in enumerate(res["columns"]) if not SECRET_COL.search(c["name"])]
    cols = [res["columns"][i] for i in keep]
    rows = [[r[i] for i in keep] for r in res["rows"]]
    total = len(rows)
    stats = {}
    for j, c in enumerate(cols[:20]):
        s = _stats(c, [r[j] for r in rows])
        if s:
            stats[c["name"]] = s
    meta = {k: res["meta"][k] for k in ("cached", "stale", "generated_at", "truncated") if k in res["meta"]}
    if res["meta"].get("sources"):
        meta["source_errors"] = [{"name": s.get("name"), "error": s["error"]} for s in res["meta"]["sources"]
                                 if s.get("error")]
    n = min(max_rows, total)
    while True:
        out = {"columns": [c["name"] for c in cols], "row_count": total,
               "rows": [[_cell(v) for v in r] for r in rows[:n]], "stats": stats, "meta": meta}
        if n < total:
            out["note"] = f"Showing {n} of {total} rows."
        if len(json.dumps(out, default=str)) <= max_chars or (n == 0 and not stats):
            return out
        if n == 0:
            stats = {}
        else:
            n = n * 3 // 4


# ---- access ----------------------------------------------------------------------------------
def _resources_ok(db: Session, view: View) -> bool:
    ids = [uuid.UUID(str(i)) for i in view.source_resource_ids]
    rows = list(db.scalars(select(Resource).where(Resource.id.in_(ids)))) if ids else []
    return len(rows) == len(ids) and all(r.llm_allowed for r in rows)


def _view(db: Session, user: User, raw) -> View:
    try:
        v = db.get(View, uuid.UUID(str(raw)))
    except ValueError:
        v = None
    # Same answer for "does not exist", "not allowed for the assistant" and "no access": nothing is revealed.
    if v is None or not v.llm_allowed or not _resources_ok(db, v) or v.id not in levels_for(db, user, [v], ViewAcl):
        raise ToolError("NOT_FOUND: unknown view_id; call list_views")
    return v


def _app_error(e: AppError) -> ToolError:
    d = e.details or {}
    if e.code == "VALIDATION" and d.get("missing") and not d.get("invalid"):
        return ToolError(f"MISSING_PARAMS: {d['missing']}")
    return ToolError(f"{e.code}: {e.message}" + (f" {json.dumps(d)}" if d else ""))


def _params(view: View, raw) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ToolError("VALIDATION: params must be an object")
    plugin, cfg = engine.load_plugin(view)
    blocked = [v.name for v in plugin.variables(cfg) if (v.sensitive or not v.llm_fillable) and v.name in raw]
    if blocked:
        raise ToolError(f"VALIDATION: {blocked} must be entered by the user, not the assistant")
    return raw


def _run(db: Session, user: User, view: View, params) -> dict:
    try:
        return engine.run_view(db, user, view, _params(view, params), triggered_by="llm")
    except AppError as e:
        raise _app_error(e) from None


def _source(view: View) -> dict:
    return {"type": "view", "id": str(view.id), "name": view.name}


# ---- tools -----------------------------------------------------------------------------------
def _check(args: dict, allowed: set[str]) -> None:
    extra = sorted(set(args) - allowed)
    if extra:
        raise ToolError(f"VALIDATION: unknown arguments {extra}")


def list_resources(db, user, cfg, args):
    _check(args, {"tag", "type"})
    rows = [r for r in db.scalars(select(Resource).order_by(Resource.name)) if r.llm_allowed
            and (not args.get("type") or r.type == args["type"])
            and (not args.get("tag") or args["tag"] in (r.tags or []))]
    lv = levels_for(db, user, rows, ResourceAcl)
    seen = [r for r in rows if r.id in lv]
    return ([{"id": str(r.id), "name": r.name, "type": r.type, "status": r.status} for r in seen],
            [{"type": "resource", "id": str(r.id), "name": r.name} for r in seen])


def list_views(db, user, cfg, args):
    _check(args, {"type"})
    known = sorted({v.type for v in db.scalars(select(View))})
    if args.get("type") and args["type"] not in known:
        raise ToolError(f"VALIDATION: type must be one of {known}, or omit it to list all views")
    stmt = select(View).order_by(View.name)
    if args.get("type"):
        stmt = stmt.where(View.type == args["type"])
    rows = [v for v in db.scalars(stmt) if v.llm_allowed]
    lv = levels_for(db, user, rows, ViewAcl)
    out = []
    for v in rows:
        if v.id not in lv or lv[v.id] < Level.use or not _resources_ok(db, v):
            continue
        plugin, c = engine.load_plugin(v)
        out.append({"id": str(v.id), "name": v.name, "type": v.type, "description": v.description,
                    "params": variables_json_schema(plugin.variables(c))})
    return out, [{"type": "view", "id": o["id"], "name": o["name"]} for o in out]


def run_view(db, user, cfg, args):
    _check(args, {"view_id", "params"})
    v = _view(db, user, args.get("view_id"))
    return reduce(_run(db, user, v, args.get("params")), cfg["max_rows"], cfg["max_chars"]), [_source(v)]


def get_view_summary(db, user, cfg, args):
    _check(args, {"view_id"})
    v = _view(db, user, args.get("view_id"))
    try:
        res = engine.latest_result(db, user, v, {})
    except AppError as e:
        raise _app_error(e) from None
    if res is None:
        raise ToolError("NOT_FOUND: no result yet; call run_view")
    return reduce(res, min(10, cfg["max_rows"]), cfg["max_chars"]), [_source(v)]


def _match(cell, op: str, want) -> bool:
    if cell is None:
        return False
    if op == "contains":
        return str(want).lower() in str(cell).lower()
    try:
        a, b = (float(cell), float(want)) if isinstance(cell, (int, float)) else (str(cell), str(want))
    except (TypeError, ValueError):
        return False
    return {"=": a == b, "!=": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]


def query_view_table(db, user, cfg, args):
    _check(args, {"view_id", "params", "filters", "order_by", "limit"})
    v = _view(db, user, args.get("view_id"))
    res = _run(db, user, v, args.get("params"))
    names = [c["name"] for c in res["columns"]]
    filters = args.get("filters") or []
    if not isinstance(filters, list):
        raise ToolError("VALIDATION: filters must be a list")
    rows = res["rows"]
    for f in filters:
        if not isinstance(f, dict) or f.get("column") not in names or f.get("op") not in OPS or "value" not in f:
            raise ToolError(f"VALIDATION: each filter needs column (one of {names}), op (one of {list(OPS)}), value")
        i = names.index(f["column"])
        rows = [r for r in rows if _match(r[i], f["op"], f["value"])]
    if (ob := args.get("order_by")):
        col = str(ob).lstrip("-")
        if col not in names:
            raise ToolError(f"VALIDATION: order_by must be one of {names}")
        i = names.index(col)
        rows = sorted(rows, key=lambda r: (r[i] is None, r[i] if isinstance(r[i], (int, float)) else str(r[i])),
                      reverse=str(ob).startswith("-"))
    limit = args.get("limit", 50)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        raise ToolError("VALIDATION: limit must be an integer 1-50")
    sub = {"columns": res["columns"], "rows": rows[:limit], "meta": {**res["meta"], "truncated": len(rows) > limit}}
    out = reduce(sub, min(limit, cfg["max_rows"]), cfg["max_chars"])
    out["matched_rows"] = len(rows)
    return out, [_source(v)]


HANDLERS = {"list_resources": list_resources, "list_views": list_views, "run_view": run_view,
            "get_view_summary": get_view_summary, "query_view_table": query_view_table}


def execute(db: Session, user: User, cfg: dict, name: str, args) -> tuple[object, list[dict]]:
    """Returns (result, sources). Raises ToolError for anything the model should see and correct."""
    fn = HANDLERS.get(name)
    if fn is None:
        raise ToolError(f"VALIDATION: unknown tool {name!r}; available: {sorted(HANDLERS)}")
    if not isinstance(args, dict):
        raise ToolError("VALIDATION: arguments must be a JSON object")
    return fn(db, user, cfg, args)
