"""Browser-only cross-origin transport and consistent session-cookie attributes."""

from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from app.config import Settings


class BrowserCORSMiddleware(CORSMiddleware):
    """Keep service-only endpoints outside the browser CORS surface."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and (path == "/api" or path.startswith("/api/")):
            await super().__call__(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def session_cookie(
    response: Response, settings: Settings, token: str = "", *, clear: bool = False
) -> None:
    """Set or expire the same host-only cookie, including its partition key.

    Python 3.12's cookie serializer does not yet understand Partitioned. Let
    Starlette serialize all variable values and append only this fixed flag to
    the new header; never change stdlib cookie internals or splice user input.
    """
    response.set_cookie(
        key=settings.cookie_name,
        value="" if clear else token,
        max_age=0 if clear else settings.session_ttl_seconds,
        expires=0 if clear else None,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/",
    )
    if settings.cookie_partitioned:
        name, value = response.raw_headers[-1]
        response.raw_headers[-1] = (name, value + b"; Partitioned")
