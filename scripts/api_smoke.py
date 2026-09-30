"""Synthetic two-user walkthrough against the running API; never print credentials or bodies."""

import json
import os
import secrets
from contextlib import ExitStack
from uuid import uuid4

import httpx


def checked(response: httpx.Response, expected: int) -> dict:
    assert response.status_code == expected, (
        f"{response.request.method} {response.request.url.path}: "
        f"expected {expected}, received {response.status_code}"
    )
    return response.json() if response.content else {}


def main() -> None:
    if os.environ.get("APP_ENV") != "development":
        raise SystemExit("This synthetic walkthrough is enabled only in development.")
    origin = os.environ["DASHBOARD_ORIGIN"]
    identifiers = [secrets.randbits(62) + 1, secrets.randbits(62) + 1]
    with ExitStack() as stack:
        bot = stack.enter_context(
            httpx.Client(
                base_url="http://127.0.0.1:8000",
                timeout=10,
                headers={"Authorization": "Bearer " + os.environ["BOT_API_KEY"]},
            )
        )
        browser = [
            stack.enter_context(
                httpx.Client(
                    base_url="http://127.0.0.1:8000",
                    timeout=10,
                    headers={"Origin": origin},
                )
            )
            for _ in identifiers
        ]
        tasks = []
        for index, identity in enumerate(identifiers):
            checked(
                bot.post(
                    "/internal/bot/users",
                    json={
                        "telegram_user_id": identity,
                        "private_chat_id": identity,
                    },
                ),
                200,
            )
            content = f"  Synthetic foundation check {index}\nPreserve this line. 🧪  "
            payload = {"telegram_user_id": identity, "message_id": 1, "content": content}
            task = checked(bot.post("/internal/bot/tasks", json=payload), 201)
            assert task["content"] == content
            assert checked(bot.post("/internal/bot/tasks", json=payload), 200)["id"] == task["id"]
            tasks.append((task, payload))
            link = checked(
                bot.post(
                    "/internal/bot/login-links",
                    json={
                        "telegram_user_id": identity,
                    },
                ),
                200,
            )
            checked(browser[index].get("/api/auth/exchange"), 405)
            checked(browser[index].post("/api/auth/exchange", json={"token": link["token"]}), 200)
            checked(browser[index].post("/api/auth/exchange", json={"token": link["token"]}), 401)
            session = checked(browser[index].get("/api/session"), 200)
            browser[index].headers["X-CSRF-Token"] = session["csrf_token"]
            own_page = checked(browser[index].get("/api/tasks"), 200)
            assert [item["id"] for item in own_page["items"]] == [task["id"]]
        first = tasks[0][0]
        checked(browser[1].get(f"/api/tasks/{first['id']}"), 404)
        checked(
            browser[1].patch(
                f"/api/tasks/{first['id']}",
                headers={"If-Match": str(first["version"])},
                json={"status": "completed"},
            ),
            404,
        )
        checked(
            browser[1].delete(
                f"/api/tasks/{first['id']}", headers={"If-Match": str(first["version"])}
            ),
            404,
        )
        checked(
            browser[0].patch(
                f"/api/tasks/{first['id']}",
                headers={"If-Match": str(first["version"])},
                json={"status": "invalid"},
            ),
            422,
        )
        changed = checked(
            browser[0].patch(
                f"/api/tasks/{first['id']}",
                headers={"If-Match": str(first["version"])},
                json={"status": "in_progress"},
            ),
            200,
        )
        tasks[0] = (changed, tasks[0][1])
        checked(
            browser[0].post(
                "/api/tasks",
                headers={"Idempotency-Key": str(uuid4())},
                json={"content": "Synthetic forged owner", "owner_id": tasks[1][0]["owner_id"]},
            ),
            422,
        )
        for index, (task, payload) in enumerate(tasks):
            checked(
                browser[index].delete(
                    f"/api/tasks/{task['id']}", headers={"If-Match": str(task["version"])}
                ),
                204,
            )
            checked(bot.post("/internal/bot/tasks", json=payload), 410)
            checked(browser[index].post("/api/auth/logout"), 204)
            checked(browser[index].get("/api/tasks"), 401)
    print(
        json.dumps(
            {
                "status": "passed",
                "users": 2,
                "checks": [
                    "exact content",
                    "duplicate source",
                    "single-use login",
                    "owner isolation",
                    "forged owner",
                    "status validation",
                    "status change",
                    "delete and no resurrection",
                    "logout",
                ],
            }
        )
    )
    print("Synthetic tasks deleted; synthetic users and minimal source/session receipts remain.")


if __name__ == "__main__":
    main()
