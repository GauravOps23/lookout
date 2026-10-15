import time

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.resources import service
from app.resources.connectors.base import Handle
from app.views.base import RunContext, Variable, result_set
from app.views.ssh_health import parsers

COLUMNS = [{"name": n, "type": t} for n, t in
           (("check", "string"), ("metric", "string"), ("value", "number"), ("unit", "string"),
            ("status", "string"))]


class CheckCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    args: dict = {}
    warn: float | None = None
    crit: float | None = None

    @model_validator(mode="after")
    def _ok(self):
        if self.id not in parsers.CHECKS:
            raise ValueError(f"unknown check: {self.id}")
        parsers.command(self.id, self.args)  # validates args now, not at run time
        return self


class CustomCmd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    command: str = Field(min_length=1, max_length=1024)


class SshHealthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checks: list[CheckCfg] = []
    custom: list[CustomCmd] = []  # needs view.ssh.custom, enforced by the router
    timeout_s: int = Field(30, ge=1, le=120)


class SshHealthView:
    key = "ssh_health"
    permission = "view.ssh_health.create"
    source_kind = "ssh"
    config_schema = SshHealthConfig
    default_renderer = "table"

    def variables(self, cfg) -> list[Variable]:
        return []

    def run(self, ctx: RunContext, cfg: SshHealthConfig, h: Handle) -> dict:
        t0, rows = time.monotonic(), []
        ssh = service.CONNECTORS["ssh"]
        for c in cfg.checks:
            try:
                out = ssh.exec(h, parsers.command(c.id, c.args), cfg.timeout_s)
                rows += parsers.parse(c.id, c.args, out, c.warn, c.crit)
            except (ValueError, StopIteration, IndexError, KeyError) as e:
                if isinstance(e, parsers.ArgError):
                    raise
                rows.append((c.id, "parse_error", None, "", "crit"))  # visible, not silently dropped
        for cmd in cfg.custom:
            out = ssh.exec(h, cmd.command, cfg.timeout_s)  # already capped to 64 KB by the connector
            rows.append(("custom", cmd.name, None, out.strip()[:500], "ok"))
        return result_set(COLUMNS, [list(r) for r in rows], source=h.id,
                          duration_ms=int((time.monotonic() - t0) * 1000))

    def llm_tool_schema(self, cfg) -> dict:
        return {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
