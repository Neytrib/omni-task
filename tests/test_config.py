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


def test_local_browser_configuration_remains_same_origin():
    settings = configuration()
    assert settings.dashboard_url is None
    assert settings.cookie_samesite == "lax"
    assert not settings.cookie_partitioned
    assert settings.cookie_name == "omni_session"
    assert configuration(dashboard_url="").dashboard_url is None


def test_hosted_browser_configuration_accepts_secure_partitioned_cookie():
    settings = configuration(
        app_env="production",
        dashboard_origin="https://pages.example/",
        dashboard_url="https://pages.example/project/",
        cookie_secure=True,
        cookie_samesite="none",
        cookie_partitioned=True,
        cookie_name="__Host-omni_session",
    )
    assert settings.dashboard_origin == "https://pages.example"
    assert settings.dashboard_url == "https://pages.example/project/"


@pytest.mark.parametrize(
    "changes",
    [
        {"cookie_samesite": "none", "cookie_secure": False},
        {"cookie_samesite": "none", "cookie_secure": True, "dashboard_origin": "http://localhost"},
        {"cookie_partitioned": True},
        {"cookie_partitioned": True, "cookie_samesite": "strict", "cookie_secure": True},
        {"cookie_name": "__Host-omni_session", "cookie_secure": False},
        {"cookie_name": "__Secure-omni_session", "cookie_secure": False},
        {"cookie_name": "session;unsafe"},
        {"cookie_name": "session\r\nother"},
        {"cookie_name": ""},
    ],
)
def test_unsafe_cookie_configurations_fail_closed(changes):
    with pytest.raises(ValidationError):
        configuration(**{"dashboard_origin": "https://pages.example", **changes})


@pytest.mark.parametrize(
    "url",
    [
        "https://other.example/project/",
        "http://pages.example/project/",
        "//pages.example/project/",
        "https://pages.example:444/project/",
        "https://user:secret@pages.example/project/",
        "https://pages.example/project/?token=synthetic",
        "https://pages.example/project/#token=synthetic",
        "https://pages.example\\attacker.example/project/",
        "https://pages.example/\nproject/",
    ],
)
def test_dashboard_link_must_use_configured_origin_without_credentials_or_token(url):
    with pytest.raises(ValidationError):
        configuration(
            dashboard_origin="https://pages.example", cookie_secure=True, dashboard_url=url
        )


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
