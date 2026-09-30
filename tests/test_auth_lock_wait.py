from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from time import monotonic, sleep

import pytest
from app import auth
from app.errors import AppError
from app.models import BrowserSession, LoginLink
from sqlalchemy import func, select, text


def test_login_expiry_rechecked_after_postgres_lock_wait(app, login_link, settings, monkeypatch):
    token = login_link()["token"]
    factory = app.state.session_factory
    clock = [auth.utcnow()]
    monkeypatch.setattr(auth, "utcnow", lambda: clock[0])
    entered = Event()
    identity = {}

    def waiting_exchange():
        with factory.begin() as db:
            identity["pid"] = db.scalar(select(func.pg_backend_pid()))
            entered.set()
            return auth.exchange_link(db, token, settings)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with factory.begin() as blocker:
            link = blocker.scalar(select(LoginLink).with_for_update())
            future = pool.submit(waiting_exchange)
            assert entered.wait(5)
            deadline = monotonic() + 5
            while monotonic() < deadline:
                with factory() as observer:
                    waiting = observer.scalar(
                        text(
                            "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid=:pid"
                        ),
                        identity,
                    )
                if waiting:
                    break
                sleep(0.01)
            else:
                raise AssertionError("Login exchange did not block on its PostgreSQL row lock")
            clock[0] = link.expires_at + timedelta(seconds=1)
        with pytest.raises(AppError) as rejected:
            future.result(timeout=5)
    assert rejected.value.status == 401
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(BrowserSession)) == 0
        assert db.scalar(select(LoginLink)).consumed_at is None
