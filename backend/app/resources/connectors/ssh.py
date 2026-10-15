import base64
import hashlib

import paramiko

from app.resources.connectors.base import ConnectorError, Handle

MAX_OUTPUT = 64 * 1024


def _fingerprint(key: paramiko.PKey) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


class _Pin(paramiko.MissingHostKeyPolicy):
    """Trust-on-first-use: accept an unknown key but record it; refuse a changed key."""

    def __init__(self, h: Handle):
        self.h = h

    def missing_host_key(self, client, hostname, key):
        seen, pinned = _fingerprint(key), self.h.config.get("host_key_fingerprint")
        if pinned and pinned != seen:
            raise ConnectorError("HOST_KEY_CHANGED")
        self.h.observed["host_key_fingerprint"] = seen


class SshConnector:
    def _client(self, h: Handle) -> paramiko.SSHClient:
        c, s = h.config, h.secret
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(_Pin(h))
        kw: dict = dict(hostname=c["host"], port=c["port"], username=c["username"],
                        timeout=c.get("connect_timeout_s", 10), allow_agent=False, look_for_keys=False)
        if c["auth"] == "password":
            kw["password"] = s.get("password")
        elif c["auth"] == "key":
            import io
            kw["pkey"] = paramiko.PKey.from_private_key(io.StringIO(s["private_key"]), s.get("passphrase"))
        else:  # local: the app account's agent / known keys
            kw.update(allow_agent=True, look_for_keys=True)
        client.connect(**kw)
        return client

    def ping(self, h: Handle) -> None:
        self.exec(h, "true", 10)

    def exec(self, h: Handle, command: str, timeout_s: int = 30) -> str:
        """Run one command: no PTY, no stdin, output capped at 64 KB."""
        client = self._client(h)
        try:
            _, out, _err = client.exec_command(command, timeout=timeout_s, get_pty=False)
            out.channel.settimeout(timeout_s)
            return out.read(MAX_OUTPUT).decode("utf-8", "replace")
        finally:
            client.close()
