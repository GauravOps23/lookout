import time

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.resources.connectors.base import Handle
from app.views.base import RunContext, Variable, result_set
from app.views.ocp_pods import parse

COLUMNS = [{"name": n, "type": t} for n, t in
           (("namespace", "string"), ("pod", "string"), ("state", "string"), ("ready", "string"),
            ("restarts", "number"), ("age", "string"), ("node", "string"), ("status", "string"))]


class OcpPodsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    namespaces: list[str] = Field([], max_length=50)  # empty: the cluster's own namespaces setting, else all
    only_problems: bool = False

    @field_validator("namespaces")
    @classmethod
    def _names(cls, v):
        for n in v:
            if len(n) > 63 or not parse.NAMESPACE.match(n):
                raise ValueError(f"invalid namespace: {n}")
        return v


class OcpPodsView:
    key = "ocp_pods"
    permission = "view.ocp_pods.create"
    source_kind = "ocp"
    config_schema = OcpPodsConfig
    default_renderer = "table"

    def variables(self, cfg) -> list[Variable]:
        return []

    def run(self, ctx: RunContext, cfg: OcpPodsConfig, h: Handle) -> dict:
        t0 = time.monotonic()
        namespaces = cfg.namespaces or h.config.get("namespaces", [])
        pods, truncated = parse.fetch_pods(ctx.meta["connectors"]["ocp"], h, namespaces)
        if cfg.only_problems:
            pods = [p for p in pods if p["status"] != parse.OK]
        rows = [[p["namespace"], p["name"], p["state"], p["ready"], p["restarts"], p["age"], p["node"], p["status"]]
                for p in pods]
        return result_set(COLUMNS, rows, source=h.id, duration_ms=int((time.monotonic() - t0) * 1000),
                          truncated=truncated)

    def llm_tool_schema(self, cfg) -> dict:
        return {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
