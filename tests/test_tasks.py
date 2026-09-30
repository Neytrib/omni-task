from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from conftest import ORIGIN
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError


def test_text_roundtrip_preserves_complete_content(api, bot_headers, users, make_task):
    content = "  First line\n\nԵրկրորդ տող 🧪 <script>alert('text')</script>\t " + "長" * 49920
    assert len(content) < 50000
    task = make_task(content=content)
    assert task["content"] == content
    assert task["status"] == "pending"
    assert task["source"] == "telegram_text"
    assert len(task["title"]) <= 80
    assert "\n" not in task["title"]
    assert task["created_at"] and task["updated_at"]
    result = api.get(
        f"/internal/bot/tasks/{task['id']}",
        headers=bot_headers,
        params={"telegram_user_id": users[0]["telegram_user_id"]},
    )
    assert result.status_code == 200
    assert result.json()["content"] == content


@pytest.mark.parametrize(
    "content",
    ["", " \n\t", "x" * 50001, None, 123],
    ids=["empty", "whitespace", "oversized", "null", "number"],
)
def test_invalid_content_is_rejected_without_task(api, bot_headers, users, content):
    result = api.post(
        "/internal/bot/tasks",
        headers=bot_headers,
        json={
            "telegram_user_id": users[0]["telegram_user_id"],
            "message_id": 1,
            "content": content,
        },
    )
    assert result.status_code == 422
    listed = api.get(
        "/internal/bot/tasks",
        headers=bot_headers,
        params={"telegram_user_id": users[0]["telegram_user_id"]},
    )
    assert listed.json()["items"] == []


def test_two_users_are_isolated_for_every_task_operation(api, bot_headers, users, make_task):
    alice = make_task(user=0, content="Alice task body")
    bob = make_task(user=1, content="Bob task body")
    for index, expected in ((0, alice), (1, bob)):
        result = api.get(
            "/internal/bot/tasks",
            headers=bot_headers,
            params={"telegram_user_id": users[index]["telegram_user_id"]},
        )
        assert result.status_code == 200
        assert [task["id"] for task in result.json()["items"]] == [expected["id"]]
    path = f"/internal/bot/tasks/{alice['id']}"
    params = {"telegram_user_id": users[1]["telegram_user_id"]}
    for method, kwargs in (
        ("get", {}),
        ("patch", {"json": {"status": "completed"}}),
        ("delete", {}),
    ):
        result = getattr(api, method)(
            path,
            params=params,
            headers={**bot_headers, "If-Match": str(alice["version"])},
            **kwargs,
        )
        assert result.status_code == 404
        assert alice["content"] not in result.text


@pytest.mark.parametrize("status", ["done", "PENDING", "", None, 3])
def test_invalid_status_is_rejected(api, bot_headers, users, make_task, status):
    task = make_task()
    result = api.patch(
        f"/internal/bot/tasks/{task['id']}",
        params={"telegram_user_id": users[0]["telegram_user_id"]},
        headers={**bot_headers, "If-Match": str(task["version"])},
        json={"status": status},
    )
    assert result.status_code == 422


def test_valid_status_transitions_and_stale_versions(api, bot_headers, users, make_task):
    task = make_task()
    path = f"/internal/bot/tasks/{task['id']}"
    params = {"telegram_user_id": users[0]["telegram_user_id"]}
    original_version = task["version"]
    for status in ("in_progress", "completed", "pending"):
        result = api.patch(
            path,
            params=params,
            headers={**bot_headers, "If-Match": str(task["version"])},
            json={"status": status},
        )
        assert result.status_code == 200
        task = result.json()
        assert task["status"] == status
    result = api.patch(
        path,
        params=params,
        headers={**bot_headers, "If-Match": str(original_version)},
        json={"status": "completed"},
    )
    assert result.status_code == 409
    result = api.delete(
        path, params=params, headers={**bot_headers, "If-Match": str(original_version)}
    )
    assert result.status_code == 409


@pytest.mark.parametrize("version", ["0", "-1", '"1', '1"', "1,2", "*", "garbage"])
def test_malformed_versions_cannot_mutate_tasks(api, bot_headers, users, make_task, version):
    task = make_task()
    response = api.patch(
        f"/internal/bot/tasks/{task['id']}",
        params={"telegram_user_id": users[0]["telegram_user_id"]},
        headers={**bot_headers, "If-Match": version},
        json={"status": "completed"},
    )
    assert response.status_code == 422


def test_source_replay_same_text_is_one_task_and_changed_text_conflicts(
    api, bot_headers, users, make_task
):
    task = make_task(message_id=77, content="Original source body")
    duplicate = make_task(message_id=77, content="Original source body")
    assert duplicate["id"] == task["id"]
    other_message = make_task(message_id=78, content="Original source body")
    assert other_message["id"] != task["id"]
    result = api.post(
        "/internal/bot/tasks",
        headers=bot_headers,
        json={
            "telegram_user_id": users[0]["telegram_user_id"],
            "message_id": 77,
            "content": "Changed body",
        },
    )
    assert result.status_code == 409
    same_message_other_user = make_task(user=1, message_id=77)
    assert same_message_other_user["id"] != task["id"]


def test_deleted_source_never_resurrects_and_body_is_removed(
    api, bot_headers, users, make_task, db_engine, db_tables
):
    content = "Disposable secret-like synthetic content for deletion test"
    task = make_task(message_id=81, content=content)
    params = {"telegram_user_id": users[0]["telegram_user_id"]}
    response = api.delete(
        f"/internal/bot/tasks/{task['id']}",
        params=params,
        headers={**bot_headers, "If-Match": str(task["version"])},
    )
    assert response.status_code in (200, 204)
    assert (
        api.get(f"/internal/bot/tasks/{task['id']}", params=params, headers=bot_headers).status_code
        == 404
    )
    replay = api.post(
        "/internal/bot/tasks",
        headers=bot_headers,
        json={**params, "message_id": 81, "content": content},
    )
    assert replay.status_code == 410
    with db_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(db_tables["tasks"])) == 0
        assert (
            connection.scalar(select(func.count()).select_from(db_tables["source_receipts"])) == 1
        )
        for name in ("source_receipts", "processing_requests"):
            rows = connection.execute(select(db_tables[name])).mappings().all()
            assert content not in repr(rows)


def test_concurrent_source_delivery_creates_exactly_one_task(
    app, bot_headers, users, db_engine, db_tables
):
    barrier = Barrier(6)
    payload = {
        "telegram_user_id": users[0]["telegram_user_id"],
        "message_id": 99,
        "content": "Concurrent source",
    }

    def submit(_):
        with TestClient(app, base_url=ORIGIN) as client:
            barrier.wait(timeout=15)
            return client.post("/internal/bot/tasks", headers=bot_headers, json=payload)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(submit, range(6)))
    assert all(result.status_code in (200, 201) for result in results)
    assert len({result.json()["id"] for result in results}) == 1
    with db_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(db_tables["tasks"])) == 1
        assert (
            connection.scalar(select(func.count()).select_from(db_tables["source_receipts"])) == 1
        )


def test_source_identity_is_database_enforced(make_task, db_engine, db_tables):
    make_task(message_id=100)
    receipts = db_tables["source_receipts"]
    with db_engine.connect() as connection:
        row = dict(connection.execute(select(receipts)).mappings().one())
    row["id"] = uuid4()
    # Remove the separate task uniqueness collision so this proves source identity itself.
    row["task_id"] = None
    with pytest.raises(IntegrityError) as failure, db_engine.begin() as connection:
        connection.execute(receipts.insert().values(**row))
    assert failure.value.orig.diag.constraint_name == "uq_receipt_telegram_message"


@pytest.mark.parametrize("missing", ["bot_identity", "chat_id", "message_id"])
def test_database_rejects_incomplete_source_identity(make_task, db_engine, db_tables, missing):
    make_task(message_id=100)
    receipts = db_tables["source_receipts"]
    with db_engine.connect() as connection:
        row = dict(connection.execute(select(receipts)).mappings().one())
    row.update(id=uuid4(), task_id=None)
    row[missing] = None
    with pytest.raises(IntegrityError) as failure, db_engine.begin() as connection:
        connection.execute(receipts.insert().values(**row))
    assert failure.value.orig.diag.constraint_name == "ck_receipt_source_identity"


def test_database_rejects_invalid_task_status(make_task, db_engine, db_tables):
    make_task()
    with pytest.raises(IntegrityError), db_engine.begin() as connection:
        connection.execute(update(db_tables["tasks"]).values(status="invalid-status"))


def test_pagination_has_no_omissions_and_cursor_cannot_change_owner(
    api, bot_headers, users, make_task
):
    expected = {make_task(content=f"Task {number}")["id"] for number in range(12)}
    seen = []
    cursor = None
    first_cursor = None
    for _ in range(4):
        params = {"telegram_user_id": users[0]["telegram_user_id"], "limit": 5}
        if cursor:
            params["cursor"] = cursor
        result = api.get("/internal/bot/tasks", headers=bot_headers, params=params)
        assert result.status_code == 200
        data = result.json()
        assert len(data["items"]) <= 5
        seen.extend(task["id"] for task in data["items"])
        cursor = data["next_cursor"]
        first_cursor = first_cursor or cursor
        if not cursor:
            break
    assert len(seen) == 12 and set(seen) == expected
    foreign = api.get(
        "/internal/bot/tasks",
        headers=bot_headers,
        params={"telegram_user_id": users[1]["telegram_user_id"], "cursor": first_cursor},
    )
    assert foreign.status_code in (400, 403, 422)


@pytest.mark.parametrize(
    "query", [{"limit": 0}, {"limit": 101}, {"limit": "bad"}, {"cursor": "forged"}]
)
def test_pagination_validates_input(api, bot_headers, users, query):
    result = api.get(
        "/internal/bot/tasks",
        headers=bot_headers,
        params={"telegram_user_id": users[0]["telegram_user_id"], **query},
    )
    assert result.status_code in (400, 422)


def test_browser_owner_is_session_bound(browser, dashboard_create):
    alice, bob = browser(0), browser(1)
    created = dashboard_create(alice, "Alice dashboard content")
    assert created.status_code in (200, 201)
    task = created.json()
    assert bob.get("/api/tasks").json()["items"] == []
    path = f"/api/tasks/{task['id']}"
    for method, kwargs in (
        ("get", {}),
        ("patch", {"json": {"status": "completed"}}),
        ("delete", {}),
    ):
        result = getattr(bob, method)(path, headers={"If-Match": str(task["version"])}, **kwargs)
        assert result.status_code == 404
    forged = bob.post(
        "/api/tasks",
        headers={"Idempotency-Key": str(uuid4())},
        json={"content": "forged", "owner_id": task["owner_id"]},
    )
    assert forged.status_code == 422


def test_browser_creation_idempotency_and_deleted_receipt(browser, dashboard_create):
    client = browser()
    key = str(uuid4())
    first = dashboard_create(client, key=key)
    duplicate = dashboard_create(client, key=key)
    assert first.status_code in (200, 201) and duplicate.status_code in (200, 201)
    assert first.json()["id"] == duplicate.json()["id"]
    assert dashboard_create(client, content="Changed input", key=key).status_code == 409
    task = first.json()
    deleted = client.delete(f"/api/tasks/{task['id']}", headers={"If-Match": str(task["version"])})
    assert deleted.status_code in (200, 204)
    assert dashboard_create(client, key=key).status_code == 410


def test_concurrent_dashboard_retries_create_exactly_one_task(browser, app, db_engine, db_tables):
    client = browser()
    key = str(uuid4())
    cookie = client.cookies.get("omni_session")
    csrf = client.headers["X-CSRF-Token"]
    barrier = Barrier(4)

    def submit(_):
        with TestClient(app, base_url=ORIGIN) as retry:
            retry.cookies.set("omni_session", cookie)
            barrier.wait(timeout=15)
            return retry.post(
                "/api/tasks",
                json={"content": "Retried dashboard submission"},
                headers={"Origin": ORIGIN, "X-CSRF-Token": csrf, "Idempotency-Key": key},
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(submit, range(4)))
    assert all(response.status_code in (200, 201) for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1
    with db_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(db_tables["tasks"])) == 1


def test_concurrent_status_changes_have_one_winner(app, bot_headers, users, make_task):
    task = make_task()
    barrier = Barrier(2)

    def change(status):
        with TestClient(app, base_url=ORIGIN) as client:
            barrier.wait(timeout=15)
            return client.patch(
                f"/internal/bot/tasks/{task['id']}",
                params={"telegram_user_id": users[0]["telegram_user_id"]},
                headers={**bot_headers, "If-Match": str(task["version"])},
                json={"status": status},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(change, ("in_progress", "completed")))
    assert sorted(response.status_code for response in results) == [200, 409]


def test_task_and_revision_survive_a_new_api_instance(api, settings, bot_headers, users, make_task):
    from app.main import create_app

    original = make_task(content="Persisted across API instance")
    before = api.get(
        "/internal/bot/tasks",
        headers=bot_headers,
        params={"telegram_user_id": users[0]["telegram_user_id"]},
    ).json()
    with TestClient(create_app(settings), base_url=ORIGIN) as restarted:
        after = restarted.get(
            "/internal/bot/tasks",
            headers=bot_headers,
            params={"telegram_user_id": users[0]["telegram_user_id"]},
        ).json()
    assert after["revision"] == before["revision"]
    assert after["items"] == [original]


def test_processing_requests_are_separate_idempotent_and_owner_scoped(api, bot_headers, users):
    body = {
        "telegram_user_id": users[0]["telegram_user_id"],
        "message_id": 123,
        "file_id": "synthetic-voice-file",
        "duration_seconds": 1,
    }
    response = api.post("/internal/bot/processing-requests", headers=bot_headers, json=body)
    assert response.status_code in (200, 201, 202)
    request = response.json()
    assert request["state"] == "queued"
    assert request.get("task_id") is None
    replay = api.post("/internal/bot/processing-requests", headers=bot_headers, json=body)
    assert replay.status_code in (200, 201, 202)
    assert replay.json()["id"] == request["id"]
    altered = api.post(
        "/internal/bot/processing-requests",
        headers=bot_headers,
        json={**body, "file_id": "changed"},
    )
    assert altered.status_code == 409
    foreign = api.get(
        f"/internal/bot/processing-requests/{request['id']}",
        headers=bot_headers,
        params={"telegram_user_id": users[1]["telegram_user_id"]},
    )
    assert foreign.status_code == 404
    tasks = api.get(
        "/internal/bot/tasks",
        headers=bot_headers,
        params={"telegram_user_id": users[0]["telegram_user_id"]},
    )
    assert tasks.json()["items"] == []
