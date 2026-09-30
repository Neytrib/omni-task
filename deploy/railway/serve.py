"""Run one API or polling-bot process on Railway's public and private networks."""

import argparse
import os
import socket

import uvicorn


def bind_socket(port: int) -> socket.socket:
    # asyncio's host='::' can force IPV6_V6ONLY. Pass an explicitly dual-stack socket
    # so both Railway's IPv4 health checks and IPv6 private traffic reach this process.
    return socket.create_server(("::", port), family=socket.AF_INET6, dualstack_ipv6=True)


def configuration(service: str, port: str | None = None) -> uvicorn.Config:
    value = (
        port if port is not None else os.environ.get("PORT", "8000" if service == "api" else "8001")
    )
    try:
        number = int(value)
        if not 1 <= number <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("PORT must be an integer between 1 and 65535") from None
    if service not in {"api", "bot"}:
        raise ValueError("Service must be api or bot")
    return uvicorn.Config(
        "app.main:create_app" if service == "api" else "bot.app.main:create_app",
        factory=True,
        port=number,
        host="::",
        workers=1,
        access_log=False,
        log_level="warning",
        ws="websockets-sansio" if service == "api" else "none",
        ws_max_size=1024,
        timeout_graceful_shutdown=25,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=["api", "bot"])
    options = parser.parse_args()
    config = configuration(options.service)
    with bind_socket(config.port) as listener:
        uvicorn.Server(config).run(sockets=[listener])


if __name__ == "__main__":
    main()
