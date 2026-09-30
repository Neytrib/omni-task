from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TelegramID = Annotated[int, Field(gt=0, le=9223372036854775807, strict=True)]
TaskStatus = Literal["pending", "in_progress", "completed"]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class UserCreate(InputModel):
    telegram_user_id: TelegramID
    private_chat_id: TelegramID

    @model_validator(mode="after")
    def private_identity(self):
        if self.private_chat_id != self.telegram_user_id:
            raise ValueError("Only the sender's private chat is supported")
        return self


class TelegramActor(InputModel):
    telegram_user_id: TelegramID


class TaskCreate(InputModel):
    content: str = Field(min_length=1, max_length=50000, strict=True)

    @field_validator("content")
    @classmethod
    def preserve_valid_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Content must not be whitespace-only")
        if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("Content must contain valid Unicode without NUL")
        return value


class BotTaskCreate(TaskCreate, TelegramActor):
    message_id: TelegramID


class ProcessingCreate(TelegramActor):
    message_id: TelegramID
    file_id: str = Field(min_length=1, max_length=1024, strict=True, pattern=r"^[A-Za-z0-9_-]+$")
    acknowledgement_message_id: TelegramID | None = None
    duration_seconds: int = Field(ge=1, le=600, strict=True)
    file_size: int | None = Field(default=None, ge=1, le=19_000_000, strict=True)


class VoiceLease(InputModel):
    lease_token: UUID


class NotificationCheckpoint(InputModel):
    lease_token: UUID
    message_id: TelegramID


class StatusChange(InputModel):
    status: TaskStatus


class Exchange(InputModel):
    token: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")


class PageQuery(InputModel):
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=1024)


class BotPageQuery(PageQuery):
    telegram_user_id: int = Field(gt=0, le=9223372036854775807)


class BotActorQuery(InputModel):
    telegram_user_id: int = Field(gt=0, le=9223372036854775807)


class OutputModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserView(OutputModel):
    id: UUID
    telegram_user_id: int
    private_chat_id: int
    created_at: datetime


class TaskView(OutputModel):
    id: UUID
    owner_id: UUID
    title: str
    content: str
    status: TaskStatus
    source: str
    version: int
    created_at: datetime
    updated_at: datetime


class TaskPage(OutputModel):
    items: list[TaskView]
    next_cursor: str | None
    revision: int


class ProcessingView(OutputModel):
    id: UUID
    owner_id: UUID
    source_receipt_id: UUID
    state: str
    attempts: int
    error_code: str | None
    acknowledgement_message_id: int | None
    duration_seconds: int
    file_size: int | None
    task_id: UUID | None
    deleted: bool = False
    notification_state: str | None = None
    provider_name: str
    created_at: datetime
    updated_at: datetime
