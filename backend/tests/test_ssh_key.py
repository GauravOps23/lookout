import pytest
from cryptography.hazmat.primitives import serialization as s
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from app.resources.connectors.base import ConnectorError
from app.resources.connectors.ssh import _load_key


def _ed25519(fmt=s.PrivateFormat.OpenSSH, enc=None):
    k = ed25519.Ed25519PrivateKey.generate()
    return k.private_bytes(s.Encoding.PEM, fmt, enc or s.NoEncryption()).decode()


def test_loads_openssh_ed25519():
    assert _load_key(_ed25519(), None).get_name() == "ssh-ed25519"


def test_loads_pem_rsa():
    k = rsa.generate_private_key(65537, 2048)
    pem = k.private_bytes(s.Encoding.PEM, s.PrivateFormat.TraditionalOpenSSL, s.NoEncryption()).decode()
    assert _load_key(pem, None).get_name() == "ssh-rsa"


def test_passphrase_required_and_accepted():
    pem = _ed25519(enc=s.BestAvailableEncryption(b"pw"))
    with pytest.raises(ConnectorError, match="KEY_PASSPHRASE_REQUIRED"):
        _load_key(pem, None)
    assert _load_key(pem, "pw").get_name() == "ssh-ed25519"


def test_garbage_rejected():
    with pytest.raises(ConnectorError, match="KEY_INVALID"):
        _load_key("not a key", None)
