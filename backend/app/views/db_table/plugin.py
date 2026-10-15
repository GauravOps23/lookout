import re
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.errors import AppError
from app.resources import service
from app.resources.connectors.base import Handle
from app.views import sqlguard
from app.views.base import RunContext, Variable, result_set, variables_json_schema

_IDENT = r"^[A-Za-z][A-Za-z0-9_$#]{0,29}(\.[A-Za-z][A-Za-z0-9_$#]{0,29})?$"
_OPS = {"=": "=", "!=": "<>", "<": "<", ">": ">", "<=": "<=", ">=": ">=", "like": "LIKE"}

# Spec 7.3: the get_error.py query with an index-friendly range predicate instead of to_char(...) = to_char(...).
# NOTE: `system_name_tmp b` has no join condition (a cross join), exactly as in the legacy query, so that
# results stay comparable with the old CSVs. Add a join predicate once the intended relation is confirmed.
DAILY_ERRORS_SQL = """
SELECT b.system AS "SYSTEM", a.req_user AS "REQ_USER",
       REPLACE(TRANSLATE(a.smessage, '0123456789,', '$'), '$', '') AS "ERROR",
       COUNT(*) AS "COUNT"
FROM sas_re_Request_message a, system_name_tmp b
WHERE a.req_status IN ('9', '8')
  AND a.req_start_time >= TRUNC(SYSDATE) AND a.req_start_time < TRUNC(SYSDATE) + 1
GROUP BY a.req_user, b.system, REPLACE(TRANSLATE(a.smessage, '0123456789,', '$'), '$', '')
ORDER BY COUNT(*) DESC
""".strip()


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    column: str = Field(pattern=_IDENT)
    op: Literal["=", "!=", "<", ">", "<=", ">=", "like"]
    value: str | int | float


class DbTableConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["sql", "table"] = "sql"
    sql: str | None = None
    table: str | None = Field(None, pattern=_IDENT)
    columns: list[str] = []
    filters: list[Filter] = []
    params: list[Variable] = []
    row_limit: int = Field(10000, ge=1, le=10000)
    timeout_s: int = Field(30, ge=1, le=600)

    @model_validator(mode="after")
    def _check(self):
        if any(not re.match(_IDENT, c) for c in self.columns):
            raise ValueError("invalid column name")
        if self.mode == "sql":
            if not self.sql:
                raise ValueError("sql is required")
            sqlguard.validate(sqlguard.expand_macros(self.sql, {p.name for p in self.params}))
        elif not self.table:
            raise ValueError("table is required")
        return self


def build(cfg: DbTableConfig) -> tuple[str, dict]:
    """Return (validated SQL with named binds, extra binds). Identifiers are regex-checked; values are binds."""
    if cfg.mode == "sql":
        return sqlguard.expand_macros(cfg.sql, {p.name for p in cfg.params}), {}
    cols = ", ".join(cfg.columns) or "*"
    binds, where = {}, []
    for i, f in enumerate(cfg.filters):
        binds[f"f{i}"] = f.value
        where.append(f"{f.column} {_OPS[f.op]} :f{i}")
    sql = f"SELECT {cols} FROM {cfg.table}" + (f" WHERE {' AND '.join(where)}" if where else "")
    sqlguard.validate(sql)
    return sql, binds


class DbTableView:
    key = "db_table"
    permission = "view.db_table.create"
    source_kind = "oracle"
    config_schema = DbTableConfig
    default_renderer = "table"

    def variables(self, cfg: DbTableConfig) -> list[Variable]:
        return cfg.params

    def run(self, ctx: RunContext, cfg: DbTableConfig, h: Handle) -> dict:
        sql, extra = build(cfg)
        used = sqlguard.validate(sql)
        binds = {k: v for k, v in {**ctx.params, **extra}.items() if k in used}  # oracledb rejects unused binds
        limit = min(ctx.row_limit, cfg.row_limit)
        t0 = time.monotonic()
        cols, rows = service.CONNECTORS["oracle"].query(h, sqlguard.wrap(sql), {**binds, "max_rows": limit + 1}, limit + 1)
        return result_set(cols, rows, row_limit=limit, source=h.id, duration_ms=int((time.monotonic() - t0) * 1000))

    def llm_tool_schema(self, cfg: DbTableConfig) -> dict:
        return variables_json_schema(cfg.params)
