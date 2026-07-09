import pytest
from pydantic import ValidationError

from app.config import (
    FDM_SECRET_KEY_PLACEHOLDER,
    JWT_SECRET_PLACEHOLDER,
    Settings,
    get_settings,
)

# Real-shaped secrets for the debug=False happy path.
GOOD_JWT_SECRET = "u" * 48
GOOD_FERNET_KEY = "3vGpZ0Jb8xJXn4mYq2sD7kT1wR5cA9fLhN6uE0iOgQY="


def _prod_settings(**overrides):
    values = {
        "debug": False,
        "jwt_secret": GOOD_JWT_SECRET,
        "fdm_secret_key": GOOD_FERNET_KEY,
        **overrides,
    }
    return Settings(_env_file=None, **values)


def test_cors_origins_split_and_trimmed():
    settings = Settings(cors_origins="http://localhost:5173, https://panel.example.com ,")
    assert settings.cors_origin_list == ["http://localhost:5173", "https://panel.example.com"]


def test_env_var_names_map_case_insensitively(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./test.db")
    monkeypatch.setenv("FDM_SECRET_KEY", "test-key")
    settings = Settings(_env_file=None)
    assert settings.database_url == "sqlite:///./test.db"
    assert settings.fdm_secret_key == "test-key"


# --- SEC-H1 (DOO-66): fail-closed startup validation of signing secrets ---


def test_prod_refuses_placeholder_jwt_secret():
    with pytest.raises(ValidationError, match="token_urlsafe"):
        _prod_settings(jwt_secret=JWT_SECRET_PLACEHOLDER)


def test_prod_refuses_short_jwt_secret():
    with pytest.raises(ValidationError, match="32 bytes"):
        _prod_settings(jwt_secret="x" * 31)


def test_prod_refuses_placeholder_fernet_key():
    with pytest.raises(ValidationError, match="Fernet.generate_key"):
        _prod_settings(fdm_secret_key=FDM_SECRET_KEY_PLACEHOLDER)


def test_prod_starts_with_real_secrets():
    settings = _prod_settings()
    assert settings.jwt_secret == GOOD_JWT_SECRET


def test_debug_true_tolerates_placeholders():
    settings = Settings(_env_file=None, debug=True)
    assert settings.jwt_secret == JWT_SECRET_PLACEHOLDER


def test_startup_path_fails_closed_via_get_settings(monkeypatch):
    """End-to-end proof: importing the app with DEBUG=false and placeholder
    secrets aborts before it can serve (get_settings() is the startup path)."""
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("FDM_SECRET_KEY", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError, match="Refusing to start"):
            get_settings()
    finally:
        get_settings.cache_clear()
