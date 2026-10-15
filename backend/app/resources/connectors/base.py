import re
import socket
from dataclasses import dataclass, field


class ConnectorError(Exception):
    """Carries a message that is safe to store and show (no hosts, credentials or SQL)."""

    def __init__(self, message: str, code: str = "UPSTREAM_ERROR"):
        super().__init__(message)
        self.message, self.code = message, code


@dataclass
class Handle:
    """Everything a connector needs, with the secret already decrypted. Only connectors read `secret`."""
    id: str
    name: str
    type: str
    config: dict
    secret: dict = field(default_factory=dict, repr=False)
    observed: dict = field(default_factory=dict)  # e.g. {"host_key_fingerprint": "..."} for TOFU


_CODE = re.compile(r"\b(ORA-\d{5}|DPY-\d{4})\b")


def sanitize(exc: Exception) -> str:
    """Turn any exception into text without hostnames, credentials or SQL."""
    if isinstance(exc, ConnectorError):
        return exc.message
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "TIMEOUT"
    m = _CODE.search(str(exc))
    if m:
        return f"Oracle error {m.group(1)}"
    return f"Unexpected error ({type(exc).__name__})"
