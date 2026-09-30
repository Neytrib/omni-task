"""Owner-scoped, content-free live hints; PostgreSQL remains authoritative."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from contextlib import suppress
from dataclasses import dataclass, field
from uuid import UUID

from fastapi import WebSocket
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from starlette.websockets import WebSocketDisconnect

from app import auth
from app.errors import AppError
from omni_logging import correlation_context, log_event

logger = logging.getLogger("omni_task.live")
EVENT_TYPES = {"task_created", "task_updated", "task_deleted"}
QUEUE_SIZE = 64
IO_TIMEOUT_SECONDS = 2


def decode_hint(raw: object) -> dict | None:
    """Reject unexpected fields, including task content, before fan-out."""
    if not isinstance(raw, (str, bytes)) or len(raw) > 1024:
        return None
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"type", "owner_id", "task_id", "revision"}:
            return None
        if value["type"] not in EVENT_TYPES:
            return None
        if type(value["revision"]) is not int or not 1 <= value["revision"] <= 2**53 - 1:
            return None
        return {
            "type": value["type"],
            "owner_id": str(UUID(value["owner_id"])),
            "task_id": str(UUID(value["task_id"])),
            "revision": value["revision"],
        }
    except (ValueError, TypeError, AttributeError, UnicodeError):
        return None


@dataclass(eq=False)
class Connection:
    owner_id: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_SIZE))

    def offer(self, message: dict, live: bool) -> None:
        if self.queue.full():
            # A slow tab needs one authoritative reload, not an unbounded event backlog.
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait({"type": "resync", "live": live})
        else:
            self.queue.put_nowait(message)


class LiveHub:
    """This API process owns sockets only; Redis fans hints across API processes."""

    def __init__(self) -> None:
        self.live = False
        self.connections: set[Connection] = set()

    def register(self, owner_id: str) -> Connection:
        connection = Connection(owner_id)
        self.connections.add(connection)
        return connection

    def unregister(self, connection: Connection) -> None:
        self.connections.discard(connection)

    def set_live(self, live: bool, *, force: bool = False) -> None:
        if self.live == live and not force:
            return
        self.live = live
        for connection in tuple(self.connections):
            connection.offer({"type": "resync", "live": live}, live)

    def dispatch(self, hint: dict) -> None:
        message = {key: hint[key] for key in ("type", "task_id", "revision")}
        for connection in tuple(self.connections):
            if connection.owner_id == hint["owner_id"]:
                connection.offer(message, self.live)


async def subscriber_loop(settings, hub: LiveHub, stop: asyncio.Event) -> None:
    client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=IO_TIMEOUT_SECONDS,
        socket_timeout=IO_TIMEOUT_SECONDS,
        health_check_interval=0,
        retry=Retry(NoBackoff(), 0),
        client_name=f"{settings.live_channel}:subscriber:{os.getpid()}",
    )
    delay = 0.25
    try:
        while not stop.is_set():
            try:
                async with client.pubsub() as subscription:
                    await subscription.subscribe(settings.live_channel)
                    acknowledged = False
                    loop = asyncio.get_running_loop()
                    acknowledgement_deadline = loop.time() + IO_TIMEOUT_SECONDS
                    next_ping = loop.time() + 5
                    pong_deadline = None
                    while not stop.is_set():
                        now = loop.time()
                        if not acknowledged and now >= acknowledgement_deadline:
                            raise TimeoutError("Subscription acknowledgement timed out")
                        if pong_deadline is not None and now >= pong_deadline:
                            raise TimeoutError("Subscriber heartbeat timed out")
                        if acknowledged and pong_deadline is None and now >= next_ping:
                            await subscription.ping("live")
                            pong_deadline = loop.time() + IO_TIMEOUT_SECONDS
                        message = await subscription.get_message(
                            ignore_subscribe_messages=False, timeout=1
                        )
                        if message is None:
                            continue
                        if message["type"] == "subscribe":
                            # subscribe() writes the command; only the Redis reply proves readiness.
                            acknowledged = True
                            hub.set_live(True, force=True)
                            delay = 0.25
                        elif message["type"] == "pong":
                            pong_deadline = None
                            next_ping = loop.time() + 5
                        elif message["type"] == "message" and acknowledged:
                            hint = decode_hint(message["data"])
                            if hint is not None:
                                hub.dispatch(hint)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Exception reprs may contain broker credentials; log fixed metadata only.
                log_event(
                    logger,
                    "live_subscriber_reconnecting",
                    level=logging.WARNING,
                    error_code="broker_unavailable",
                )
            finally:
                hub.set_live(False)
            if not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay * random.uniform(0.8, 1.2))
                except TimeoutError:
                    pass
                delay = min(delay * 2, 5)
    finally:
        hub.set_live(False)
        await client.aclose()


def session_state(factory, token: str | None) -> tuple[str, int]:
    # Never retain ORM sessions across async waits or share them between threads.
    with factory() as db:
        current = auth.authenticate_session(db, token)
        return str(current.user.id), current.user.task_revision


async def _session_state(factory, token: str | None) -> tuple[str, int]:
    return await asyncio.wait_for(
        asyncio.to_thread(session_state, factory, token), timeout=IO_TIMEOUT_SECONDS
    )


async def _send(socket: WebSocket, message: dict) -> None:
    await asyncio.wait_for(socket.send_json(message), timeout=IO_TIMEOUT_SECONDS)


async def task_socket(socket: WebSocket) -> None:
    with correlation_context():
        await _task_socket(socket)


async def _task_socket(socket: WebSocket) -> None:
    settings = socket.app.state.settings
    if socket.headers.getlist("origin") != [settings.dashboard_origin] or socket.scope.get(
        "query_string"
    ):
        await socket.close(code=4403)
        return
    if not settings.live_enabled:
        await socket.close(code=1013)
        return
    factory = socket.app.state.session_factory
    token = socket.cookies.get(settings.cookie_name)
    try:
        owner_id, _ = await _session_state(factory, token)
    except AppError:
        await socket.close(code=4401)
        return
    except Exception:
        await socket.close(code=1013)
        return

    hub: LiveHub = socket.app.state.live_hub
    connection = hub.register(owner_id)
    receiver = None
    queued = None
    try:
        # Registration precedes this fresh revision read and the browser's initial snapshot.
        # Any commit racing that snapshot is buffered as a hint or repaired by heartbeat.
        current_owner, revision = await _session_state(factory, token)
        if current_owner != owner_id:
            await socket.close(code=4401)
            return
        await socket.accept()
        await _send(socket, {"type": "ready", "revision": revision, "live": hub.live})
        receiver = asyncio.create_task(socket.receive())
        queued = asyncio.create_task(connection.queue.get())
        loop = asyncio.get_running_loop()
        next_heartbeat = loop.time() + settings.live_heartbeat_seconds
        while True:
            done, _ = await asyncio.wait(
                {receiver, queued},
                timeout=max(0, next_heartbeat - loop.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if receiver in done:
                received = receiver.result()
                if received["type"] != "websocket.disconnect":
                    # This endpoint is read-only: clients cannot supply actor/task events.
                    await socket.close(code=1008)
                return
            if loop.time() >= next_heartbeat:
                current_owner, revision = await _session_state(factory, token)
                if current_owner != owner_id:
                    await socket.close(code=4401)
                    return
                await _send(socket, {"type": "heartbeat", "revision": revision, "live": hub.live})
                next_heartbeat = loop.time() + settings.live_heartbeat_seconds
            if queued in done:
                message = queued.result()
                if message["type"] == "resync":
                    _, revision = await _session_state(factory, token)
                    message = {**message, "revision": revision}
                await _send(socket, message)
                queued = asyncio.create_task(connection.queue.get())
    except AppError:
        with suppress(WebSocketDisconnect, RuntimeError, OSError):
            await socket.close(code=4401)
    except (WebSocketDisconnect, OSError):
        pass
    except Exception:
        log_event(
            logger,
            "live_connection_closed",
            level=logging.WARNING,
            error_code="connection_unavailable",
        )
        with suppress(WebSocketDisconnect, RuntimeError, OSError):
            await socket.close(code=1013)
    finally:
        hub.unregister(connection)
        for task in (receiver, queued):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError, WebSocketDisconnect, OSError):
                    await task
