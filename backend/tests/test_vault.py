import uuid

import pytest

from app.errors import AppError
from app.secrets.vault import Vault


def vault(version=1, keys=None):
    return Vault(keys or {1: b"k" * 32}, version)


def test_round_trip():
    v, rid = vault(), uuid.uuid4()
    assert v.open(v.seal(b"s3cret", rid), rid) == b"s3cret"


def test_blobs_differ_each_time():
    v, rid = vault(), uuid.uuid4()
    assert v.seal(b"x", rid) != v.seal(b"x", rid)


def test_wrong_aad_fails():
    v = vault()
    blob = v.seal(b"x", uuid.uuid4())
    with pytest.raises(AppError):
        v.open(blob, uuid.uuid4())


def test_tamper_detected():
    v, rid = vault(), uuid.uuid4()
    blob = bytearray(v.seal(b"hello", rid))
    blob[-1] ^= 1
    with pytest.raises(AppError):
        v.open(bytes(blob), rid)


def test_rotation_rewraps_dek_only():
    rid = uuid.uuid4()
    old = vault(1, {1: b"a" * 32})
    blob = old.seal(b"pw", rid)
    new = vault(2, {1: b"a" * 32, 2: b"b" * 32})
    rotated = new.rewrap(blob, rid)
    assert rotated[:4] == (2).to_bytes(4, "big") and rotated[64:] == blob[64:]
    assert new.open(rotated, rid) == b"pw"
    with pytest.raises(AppError):  # a vault holding only the retired key cannot open it
        old.open(rotated, rid)
