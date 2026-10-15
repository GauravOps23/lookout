import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_LENGTH = 12
_COMMON = {
    "password1234", "123456789012", "qwertyuiop12", "administrator", "welcome12345",
    "letmein12345", "airtel123456", "changeme1234", "password12345", "iloveyou1234",
}

_hasher = PasswordHasher()
# Verified against when the login is unknown, so timing does not reveal which logins exist.
DUMMY_HASH = _hasher.hash("dummy-password-for-timing")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except (VerificationError, InvalidHashError):
        return False


def validate_strength(password: str) -> str | None:
    """Return an error message, or None if the password is acceptable."""
    if len(password) < MIN_LENGTH:
        return f"Password must be at least {MIN_LENGTH} characters"
    if password.lower() in _COMMON or len(set(password)) < 4:
        return "Password is too common"
    return None


def temp_password() -> str:
    return secrets.token_urlsafe(15)
