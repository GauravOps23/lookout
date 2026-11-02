import json
from urllib.parse import urlencode

from app.resources.connectors.base import ConnectorError, Handle
from app.resources.connectors.http import HttpConnector


class OcpConnector:
    """Read-only GETs against the cluster API. All network rules (SSRF guard, size cap, TLS) live in HttpConnector."""

    def __init__(self, ssrf_allow: list[str] | None = None):
        self.http = HttpConnector(ssrf_allow)

    @staticmethod
    def _as_http(h: Handle) -> Handle:
        c = h.config
        return Handle(id=h.id, name=h.name, type="api", secret={"token": h.secret.get("token", "")}, config={
            "base_url": c["api_url"], "allowed_methods": ["GET"], "auth": "bearer", "timeout_s": c.get("timeout_s", 15),
            "verify_tls": c.get("verify_tls", True), "default_headers": {"Accept": "application/json"}})

    def get_json(self, h: Handle, path: str, query: dict | None = None):
        hh = self._as_http(h)
        url = hh.config["base_url"].rstrip("/") + path + ("?" + urlencode(query) if query else "")
        status, body = self.http.request(hh, "GET", url, {}, None)
        if status in (401, 403):
            raise ConnectorError("The cluster rejected the token")
        if status >= 400:
            raise ConnectorError(f"The cluster API returned HTTP {status}")
        try:
            return json.loads(body)
        except ValueError:
            raise ConnectorError("The cluster API did not return JSON") from None

    def ping(self, h: Handle) -> None:
        self.get_json(h, "/version")
