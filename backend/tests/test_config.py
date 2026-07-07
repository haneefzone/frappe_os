from app.config import Settings


def test_cors_origins_split_and_trimmed():
    settings = Settings(cors_origins="http://localhost:5173, https://panel.example.com ,")
    assert settings.cors_origin_list == ["http://localhost:5173", "https://panel.example.com"]


def test_env_var_names_map_case_insensitively(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./test.db")
    monkeypatch.setenv("FDM_SECRET_KEY", "test-key")
    settings = Settings(_env_file=None)
    assert settings.database_url == "sqlite:///./test.db"
    assert settings.fdm_secret_key == "test-key"
