import json
import time
import urllib.parse
from typing import Any, Literal

import defusedxml.ElementTree as DET
from jsonpath_ng.ext import parse as jp_parse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.errors import AppError
from app.resources.connectors.base import ConnectorError, Handle
from app.views.api_response import render
from app.views.base import RunContext, Variable, result_set, variables_json_schema


class FieldMap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    path: str
    type: Literal["string", "number", "boolean", "datetime", "json"] = "string"


class Mapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: str
    fields: list[FieldMap] = Field(min_length=1)


class SoapCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str = Field(pattern=r"^[A-Za-z0-9_:./#-]{1,200}$")  # sent as SOAPAction
    envelope_template: str


class ApiResponseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["GET", "POST"] = "GET"
    path: str = ""
    query_template: str = ""
    headers: dict[str, str] = {}
    body_format: Literal["json", "form"] = "json"
    body_template: Any = None
    soap: SoapCfg | None = None
    variables: list[Variable] = []
    mapping: Mapping | None = None

    @model_validator(mode="after")
    def _check(self):
        declared = {v.name: v for v in self.variables}
        if self.path and not self.path.startswith("/"):
            raise ValueError("path must start with '/'")
        used: set[str] = render.placeholders(self.path) | render.placeholders(self.query_template)
        if self.body_template is not None:
            used |= render.placeholders(json.dumps(self.body_template))
        if self.soap:
            used |= render.placeholders(self.soap.envelope_template)
        for k, tmpl in self.headers.items():
            hv = render.placeholders(tmpl)
            for name in hv:  # header variables only if declared AND restricted by a pattern
                if name not in declared or not declared[name].pattern:
                    raise ValueError(f"header variable '{name}' must be declared with a pattern")
            used |= hv
        if used - set(declared):
            raise ValueError(f"undeclared variables: {sorted(used - set(declared))}")
        if self.mapping:
            try:
                jp_parse(self.mapping.root)
                for f in self.mapping.fields:
                    jp_parse(f.path)
            except Exception:  # noqa: BLE001
                if not self.soap:
                    raise ValueError("invalid JSONPath in mapping") from None
        return self


def _coerce(v: Any, t: str) -> Any:
    if v is None:
        return None
    try:
        if t == "number":
            return v if isinstance(v, (int, float)) and not isinstance(v, bool) else float(v)
        if t == "boolean":
            return v if isinstance(v, bool) else str(v).lower() == "true"
        if t == "json":
            return v
        return v if isinstance(v, str) else (json.dumps(v) if isinstance(v, (dict, list)) else str(v))
    except (TypeError, ValueError):
        return None


def _flatten(data: Any, prefix: str = "") -> list[list]:
    if isinstance(data, dict):
        return [r for k, v in data.items() for r in _flatten(v, f"{prefix}.{k}" if prefix else str(k))]
    if isinstance(data, list):
        return [r for i, v in enumerate(data) for r in _flatten(v, f"{prefix}[{i}]")]
    return [[prefix or "value", data if isinstance(data, (str, int, float, bool)) or data is None else str(data)]]


def _strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def map_json(data: Any, mapping: Mapping | None) -> tuple[list[dict], list[list]]:
    if mapping is None:
        return [{"name": "key", "type": "string"}, {"name": "value", "type": "json"}], _flatten(data)
    items = [m.value for m in jp_parse(mapping.root).find(data)]
    exprs = [(f, jp_parse(f.path)) for f in mapping.fields]
    rows = []
    for it in items:
        row = []
        for f, ex in exprs:
            hits = ex.find(it)
            row.append(_coerce(hits[0].value if hits else None, f.type))
        rows.append(row)
    return [{"name": f.name, "type": f.type} for f in mapping.fields], rows


def map_xml(root, mapping: Mapping | None) -> tuple[list[dict], list[list]]:
    """SOAP responses: namespaces stripped, ElementTree paths (e.g. './/item', 'name')."""
    _strip_ns(root)
    if mapping is None:
        return ([{"name": "key", "type": "string"}, {"name": "value", "type": "json"}],
                [[el.tag, (el.text or "").strip()] for el in root.iter() if len(el) == 0])
    rows = []
    for it in root.findall(mapping.root):
        row = []
        for f in mapping.fields:
            el = it.find(f.path)
            row.append(_coerce(el.text.strip() if el is not None and el.text else None, f.type))
        rows.append(row)
    return [{"name": f.name, "type": f.type} for f in mapping.fields], rows


class ApiResponseView:
    key = "api_response"
    permission = "view.api_response.create"
    source_kind = "api"
    config_schema = ApiResponseConfig
    default_renderer = "table"

    def variables(self, cfg: ApiResponseConfig) -> list[Variable]:
        return cfg.variables

    def build_request(self, cfg: ApiResponseConfig, h: Handle, params: dict):
        base = h.config["base_url"].rstrip("/")
        url = base + render.url_part(cfg.path, params)
        if cfg.query_template:
            url += "?" + render.url_part(cfg.query_template, params)
        headers, body = {}, None
        for k, t in cfg.headers.items():
            try:
                headers[k] = render.header_value(t, params)
            except ValueError:
                raise AppError("VALIDATION", "Invalid header value") from None
        if h.config["kind"] == "soap":
            if not cfg.soap:
                raise AppError("VALIDATION", "soap settings are required for a SOAP resource")
            headers.update({"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"{cfg.soap.operation}"'})
            body = render.xml_text(cfg.soap.envelope_template, params).encode()
        elif cfg.body_template is not None:
            data = render.json_value(cfg.body_template, params)
            if cfg.body_format == "form":
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                body = urllib.parse.urlencode(data if isinstance(data, dict) else {}).encode()
            else:
                headers["Content-Type"] = "application/json"
                body = json.dumps(data).encode()
        return url, headers, body

    def run(self, ctx: RunContext, cfg: ApiResponseConfig, h: Handle) -> dict:
        url, headers, body = self.build_request(cfg, h, ctx.params)
        t0 = time.monotonic()
        method = "POST" if h.config["kind"] == "soap" else cfg.method
        status, data = ctx.meta["connectors"]["api"].request(h, method, url, headers, body)
        if status >= 400:
            raise ConnectorError(f"API returned HTTP {status}")
        try:
            if h.config["kind"] == "soap":
                cols, rows = map_xml(DET.fromstring(data), cfg.mapping)
            else:
                cols, rows = map_json(json.loads(data), cfg.mapping)
        except (ValueError, DET.ParseError):
            raise ConnectorError("Response could not be parsed") from None
        return result_set(cols, rows, row_limit=ctx.row_limit, source=h.id,
                          duration_ms=int((time.monotonic() - t0) * 1000))

    def llm_tool_schema(self, cfg: ApiResponseConfig) -> dict:
        return variables_json_schema(cfg.variables)
