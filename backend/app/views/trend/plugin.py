"""V5 trend (spec 7.3): aggregate the stored run history of a scheduled view into (timestamp, series, value)."""
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.errors import AppError
from app.views.base import Variable, result_set


class TrendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_view_id: uuid.UUID
    value_column: str | None = None  # not needed for "count"
    group_by: str | None = None
    aggregation: Literal["sum", "avg", "max", "count"] = "sum"
    window_days: int = Field(7, ge=1, le=365)


def _agg(kind: str, vals: list[float], n: int) -> float:
    if kind == "count":
        return n
    if not vals:
        return 0
    return {"sum": sum(vals), "avg": sum(vals) / len(vals), "max": max(vals)}[kind]


def aggregate(cfg: TrendConfig, history: list[tuple[str, dict]]) -> dict:
    """history: [(iso timestamp, ResultSet)] oldest first."""
    if cfg.aggregation != "count" and not cfg.value_column:
        raise AppError("VALIDATION", "value_column is required unless aggregation is count")
    out = []
    for ts, res in history:
        names = [c["name"] for c in res["columns"]]
        for want in (cfg.value_column, cfg.group_by):
            if want and want not in names:
                raise AppError("VALIDATION", f"Column {want!r} not found in the source result")
        vi = names.index(cfg.value_column) if cfg.value_column else None
        gi = names.index(cfg.group_by) if cfg.group_by else None
        groups: dict[str, list[list]] = {}
        for r in res["rows"]:
            groups.setdefault("all" if gi is None else str(r[gi]), []).append(r)
        for series, rows in sorted(groups.items()):
            vals = [float(r[vi]) for r in rows if vi is not None and isinstance(r[vi], (int, float))
                    and not isinstance(r[vi], bool)]
            v = _agg(cfg.aggregation, vals, len(rows))
            out.append([ts, series, int(v) if v == int(v) else round(v, 4)])
    cols = [{"name": "timestamp", "type": "datetime"}, {"name": "series", "type": "string"},
            {"name": "value", "type": "number"}]
    return result_set(cols, out)


class TrendView:
    key = "trend"
    permission = "view.trend.create"
    source_kind = "view"
    derived = True
    config_schema = TrendConfig
    default_renderer = "chart_line"
    allowed_source_types = ("db_table", "api_response", "ssh_health")
    needs_scheduled_source = True

    def source_view_ids(self, cfg: TrendConfig) -> list[uuid.UUID]:
        return [cfg.source_view_id]

    def variables(self, cfg: TrendConfig) -> list[Variable]:
        return []

    def derive(self, cfg: TrendConfig, loader) -> dict:
        return aggregate(cfg, loader.history(cfg.source_view_id, cfg.window_days))

    def llm_tool_schema(self, cfg: TrendConfig) -> dict:
        return {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
