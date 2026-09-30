import pytest
from app.config import Settings
from app.services import derive_title
from pydantic import ValidationError


def configuration(**values):
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://test:test@localhost/omni_task_test",
        redis_url="redis://localhost:6379/15",
        bot_api_key="synthetic-configuration-key-not-a-real-secret",
        **values,
    )


@pytest.mark.parametrize(
    "origin,secure",
    [
        ("http://example.test", False),
        ("https://example.test", False),
        ("http://example.test", True),
    ],
)
def test_production_requires_https_and_secure_cookie(origin, secure):
    with pytest.raises(ValidationError):
        configuration(app_env="production", dashboard_origin=origin, cookie_secure=secure)


def test_production_accepts_https_secure_cookie():
    settings = configuration(
        app_env="production", dashboard_origin="https://example.test", cookie_secure=True
    )
    assert settings.cookie_secure


def test_insecure_development_cookie_requires_loopback():
    with pytest.raises(ValidationError):
        configuration(
            app_env="development", dashboard_origin="http://example.test", cookie_secure=False
        )
    assert not configuration(
        app_env="development", dashboard_origin="http://localhost:8080", cookie_secure=False
    ).cookie_secure


@pytest.mark.parametrize(
    "content,expected",
    [
        ("  First\n\tsecond   third  ", "First second third"),
        ("A" * 80, "A" * 80),
        ("🧪" * 81, "🧪" * 79 + "…"),
        ("Prepare " + "report " * 15, "Prepare " + "report " * 9 + "report…"),
    ],
)
def test_titles_are_deterministic_and_unicode_bounded(content, expected):
    assert derive_title(content) == expected
    assert len(derive_title(content)) <= 80
