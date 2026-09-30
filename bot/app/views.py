"""Plain-text Telegram presentation, without task business rules or persistence."""

from dataclasses import dataclass
from datetime import UTC, datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.app.navigation import Action, Navigation, Page

STATUSES = {
    "pending": ("⏳", "Pending"),
    "in_progress": ("🔄", "In Progress"),
    "completed": ("✅", "Completed"),
}
MESSAGE_LIMIT = 4096


def units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def preview(text: str, limit: int) -> str:
    if units(text) <= limit:
        return text
    prefix = text.encode("utf-16-le")[: (limit - 1) * 2].decode("utf-16-le", errors="ignore")
    return prefix + "…"


def status_label(status: str) -> str:
    icon, label = STATUSES[status]
    return f"{icon} {label}"


@dataclass
class View:
    text: str
    markup: InlineKeyboardMarkup | None = None


class Views:
    def __init__(self, navigation: Navigation, actor: int, chat: int):
        self.navigation = navigation
        self.actor = actor
        self.chat = chat

    def button(self, text: str, action: Action) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=text,
            callback_data=self.navigation.add(self.actor, self.chat, action),
            style="danger" if action.kind in {"delete", "confirm"} else None,
        )

    def status_buttons(self, task: dict, page: Page) -> list[list[InlineKeyboardButton]]:
        return [
            [
                self.button(
                    status_label(status),
                    Action("status", page, task["id"], task["version"], status),
                )
                for status in STATUSES
            ]
        ]

    def confirmation(self, task: dict) -> View:
        page = Page()
        rows = self.status_buttons(task, page)
        rows.append(
            [self.button("Delete task", Action("delete", page, task["id"], task["version"]))]
        )
        return View(
            f"Task saved\n\n{task['title']}\n{status_label(task['status'])}",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    def task(self, task: dict, page: Page, notice: str = "") -> View:
        created = datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))
        timestamp = created.astimezone(UTC).strftime("%d %b %Y, %H:%M UTC")
        heading = (
            notice + "\n\n" if notice else ""
        ) + f"{task['title']}\n{status_label(task['status'])}\nCreated {timestamp}\n\n"
        content = task["content"]
        suffix = ""
        if units(heading + content) > MESSAGE_LIMIT:
            suffix = (
                "\n\nPreview shortened for Telegram. The complete text is saved. "
                "Use Read full text to read every page."
            )
            content = preview(content, MESSAGE_LIMIT - units(heading + suffix))
        rows = self.status_buttons(task, page)
        rows.append([self.button("Delete", Action("delete", page, task["id"], task["version"]))])
        if suffix:
            rows.append([self.button("Read full text", Action("full", page, task["id"]))])
            rows.append([self.button("Open Dashboard", Action("dashboard", page, task["id"]))])
        rows.append([self.button("Back to tasks", Action("list", page))])
        return View(heading + content + suffix, InlineKeyboardMarkup(inline_keyboard=rows))

    def full_text(self, task: dict, page: Page, text_page: int) -> View:
        # Slice code points, never UTF-16 bytes: every original character appears once.
        chunks, chunk, length = [], [], 0
        for character in task["content"]:
            width = 2 if ord(character) > 0xFFFF else 1
            if length + width > 3500:
                chunks.append("".join(chunk))
                chunk, length = [], 0
            chunk.append(character)
            length += width
        chunks.append("".join(chunk))
        index = min(max(text_page, 0), len(chunks) - 1)
        navigation = []
        for label, target in (("← Previous text", index - 1), ("Next text →", index + 1)):
            if 0 <= target < len(chunks):
                navigation.append(
                    self.button(
                        label,
                        Action("full", page, task["id"], text_page=target),
                    )
                )
        rows = [navigation] if navigation else []
        rows.append([self.button("Back to task", Action("open", page, task["id"]))])
        return View(
            f"Full text · Page {index + 1}/{len(chunks)}\n\n{chunks[index]}",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    def tasks(self, result: dict, page: Page, notice: str = "") -> View:
        text = notice + "\n\n" if notice else ""
        if not result["items"]:
            text += "No tasks yet. Send a text message to create your first task."
        else:
            text += f"Your tasks · Page {page.number}\nTap a task to open it."
        rows = [
            [
                self.button(
                    f"{STATUSES[task['status']][0]} {preview(task['title'], 70)}",
                    Action("open", page, task["id"]),
                )
            ]
            for task in result["items"]
        ]
        pagination = []
        if page.previous:
            pagination.append(self.button("← Previous", Action("list", page.previous)))
        if result["next_cursor"]:
            following = Page(result["next_cursor"], page, page.number + 1)
            pagination.append(self.button("Next →", Action("list", following)))
        if pagination:
            rows.append(pagination)
        return View(text, InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)

    def delete(self, task: dict, page: Page) -> View:
        return View(
            f"Delete this task?\n\n{task['title']}\n\nThis cannot be undone.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        self.button(
                            "Confirm delete", Action("confirm", page, task["id"], task["version"])
                        ),
                        self.button("Cancel", Action("open", page, task["id"])),
                    ]
                ]
            ),
        )

    def dashboard(self, url: str, action: Action | None = None) -> View:
        rows = [[InlineKeyboardButton(text="Open Dashboard", url=url)]]
        if action and action.task_id:
            rows.append([self.button("Back to task", Action("open", action.page, action.task_id))])
        else:
            rows.append([self.button("Back to tasks", Action("list"))])
        return View(
            "Your private dashboard link is single-use and short-lived. "
            "Request /profile again if it expires.\n\n"
            "Open your board to create tasks, read full content, and organize their status.",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    def unavailable(self, text: str) -> View:
        return View(
            text,
            InlineKeyboardMarkup(inline_keyboard=[[self.button("Back to tasks", Action("list"))]]),
        )
