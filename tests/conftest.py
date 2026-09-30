"""Foundation tests use a dedicated, disposable PostgreSQL database only."""

from __future__ import annotations

import os
import uuid
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://testserver"
BOT_KEY = "synthetic-foundation-test-key-not-a-real-secret"


@pytest.fixture(scope="session")
def database_url():
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.fail(
            "Set TEST_DATABASE_URL to a dedicated PostgreSQL database whose name contains 'test'."
        )
    parsed = make_url(value)
    if parsed.get_backend_name() != "postgresql" or "test" not in (parsed.database or "").lower():
        pytest.fail(
            "Refusing destructive fixtures: use PostgreSQL and a database name containing 'test'."
        )
    return value


@pytest.fixture(scope="session")
def db_engine(database_url):
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.dialect.name == "postgresql"
        configuration = Config(str(ROOT / "alembic.ini"))
        configuration.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
        configuration.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
        configuration.attributes["connection"] = connection
        command.upgrade(configuration, "head")
        connection.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def clean_database(db_engine):
    tables = [name for name in inspect(db_engine).get_table_names() if name != "alembic_version"]
    if tables:
        quoted = ", ".join(db_engine.dialect.identifier_preparer.quote(name) for name in tables)
        with db_engine.begin() as connection:
            connection.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def db_tables(db_engine, clean_database):
    metadata = MetaData()
    metadata.reflect(db_engine)
    return metadata.tables


@pytest.fixture
def settings(database_url):
    from app.config import Settings

    return Settings(
        _env_file=None,
        database_url=database_url,
        redis_url=os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15"),
        bot_api_key=BOT_KEY,
        bot_identity="foundation-test-bot",
        dashboard_origin=ORIGIN,
        app_env="test",
        cookie_secure=False,
        voice_dispatch_enabled=False,
        live_enabled=False,
    )


@pytest.fixture
def app(settings, clean_database):
    from app.main import create_app

    return create_app(settings)


@pytest.fixture
def api(app):
    with TestClient(app, base_url=ORIGIN) as client:
        yield client


@pytest.fixture
def bot_headers():
    return {"Authorization": f"Bearer {BOT_KEY}"}


@pytest.fixture
def users(api, bot_headers):
    records = []
    for telegram_id in (810001, 810002):
        response = api.post(
            "/internal/bot/users",
            headers=bot_headers,
            json={"telegram_user_id": telegram_id, "private_chat_id": telegram_id},
        )
        assert response.status_code in (200, 201)
        records.append(response.json())
    return records


@pytest.fixture
def make_task(api, bot_headers, users):
    messages = iter(range(1, 10000))

    def create(user=0, content="Synthetic task content", message_id=None):
        response = api.post(
            "/internal/bot/tasks",
            headers=bot_headers,
            json={
                "telegram_user_id": users[user]["telegram_user_id"],
                "message_id": next(messages) if message_id is None else message_id,
                "content": content,
            },
        )
        assert response.status_code in (200, 201)
        return response.json()

    return create


@pytest.fixture
def login_link(api, bot_headers, users):
    def issue(user=0):
        response = api.post(
            "/internal/bot/login-links",
            headers=bot_headers,
            json={"telegram_user_id": users[user]["telegram_user_id"]},
        )
        assert response.status_code in (200, 201)
        result = response.json()
        fragment = parse_qs(urlsplit(result["url"]).fragment)
        result["token"] = result.get("token") or fragment["token"][0]
        return result

    return issue


@pytest.fixture
def browser(app, login_link):
    with ExitStack() as stack:

        def connect(user=0):
            client = stack.enter_context(TestClient(app, base_url=ORIGIN))
            response = client.post(
                "/api/auth/exchange",
                json={"token": login_link(user)["token"]},
                headers={"Origin": ORIGIN},
            )
            assert response.status_code in (200, 201, 204)
            session = client.get("/api/session")
            assert session.status_code == 200
            client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": session.json()["csrf_token"]})
            return client

        yield connect


@pytest.fixture
def dashboard_create():
    def create(client, content="Synthetic dashboard task", key=None):
        return client.post(
            "/api/tasks",
            json={"content": content},
            headers={"Idempotency-Key": key or str(uuid.uuid4())},
        )

    return create
