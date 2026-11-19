import pytest

from travelrag.core.security import (
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)

SECRET = "unit-test-secret-at-least-32-bytes-long"


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("correct horse")
    assert verify_password("correct horse", hashed)
    assert not verify_password("wrong horse", hashed)


def test_token_roundtrip() -> None:
    token = create_access_token("user-1", secret=SECRET, algorithm="HS256", expires_minutes=5)
    assert decode_access_token(token, secret=SECRET, algorithm="HS256") == "user-1"


def test_token_rejects_wrong_secret_and_expiry() -> None:
    token = create_access_token("user-1", secret=SECRET, algorithm="HS256", expires_minutes=5)
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, secret=SECRET[::-1], algorithm="HS256")
    expired = create_access_token("user-1", secret=SECRET, algorithm="HS256", expires_minutes=-1)
    with pytest.raises(InvalidTokenError):
        decode_access_token(expired, secret=SECRET, algorithm="HS256")
