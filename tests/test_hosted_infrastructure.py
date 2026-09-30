"""Hosted runtime contracts; no cloud account, credentials or provider calls required."""

import socket
import ssl
from pathlib import Path

import pytest
from app.redis_config import normalize_redis_url
from celery import Celery
from redis import Redis
from redis.asyncio import Redis as AsyncRedis

from deploy.railway.serve import bind_socket, configuration

ROOT = Path(__file__).resolve().parents[1]
TLS_URL = "rediss://default:synthetic-password@redis.example.invalid:6379/0"


@pytest.mark.parametrize(
    "value",
    [
        TLS_URL,
        TLS_URL + "?ssl_cert_reqs=required",
        TLS_URL + "?ssl_cert_reqs=CERT_REQUIRED&ssl_check_hostname=1",
    ],
)
def test_tls_url_verifies_certificates_and_hostnames_for_all_clients(value):
    normalized = normalize_redis_url(value)
    synchronous = Redis.from_url(normalized).connection_pool.make_connection()
    asynchronous = AsyncRedis.from_url(normalized).connection_pool.make_connection()
    assert synchronous.cert_reqs == ssl.CERT_REQUIRED
    assert synchronous.check_hostname is True
    assert asynchronous.ssl_context.cert_reqs == ssl.CERT_REQUIRED
    assert asynchronous.ssl_context.check_hostname is True
    celery = Celery("synthetic_tls_contract", broker=normalized, backend=normalized)
    try:
        assert celery.connection_for_write().ssl["ssl_cert_reqs"] == ssl.CERT_REQUIRED
        result = celery.backend.client.connection_pool.make_connection()
        assert result.cert_reqs == ssl.CERT_REQUIRED
        assert result.check_hostname is True
    finally:
        celery.close()


@pytest.mark.parametrize(
    "query",
    [
        "ssl_cert_reqs=none",
        "ssl_cert_reqs=optional",
        "ssl_cert_reqs=required&ssl_cert_reqs=none",
        "ssl_check_hostname=false",
        "ssl_check_hostname=0",
        "ssl_check_hostname=true&ssl_check_hostname=false",
    ],
)
def test_tls_verification_cannot_be_downgraded(query):
    with pytest.raises(ValueError, match="certificate and hostname") as result:
        normalize_redis_url(TLS_URL + "?" + query)
    assert "synthetic-password" not in str(result.value)
    assert "rediss://" not in str(result.value)


@pytest.mark.parametrize(
    "value",
    [
        "https://redis.example.invalid",
        "redis://",
        "redis://host:99999",
        "redis://[bad",
        TLS_URL + "#token",
    ],
)
def test_invalid_redis_urls_fail_without_echoing_credentials(value):
    with pytest.raises(ValueError, match="valid Redis connection URL") as result:
        normalize_redis_url(value)
    assert value not in str(result.value)


def test_private_compose_redis_url_remains_unchanged():
    assert normalize_redis_url("redis://redis:6379/0") == "redis://redis:6379/0"


@pytest.mark.parametrize("service", ["api", "bot"])
def test_hosted_server_honors_port_and_uses_one_process_without_access_logs(service):
    config = configuration(service, "9321")
    assert config.port == 9321
    assert config.factory is True
    assert config.workers == 1
    assert config.access_log is False
    assert config.timeout_graceful_shutdown == 25


@pytest.mark.parametrize("port", ["", "-1", "0", "65536", "bad"])
def test_hosted_server_rejects_invalid_port_without_echoing_environment(port):
    with pytest.raises(ValueError, match="PORT must be an integer"):
        configuration("api", port)


@pytest.mark.skipif(not socket.has_dualstack_ipv6(), reason="Runtime lacks IPv6 dual-stack sockets")
def test_hosted_listener_accepts_real_ipv4_and_ipv6_connections():
    with bind_socket(0) as listener:
        listener.settimeout(2)
        port = listener.getsockname()[1]
        for address in ("127.0.0.1", "::1"):
            with socket.create_connection((address, port), timeout=2) as client:
                client.sendall(b"synthetic-healthcheck")
                connection, _ = listener.accept()
                with connection:
                    assert connection.recv(64) == b"synthetic-healthcheck"


def test_hosted_images_keep_pinned_local_base_and_bot_dependency_boundary():
    original = (ROOT / "Dockerfile").read_text()
    for name in ("backend", "bot"):
        dockerfile = (ROOT / f"deploy/railway/{name}.Dockerfile").read_text()
        for line in dockerfile.splitlines():
            if line.startswith("FROM python:") or line.startswith("COPY --from=ghcr.io/"):
                assert line in original
        assert "USER app" in dockerfile
        assert "--group dev" not in dockerfile
        assert "COPY . " not in dockerfile
    bot = (ROOT / "deploy/railway/bot.Dockerfile").read_text()
    assert "--group bot" in bot
    assert "--group backend" not in bot
    assert "COPY backend" not in bot


def test_hosted_templates_start_without_paid_calls_and_keep_secrets_server_side():
    def values(name):
        text = (ROOT / f"deploy/railway/{name}.env.example").read_text()
        return dict(
            row.split("=", 1) for row in text.splitlines() if row and not row.startswith("#")
        )

    api, bot, worker = (values(name) for name in ("api", "bot", "worker"))
    assert worker["OPENAI_API_KEY"] == bot["TELEGRAM_BOT_TOKEN"] == ""
    assert worker["ALLOW_PAID_TRANSCRIPTION"] == "false"
    assert api["TRANSCRIPTION_PROVIDER"] == worker["TRANSCRIPTION_PROVIDER"] == "openai"
    assert "OPENAI_API_KEY" not in api
    assert not {"DATABASE_URL", "REDIS_URL", "OPENAI_API_KEY"} & bot.keys()
    for settings in (api, worker):
        assert "DIRECT_HOST" in settings["DATABASE_URL"]
        assert "sslmode=require" in settings["DATABASE_URL"]
        assert settings["REDIS_URL"].startswith("rediss://")
        assert "ssl_cert_reqs=required" in settings["REDIS_URL"]
        assert settings["COOKIE_PARTITIONED"] == settings["COOKIE_SECURE"] == "true"
        assert settings["COOKIE_SAMESITE"] == "none"
        assert settings["DASHBOARD_URL"].endswith("/omni-task/")
