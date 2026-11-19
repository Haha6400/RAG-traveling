from datetime import UTC, datetime, timedelta

import jwt
from pwdlib import PasswordHash

_hasher = PasswordHash.recommended()


class InvalidTokenError(Exception):
    pass


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _hasher.verify(password, password_hash)


def create_access_token(subject: str, *, secret: str, algorithm: str, expires_minutes: int) -> str:
    now = datetime.now(UTC)
    claims = {"sub": subject, "iat": now, "exp": now + timedelta(minutes=expires_minutes)}
    return jwt.encode(claims, secret, algorithm=algorithm)


def decode_access_token(token: str, *, secret: str, algorithm: str) -> str:
    """Return the token subject, or raise InvalidTokenError."""
    try:
        claims = jwt.decode(token, secret, algorithms=[algorithm])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc
    subject = claims.get("sub")
    if not isinstance(subject, str):
        raise InvalidTokenError("missing subject")
    return subject
