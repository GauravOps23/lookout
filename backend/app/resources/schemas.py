"""Per-type resource config and secret schemas (spec 6.2). Unknown fields are rejected."""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter, model_validator


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SshConfig(_S):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=64)
    auth: Literal["password", "key", "local"] = "password"
    host_key_fingerprint: str | None = None
    connect_timeout_s: int = Field(10, ge=1, le=60)


class SshSecret(_S):
    password: str | None = None
    private_key: str | None = None
    passphrase: str | None = None


class OracleConfig(_S):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(1521, ge=1, le=65535)
    service_name: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=128)
    fetch_size: int = Field(500, ge=1, le=10000)
    call_timeout_ms: int = Field(30000, ge=1000, le=600000)
    connect_timeout_s: int = Field(10, ge=1, le=60)


class OracleSecret(_S):
    password: str = Field(min_length=1)


class ApiConfig(_S):
    kind: Literal["rest", "soap"] = "rest"
    base_url: str
    allowed_methods: list[Literal["GET", "POST"]] = ["GET"]  # only GET and POST exist
    default_headers: dict[str, str] = {}
    auth: Literal["none", "basic", "bearer", "api_key"] = "none"
    api_key_header: str = "X-Key"
    timeout_s: int = Field(15, ge=1, le=120)
    verify_tls: bool = True

    @model_validator(mode="after")
    def _url(self):
        TypeAdapter(HttpUrl).validate_python(self.base_url)
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be http(s)")
        if any("\n" in k + v or "\r" in k + v for k, v in self.default_headers.items()):
            raise ValueError("invalid header")
        return self


class ApiSecret(_S):
    username: str | None = None
    password: str | None = None
    token: str | None = None
    api_key: str | None = None


class OcpConfig(_S):
    """An OpenShift/Kubernetes cluster, read through its REST API with a service-account token."""
    api_url: str  # e.g. https://api.cluster.example:6443
    namespaces: list[str] = Field([], max_length=50)  # empty = all; set it when the token cannot list cluster-wide
    timeout_s: int = Field(15, ge=1, le=120)
    verify_tls: bool = True

    @model_validator(mode="after")
    def _url(self):
        TypeAdapter(HttpUrl).validate_python(self.api_url)
        if not self.api_url.startswith(("http://", "https://")):
            raise ValueError("api_url must be http(s)")
        if any(len(n) > 63 or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", n) for n in self.namespaces):
            raise ValueError("invalid namespace name")
        return self


class OcpSecret(_S):
    token: str = Field(min_length=1)


TYPES = {
    "ssh": (SshConfig, SshSecret, "inventory.ssh.create"),
    "oracle": (OracleConfig, OracleSecret, "inventory.oracle.create"),
    "api": (ApiConfig, ApiSecret, "inventory.api.create"),
    "ocp": (OcpConfig, OcpSecret, "inventory.ocp.create"),
}
