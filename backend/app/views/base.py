"""View plugin contract, ResultSet and variables (spec 7.1-7.3)."""
import datetime
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.errors import AppError
from app.resources.connectors.base import Handle

MAX_ROWS = 10000
COLUMN_TYPES = ("string", "number", "boolean", "datetime", "json")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- variables -----------------------------------------------------------------------------
class Variable(Strict):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    label: str = ""
    type: Literal["string", "number", "boolean", "date", "enum"] = "string"
    required: bool = True
    default: Any = None
    enum: list[str] | None = None
    pattern: str | None = None
    min: float | None = None
    max: float | None = None
    max_length: int = Field(256, ge=1, le=4096)
    description: str = ""
    sensitive: bool = False
    llm_fillable: bool = True


def _coerce(v: Variable, raw: Any) -> Any:
    t = v.type
    if t == "number":
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ValueError("must be a number")
        try:
            n = float(raw)
        except ValueError:
            raise ValueError("must be a number") from None
        if n != n or n in (float("inf"), float("-inf")):
            raise ValueError("must be a finite number")
        if v.min is not None and n < v.min or v.max is not None and n > v.max:
            raise ValueError("out of range")
        return int(n) if n == int(n) else n
    if t == "boolean":
        if isinstance(raw, bool):
            return raw
        if str(raw).lower() in ("true", "false"):
            return str(raw).lower() == "true"
        raise ValueError("must be true or false")
    if not isinstance(raw, str) or isinstance(raw, bool):
        raw = str(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else raw
    if not isinstance(raw, str):
        raise ValueError("must be text")
    if len(raw) > v.max_length:
        raise ValueError("too long")
    if "\x00" in raw:
        raise ValueError("invalid characters")
    if t == "date":
        try:
            datetime.date.fromisoformat(raw)
        except ValueError:
            raise ValueError("must be a date (YYYY-MM-DD)") from None
    if t == "enum" and raw not in (v.enum or []):
        raise ValueError("not an allowed value")
    if v.pattern and not re.fullmatch(v.pattern, raw):
        raise ValueError("does not match the required format")
    return raw


def bind_params(variables: list[Variable], supplied: dict | None) -> dict:
    """Validate caller values against the declared variables. Unknown names are rejected,
    missing required ones raise VALIDATION listing them. Returns typed values."""
    supplied = dict(supplied or {})
    declared = {v.name: v for v in variables}
    unknown = sorted(set(supplied) - set(declared))
    if unknown:
        raise AppError("VALIDATION", "Unknown variables", {"unknown": unknown})
    out, missing, bad = {}, [], {}
    for v in variables:
        raw = supplied.get(v.name, v.default)
        if raw is None:
            if v.required:
                missing.append(v.name)
            continue
        try:
            out[v.name] = _coerce(v, raw)
        except ValueError as e:
            bad[v.name] = str(e)
    if missing or bad:
        raise AppError("VALIDATION", "Invalid or missing variables", {"missing": missing, "invalid": bad})
    return out


def has_unsatisfied_required(variables: list[Variable]) -> list[str]:
    """Required variables with no default: a scheduled run could not supply them."""
    return [v.name for v in variables if v.required and v.default is None]


# ---- ResultSet -----------------------------------------------------------------------------
def result_set(columns: list[dict], rows: list[list], *, row_limit: int = MAX_ROWS, source: str = "",
               duration_ms: int = 0, truncated: bool = False) -> dict:
    limit = min(row_limit, MAX_ROWS)
    if len(rows) > limit:
        rows, truncated = rows[:limit], True
    return {"columns": columns, "rows": rows,
            "meta": {"row_count": len(rows), "truncated": truncated, "duration_ms": duration_ms,
                     "source": source}}


def merge_results(parts: list[tuple[str, dict]], row_limit: int = MAX_ROWS) -> dict:
    """Fan-out merge: prepend a SOURCE column (resource name). Columns come from the first non-empty part."""
    cols = next((p["columns"] for _, p in parts if p["columns"]), [])
    rows, truncated = [], False
    for name, p in parts:
        truncated |= p["meta"]["truncated"]
        rows.extend([name, *r] for r in p["rows"])
    return result_set([{"name": "SOURCE", "type": "string"}, *cols], rows, row_limit=row_limit,
                      truncated=truncated)


@dataclass
class RunContext:
    params: dict
    row_limit: int = MAX_ROWS
    meta: dict = field(default_factory=dict)


class ViewType(Protocol):
    key: str
    permission: str
    source_kind: str  # resource type this view reads
    config_schema: type[BaseModel]
    default_renderer: str

    def variables(self, cfg: BaseModel) -> list[Variable]: ...

    def run(self, ctx: RunContext, cfg: BaseModel, h: Handle) -> dict:
        """Run against ONE resource and return a ResultSet; the engine handles fan-out."""
        ...

    def llm_tool_schema(self, cfg: BaseModel) -> dict: ...


def variables_json_schema(variables: list[Variable]) -> dict:
    """JSON Schema of a view's run arguments; used for the UI form and later the LLM tool (spec 9.3)."""
    props, required = {}, []
    for v in variables:
        if v.sensitive or not v.llm_fillable:
            continue
        p: dict = {"description": v.description or v.label}
        p["type"] = {"number": "number", "boolean": "boolean"}.get(v.type, "string")
        if v.type == "enum":
            p["enum"] = v.enum
        if v.pattern:
            p["pattern"] = v.pattern
        if v.type == "string":
            p["maxLength"] = v.max_length
        props[v.name] = p
        if v.required and v.default is None:
            required.append(v.name)
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}
