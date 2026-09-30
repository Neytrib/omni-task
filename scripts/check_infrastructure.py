"""Assert Compose/network boundaries; --runtime also checks live containers and the bot image."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def capture(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True)


def main() -> None:
    config = json.loads(capture("docker", "compose", "config", "--format", "json"))
    services = config["services"]
    assert set(services) == {"api", "bot", "worker", "frontend", "postgres", "redis", "migrate"}
    for name in ("api", "worker", "bot", "postgres", "redis", "migrate"):
        assert not services[name].get("ports"), f"Unexpected published {name} port"
    assert services["frontend"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert config["networks"]["data"]["internal"]
    assert "data" not in services["bot"]["networks"]
    bot_env = services["bot"]["environment"]
    assert set(bot_env) == {
        "BOT_API_KEY",
        "API_BASE_URL",
        "TELEGRAM_BOT_TOKEN",
        "VOICE_MAX_DURATION_SECONDS",
        "VOICE_MAX_BYTES",
        "VOICE_DOWNLOAD_TIMEOUT_SECONDS",
    }
    for service in ("api", "worker"):
        condition = services[service]["depends_on"]["migrate"]["condition"]
        assert condition == "service_completed_successfully"
    for name, service in services.items():
        environment = service.get("environment", {})
        if name != "worker":
            assert "OPENAI_API_KEY" not in environment, f"Provider credential leaked to {name}"
        if name != "bot":
            assert "TELEGRAM_BOT_TOKEN" not in environment, f"Telegram credential leaked to {name}"
    print("Compose boundaries passed: loopback frontend; private data network; bot HTTP-only.")
    if "--runtime" not in sys.argv:
        return
    for name in ("postgres", "redis"):
        container = capture("docker", "compose", "ps", "-q", name).strip()
        details = json.loads(capture("docker", "inspect", container))[0]
        assert not details["HostConfig"]["PortBindings"], f"Published runtime ports for {name}"
        assert details["State"]["Health"]["Status"] == "healthy"
    bot_container = capture("docker", "compose", "ps", "-q", "bot").strip()
    bot_details = json.loads(capture("docker", "inspect", bot_container))[0]
    assert config["networks"]["data"]["name"] not in bot_details["NetworkSettings"]["Networks"]
    bot_check = (
        "import importlib.util, os; "
        "assert all(importlib.util.find_spec(name) is None "
        "for name in ('sqlalchemy', 'psycopg', 'redis', 'celery')); "
        "assert not any(key in os.environ for key in "
        "('DATABASE_URL','REDIS_URL','POSTGRES_PASSWORD','OPENAI_API_KEY')); "
        "print('Runtime bot dependency/credential isolation passed.')"
    )
    print(capture("docker", "compose", "exec", "-T", "bot", "python", "-c", bot_check).strip())
    print("Runtime PostgreSQL/Redis checks passed: healthy and no host port bindings.")


if __name__ == "__main__":
    main()
