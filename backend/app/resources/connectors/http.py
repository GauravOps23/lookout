import base64
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

from app.resources.connectors.base import ConnectorError, Handle

MAX_RESPONSE = 5 * 1024 * 1024
ALLOWED_METHODS = {"GET", "POST"}  # enforced here, not only in the UI


def check_ssrf(host: str, allow: list[str] | None = None) -> None:
    """Reject loopback, link-local (cloud metadata), unspecified, multicast and reserved targets.

    RFC1918 private ranges are allowed (internal APIs are the point). An admin allow-list of
    hostnames/CIDRs overrides the block. Residual risk: DNS can change between this check and connect.
    """
    allow = allow or []
    if host in allow:
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ConnectorError("Host could not be resolved") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if any(ip in ipaddress.ip_network(a, strict=False) for a in allow if _is_net(a)):
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            raise ConnectorError("Target address is not allowed")


def _is_net(s: str) -> bool:
    try:
        ipaddress.ip_network(s, strict=False)
        return True
    except ValueError:
        return False


class HttpConnector:
    def __init__(self, ssrf_allow: list[str] | None = None):
        self.ssrf_allow = ssrf_allow or []

    def ping(self, h: Handle) -> None:
        check_ssrf(urlsplit(h.config["base_url"]).hostname or "", self.ssrf_allow)
        method = "GET" if "GET" in h.config["allowed_methods"] else "POST"
        self.request(h, method, h.config["base_url"], {}, None)

    def request(self, h: Handle, method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
        c = h.config
        method = method.upper()
        if method not in ALLOWED_METHODS or method not in c["allowed_methods"]:
            raise ConnectorError("Method not allowed for this resource")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.hostname != urlsplit(c["base_url"]).hostname:
            raise ConnectorError("URL does not match the resource")
        check_ssrf(parts.hostname, self.ssrf_allow)

        hdrs = {**c.get("default_headers", {}), **headers}
        s, auth = h.secret, c.get("auth", "none")
        if auth == "basic":
            hdrs["Authorization"] = "Basic " + base64.b64encode(
                f"{s.get('username', '')}:{s.get('password', '')}".encode()).decode()
        elif auth == "bearer":
            hdrs["Authorization"] = f"Bearer {s.get('token', '')}"
        elif auth == "api_key":
            hdrs[c.get("api_key_header", "X-Key")] = s.get("api_key", "")

        try:
            with httpx.Client(timeout=c.get("timeout_s", 15), verify=c.get("verify_tls", True),
                              follow_redirects=False) as client:
                with client.stream(method, url, headers=hdrs, content=body) as r:
                    data = bytearray()
                    for chunk in r.iter_bytes():
                        data += chunk
                        if len(data) > MAX_RESPONSE:
                            raise ConnectorError("Response too large")
                    return r.status_code, bytes(data)
        except httpx.TimeoutException:
            raise ConnectorError("TIMEOUT", "TIMEOUT") from None
        except httpx.HTTPError:
            raise ConnectorError("HTTP request failed") from None
