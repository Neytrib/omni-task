import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError
from app.models import BrowserSession, LoginLink, User
from app.services import lock_user, utcnow

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def credential_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_login_link(db: Session, user: User, settings: Settings) -> dict:
    lock_user(db, user.id)
    now = utcnow()
    db.execute(
        update(LoginLink)
        .where(
            LoginLink.owner_id == user.id,
            LoginLink.consumed_at.is_(None),
            LoginLink.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    token = secrets.token_urlsafe(32)
    expires_at = now + timedelta(seconds=settings.login_ttl_seconds)
    db.add(LoginLink(owner_id=user.id, token_hash=credential_hash(token), expires_at=expires_at))
    db.flush()
    return {
        "token": token,
        "url": f"{settings.dashboard_origin}/login#token={token}",
        "expires_at": expires_at,
    }


def exchange_link(db: Session, token: str, settings: Settings) -> tuple[str, BrowserSession]:
    # Check time after obtaining the lock: a request may wait past link expiry.
    link = db.scalar(
        select(LoginLink)
        .where(LoginLink.token_hash == credential_hash(token))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = utcnow()
    if (
        link is None
        or link.consumed_at is not None
        or link.revoked_at is not None
        or link.expires_at <= now
    ):
        raise AppError(
            401, "invalid_login_link", "This login link is invalid or expired. Request a new link."
        )
    link.consumed_at = now
    session_token = secrets.token_urlsafe(32)
    session = BrowserSession(
        owner_id=link.owner_id,
        token_hash=credential_hash(session_token),
        csrf_secret=secrets.token_hex(32),
        expires_at=now + timedelta(seconds=settings.session_ttl_seconds),
    )
    db.add(session)
    db.flush()
    return session_token, session


@dataclass
class AuthenticatedSession:
    user: User
    session: BrowserSession
    # Never log this object; repr excludes the raw cookie credential.
    raw_token: str

    def __repr__(self) -> str:
        return f"AuthenticatedSession(user_id={self.user.id})"

    @property
    def csrf_token(self) -> str:
        return hmac.new(
            self.session.csrf_secret.encode(), b"csrf:" + self.raw_token.encode(), hashlib.sha256
        ).hexdigest()


def authenticate_session(db: Session, token: str | None) -> AuthenticatedSession:
    if token is None or not TOKEN_PATTERN.fullmatch(token):
        raise AppError(401, "unauthenticated", "Sign in using a fresh link from the bot.")
    session = db.scalar(
        select(BrowserSession).where(
            BrowserSession.token_hash == credential_hash(token),
            BrowserSession.revoked_at.is_(None),
            BrowserSession.expires_at > utcnow(),
        )
    )
    if session is None:
        raise AppError(401, "unauthenticated", "Sign in using a fresh link from the bot.")
    user = db.get(User, session.owner_id)
    if user is None:
        raise AppError(401, "unauthenticated", "Sign in using a fresh link from the bot.")
    return AuthenticatedSession(user=user, session=session, raw_token=token)
