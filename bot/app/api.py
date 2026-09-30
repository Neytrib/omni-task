"""Authenticated HTTP transport; the bot never imports application persistence or queues."""

import re
from uuid import UUID

import httpx

from omni_logging import current_correlation_id


class APIError(Exception):
    """A safe API outcome. Never retain response bodies, content, URLs, or credentials."""

    def __init__(self, status: int, code: str = "api_error"):
        self.status = status
        self.code = code if re.fullmatch(r"[a-z_]{1,64}", code) else "api_error"
        super().__init__(f"Internal API request failed (HTTP {status})")


class APIUnavailable(APIError):
    """The outcome may be ambiguous; retry the original message identity unchanged."""

    def __init__(self, status: int = 503, code: str = "api_unavailable"):
        super().__init__(status, code)


def task_path(task_id: str) -> str:
    try:
        identifier = UUID(str(task_id))
    except (ValueError, TypeError, AttributeError):
        raise APIError(422, "invalid_task_id") from None
    return f"/internal/bot/tasks/{identifier}"


class APIClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = httpx.Headers(kwargs.pop("headers", {}))
        headers["X-Correlation-ID"] = current_correlation_id()
        try:
            response = await self.client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError:
            raise APIUnavailable() from None
        if response.status_code == 429 or response.status_code >= 500:
            raise APIUnavailable(response.status_code)
        if not response.is_success:
            code = "api_error"
            try:
                document = response.json()
                if isinstance(document, dict) and isinstance(document.get("error"), dict):
                    candidate = document["error"].get("code")
                    if isinstance(candidate, str):
                        code = candidate
            except ValueError:
                pass
            raise APIError(response.status_code, code)
        if response.status_code == 204:
            return {}
        try:
            result = response.json()
        except ValueError:
            raise APIUnavailable(code="invalid_api_response") from None
        if not isinstance(result, dict):
            raise APIUnavailable(code="invalid_api_response")
        return result

    async def health(self) -> dict:
        return await self._request("GET", "/internal/health")

    async def ensure_user(self, actor: int, chat: int) -> dict:
        return await self._request(
            "POST",
            "/internal/bot/users",
            json={"telegram_user_id": actor, "private_chat_id": chat},
        )

    async def create_task(self, actor: int, message_id: int, content: str) -> dict:
        return await self._request(
            "POST",
            "/internal/bot/tasks",
            json={"telegram_user_id": actor, "message_id": message_id, "content": content},
        )

    async def list_tasks(self, actor: int, cursor: str | None = None, limit: int = 5) -> dict:
        params: dict[str, str | int] = {"telegram_user_id": actor, "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request("GET", "/internal/bot/tasks", params=params)

    async def get_task(self, actor: int, task_id: str) -> dict:
        return await self._request("GET", task_path(task_id), params={"telegram_user_id": actor})

    async def change_status(self, actor: int, task_id: str, status: str, version: int) -> dict:
        return await self._request(
            "PATCH",
            task_path(task_id),
            params={"telegram_user_id": actor},
            json={"status": status},
            headers={"If-Match": str(version)},
        )

    async def delete_task(self, actor: int, task_id: str, version: int) -> None:
        await self._request(
            "DELETE",
            task_path(task_id),
            params={"telegram_user_id": actor},
            headers={"If-Match": str(version)},
        )

    async def login_link(self, actor: int) -> dict:
        return await self._request(
            "POST",
            "/internal/bot/login-links",
            json={"telegram_user_id": actor},
        )

    async def create_processing_request(
        self,
        actor: int,
        message_id: int,
        file_id: str,
        duration_seconds: int,
        file_size: int | None,
        acknowledgement_message_id: int | None,
    ) -> dict:
        return await self._request(
            "POST",
            "/internal/bot/processing-requests",
            json={
                "telegram_user_id": actor,
                "message_id": message_id,
                "file_id": file_id,
                "duration_seconds": duration_seconds,
                "file_size": file_size,
                "acknowledgement_message_id": acknowledgement_message_id,
            },
        )

    async def voice_context(self, request_id: str, lease_token: str, kind: str) -> dict:
        if kind not in {"download", "notification"}:
            raise ValueError("Unsupported voice context")
        identifier, lease = UUID(str(request_id)), UUID(str(lease_token))
        return await self._request(
            "POST",
            f"/internal/voice/{identifier}/{kind}-context",
            json={"lease_token": str(lease)},
        )

    async def notification_checkpoint(
        self,
        request_id: str,
        lease_token: str,
        message_id: int,
    ) -> dict:
        identifier, lease = UUID(str(request_id)), UUID(str(lease_token))
        return await self._request(
            "POST",
            f"/internal/voice/{identifier}/notification-checkpoint",
            json={"lease_token": str(lease), "message_id": message_id},
        )
