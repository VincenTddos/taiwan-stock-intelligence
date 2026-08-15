from __future__ import annotations

import time

import pytest

from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)


@pytest.fixture
def jwt_settings() -> Settings:
    return Settings(JWT_SECRET="unit-test-secret-key-long-enough-1234567890", _env_file=None)  # type: ignore[call-arg]


def test_password_hash_is_not_reversible_and_is_salted():
    h1 = hash_password("correct horse battery staple")
    h2 = hash_password("correct horse battery staple")
    assert "correct horse" not in h1
    assert h1 != h2, "argon2 must salt each hash"
    assert h1.startswith("$argon2id$")


def test_password_verification():
    h = hash_password("s3cret-password")
    assert verify_password("s3cret-password", h) is True
    assert verify_password("wrong-password", h) is False


def test_verify_password_tolerates_garbage_hash():
    assert verify_password("anything", "not-a-hash") is False


def test_access_token_roundtrip(jwt_settings):
    token, jti, expires_at = create_access_token(jwt_settings, "42", role="admin")
    payload = decode_token(jwt_settings, token, expected_type="access")
    assert payload["sub"] == "42"
    assert payload["role"] == "admin"
    assert payload["jti"] == jti
    assert payload["exp"] == int(expires_at.timestamp())


def test_token_type_confusion_is_rejected(jwt_settings):
    """A refresh token must not be usable as an access token."""
    refresh, _, _ = create_refresh_token(jwt_settings, "42")
    with pytest.raises(AuthenticationError, match="Expected a access token"):
        decode_token(jwt_settings, refresh, expected_type="access")


def test_token_signed_with_other_secret_rejected(jwt_settings):
    token, _, _ = create_access_token(jwt_settings, "42", role="viewer")
    other = Settings(JWT_SECRET="a-completely-different-secret-key-0987654321", _env_file=None)  # type: ignore[call-arg]
    with pytest.raises(AuthenticationError, match="Invalid token"):
        decode_token(other, token, expected_type="access")


def test_expired_token_rejected():
    s = Settings(  # type: ignore[call-arg]
        JWT_SECRET="expiry-test-secret-key-long-enough-123456",
        ACCESS_TOKEN_TTL_MINUTES=0,
        _env_file=None,
    )
    token, _, _ = create_access_token(s, "42", role="viewer")
    time.sleep(1.1)
    with pytest.raises(AuthenticationError, match="expired"):
        decode_token(s, token, expected_type="access")


def test_tampered_token_rejected(jwt_settings):
    token, _, _ = create_access_token(jwt_settings, "42", role="viewer")
    head, payload, sig = token.split(".")
    with pytest.raises(AuthenticationError):
        decode_token(jwt_settings, f"{head}.{payload}.{sig[:-2]}xx", expected_type="access")
