"""Telegram command service for Vinted Agent."""

from __future__ import annotations

import logging
import signal
import subprocess
import threading
from typing import Any, Final

import requests

from config import BOT_TOKEN, CHAT_ID, DATABASE_PATH
from database import Database
from hourly_stats import build_message, get_statistics
from runtime_state import is_paused, set_paused
from search_manager import SearchManager


LOGGER = logging.getLogger(__name__)

TELEGRAM_API_URL: Final[str] = "https://api.telegram.org"
POLL_TIMEOUT_SECONDS: Final[int] = 30
REQUEST_TIMEOUT: Final[tuple[int, int]] = (10, 40)
MAX_MESSAGE_LENGTH: Final[int] = 4_096
RECENT_ITEM_LIMIT: Final[int] = 10

STOP_EVENT = threading.Event()

BOT_COMMANDS: Final[list[dict[str, str]]] = [
    {"command": "stats", "description": "Current market statistics"},
    {"command": "report", "description": "Full market report now"},
    {"command": "sold", "description": "Latest sold/unavailable listings"},
    {"command": "new", "description": "Latest discovered listings"},
    {"command": "searches", "description": "Show configured searches"},
    {"command": "health", "description": "Agent and database health"},
    {"command": "pause", "description": "Pause catalogue monitoring"},
    {"command": "resume", "description": "Resume catalogue monitoring"},
    {"command": "status", "description": "Show monitoring state"},
    {"command": "schedule", "description": "Show report schedule"},
    {"command": "help", "description": "Show command help"},
]


class TelegramCommandBot:
    """Long-poll Telegram and handle commands from the configured chat."""

    def __init__(
        self,
        bot_token: str = BOT_TOKEN,
        chat_id: str | int = CHAT_ID,
    ) -> None:
        self.bot_token = str(bot_token).strip()
        self.chat_id = str(chat_id).strip()

        if not self.bot_token:
            raise ValueError(
                "Telegram BOT_TOKEN is not configured"
            )

        if not self.chat_id:
            raise ValueError(
                "Telegram CHAT_ID is not configured"
            )

        self.api = (
            f"{TELEGRAM_API_URL}/bot{self.bot_token}"
        )
        self.session = requests.Session()
        self.offset: int | None = None

    def run_forever(self) -> None:
        """Poll Telegram until the process receives a shutdown signal."""

        self._set_commands()
        self._discard_backlog()

        LOGGER.info(
            "Telegram command service started"
        )

        while not STOP_EVENT.is_set():
            try:
                updates = self._get_updates()

                for update in updates:
                    self._consume_update(
                        update
                    )

            except requests.RequestException as exc:
                LOGGER.warning(
                    "Telegram polling request failed: %s",
                    exc,
                )
                STOP_EVENT.wait(5)

            except Exception:
                LOGGER.exception(
                    "Unexpected Telegram command loop failure"
                )
                STOP_EVENT.wait(5)

    def close(self) -> None:
        self.session.close()

    def handle_command(
        self,
        command_text: str,
    ) -> str:
        """Return the response text for one command."""

        command = self._normalise_command(
            command_text
        )

        if command in {
            "/start",
            "/help",
        }:
            return self._help_message()

        if command == "/stats":
            return self._stats_message()

        if command == "/report":
            return build_message(
                get_statistics()
            )

        if command == "/sold":
            return self._sold_message()

        if command == "/new":
            return self._new_message()

        if command == "/searches":
            return self._searches_message()

        if command == "/health":
            return self._health_message()

        if command == "/pause":
            set_paused(True)
            return (
                "⏸ Catalogue monitoring paused.\n\n"
                "Telegram retries, status checks and the scheduled "
                "20:00 report remain available."
            )

        if command == "/resume":
            set_paused(False)
            return (
                "▶️ Catalogue monitoring resumed.\n\n"
                "Configured searches will run on the next agent cycle."
            )

        if command == "/status":
            return self._status_message()

        if command == "/schedule":
            return (
                "🕗 Scheduled report\n\n"
                "Daily at 20:00 Europe/London.\n"
                "The schedule is DST-aware.\n\n"
                "Use /report for the same report on demand."
            )

        return (
            "Unknown command.\n\n"
            "Use /help to see available commands."
        )

    def _consume_update(
        self,
        update: dict[str, Any],
    ) -> None:
        update_id = update.get(
            "update_id"
        )

        if isinstance(
            update_id,
            int,
        ):
            self.offset = update_id + 1

        message = update.get(
            "message"
        )

        if not isinstance(
            message,
            dict,
        ):
            return

        chat = message.get(
            "chat"
        )

        if not isinstance(
            chat,
            dict,
        ):
            return

        incoming_chat_id = str(
            chat.get(
                "id",
                "",
            )
        ).strip()

        if incoming_chat_id != self.chat_id:
            LOGGER.warning(
                "Ignored Telegram command from unauthorized chat %s",
                incoming_chat_id or "unknown",
            )
            return

        text = message.get(
            "text"
        )

        if not isinstance(
            text,
            str,
        ):
            return

        if not text.strip().startswith(
            "/"
        ):
            return

        try:
            response = self.handle_command(
                text
            )

        except Exception:
            LOGGER.exception(
                "Telegram command failed: %s",
                text,
            )

            response = (
                "⚠️ Command failed. Check the Beelink service logs."
            )

        self._send_message(
            response
        )

    def _get_updates(self) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": POLL_TIMEOUT_SECONDS,
            "allowed_updates": [
                "message"
            ],
        }

        if self.offset is not None:
            payload[
                "offset"
            ] = self.offset

        response = self.session.post(
            f"{self.api}/getUpdates",
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        result = response.json()

        if not result.get(
            "ok",
            False,
        ):
            raise RuntimeError(
                "Telegram rejected getUpdates"
            )

        updates = result.get(
            "result",
            [],
        )

        if not isinstance(
            updates,
            list,
        ):
            return []

        return [
            update
            for update in updates
            if isinstance(
                update,
                dict,
            )
        ]

    def _discard_backlog(self) -> None:
        """Skip old commands that accumulated before this service existed."""

        try:
            response = self.session.post(
                f"{self.api}/getUpdates",
                json={
                    "timeout": 0,
                    "limit": 100,
                    "allowed_updates": [
                        "message"
                    ],
                },
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()

            result = response.json()
            updates = result.get(
                "result",
                [],
            )

            if updates:
                final = updates[-1]
                update_id = final.get(
                    "update_id"
                )

                if isinstance(
                    update_id,
                    int,
                ):
                    self.offset = update_id + 1

        except Exception:
            LOGGER.exception(
                "Unable to discard Telegram command backlog"
            )

    def _set_commands(self) -> None:
        try:
            response = self.session.post(
                f"{self.api}/setMyCommands",
                json={
                    "commands": BOT_COMMANDS,
                },
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()

        except requests.RequestException:
            LOGGER.exception(
                "Unable to set Telegram bot command menu"
            )

    def _send_message(
        self,
        message: str,
    ) -> None:
        response = self.session.post(
            f"{self.api}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": self._truncate(
                    message,
                    MAX_MESSAGE_LENGTH,
                ),
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        payload = response.json()

        if not payload.get(
            "ok",
            False,
        ):
            raise RuntimeError(
                "Telegram rejected sendMessage"
            )

    @staticmethod
    def _normalise_command(
        text: str,
    ) -> str:
        first = (
            text.strip()
            .split(maxsplit=1)[0]
            .casefold()
        )

        if "@" in first:
            first = first.split(
                "@",
                maxsplit=1,
            )[0]

        return first

    @staticmethod
    def _help_message() -> str:
        return (
            "🤖 Vinted Agent commands\n\n"
            "/stats — current market statistics\n"
            "/report — full report now\n"
            "/sold — latest sold/unavailable listings\n"
            "/new — latest discovered listings\n"
            "/searches — configured searches and limits\n"
            "/health — service and database health\n"
            "/pause — pause catalogue searches\n"
            "/resume — resume catalogue searches\n"
            "/status — monitoring state\n"
            "/schedule — scheduled report time\n"
            "/help — this message"
        )

    @staticmethod
    def _stats_message() -> str:
        stats = get_statistics()

        average_price = (
            "n/a"
            if stats[
                "average_sold_price"
            ] is None
            else (
                f"£{stats['average_sold_price']:.2f}"
            )
        )

        return (
            "📊 Vinted statistics\n\n"
            f"Tracked: {stats['total']}\n"
            f"Active: {stats['active']}\n"
            f"Sold/unavailable: {stats['sold']}\n"
            f"Unknown: {stats['unknown']}\n"
            f"Sold today: {stats['sold_today']}\n"
            f"Sold last 7 days: {stats['sold_last_7_days']}\n"
            f"Sell-through: {stats['sell_through_rate']:.1f}%\n"
            f"Average last asking price: {average_price}"
        )

    @staticmethod
    def _sold_message() -> str:
        database = Database(
            DATABASE_PATH
        )

        try:
            rows = database.listing_records(
                sold_only=True,
                limit=RECENT_ITEM_LIMIT,
            )
        finally:
            database.close()

        if not rows:
            return (
                "🔴 No sold/unavailable listings recorded yet."
            )

        lines = [
            "🔴 Latest sold/unavailable listings",
        ]

        for row in rows:
            price = float(
                row["price"]
                or 0
            )
            lines.extend(
                [
                    "",
                    f"• {row['name']}",
                    f"  £{price:.2f} | {row['listing_status']}",
                    f"  {row['sold_at'] or 'time unknown'}",
                    f"  {row['url']}",
                ]
            )

        return "\n".join(
            lines
        )

    @staticmethod
    def _new_message() -> str:
        database = Database(
            DATABASE_PATH
        )

        try:
            rows = database.recent_listings(
                RECENT_ITEM_LIMIT
            )
        finally:
            database.close()

        if not rows:
            return (
                "🆕 No listings recorded yet."
            )

        lines = [
            "🆕 Latest discovered listings",
        ]

        for row in rows:
            price = float(
                row["current_price"]
                or 0
            )
            lines.extend(
                [
                    "",
                    f"• {row['title']}",
                    f"  £{price:.2f} | {row['listing_status']}",
                    f"  {row['first_seen']}",
                    f"  {row['url']}",
                ]
            )

        return "\n".join(
            lines
        )

    @staticmethod
    def _searches_message() -> str:
        # /searches should still show configuration when monitoring is paused,
        # so read the manager's file directly rather than calling load().
        manager = SearchManager()
        data = manager._read_file()

        if not isinstance(
            data,
            list,
        ):
            return (
                "No valid searches configured."
            )

        lines = [
            "🔎 Configured searches",
        ]

        for item in data:
            if not isinstance(
                item,
                dict,
            ):
                continue

            name = str(
                item.get(
                    "name",
                    "Unknown",
                )
            )
            maximum = item.get(
                "max_price"
            )
            max_text = (
                "no limit"
                if maximum is None
                else f"£{float(maximum):.2f}"
            )

            sizes = item.get(
                "sizes",
                [],
            )
            conditions = item.get(
                "conditions",
                [],
            )

            size_text = (
                ", ".join(
                    str(value)
                    for value in sizes
                )
                if sizes
                else "any"
            )
            condition_text = (
                ", ".join(
                    str(value)
                    for value in conditions
                )
                if conditions
                else "any"
            )

            lines.extend(
                [
                    "",
                    f"• {name}",
                    f"  Max: {max_text}",
                    f"  Sizes: {size_text}",
                    f"  Conditions: {condition_text}",
                ]
            )

        return "\n".join(
            lines
        )

    @staticmethod
    def _health_message() -> str:
        service_state = (
            TelegramCommandBot
            ._systemd_state(
                "vinted-agent.service"
            )
        )

        database = Database(
            DATABASE_PATH
        )

        try:
            total = database.count()
            pending = len(
                database.pending_notifications(
                    1000
                )
            )
            recent = database.recent_listings(
                1
            )
        finally:
            database.close()

        last_seen = (
            recent[0]["last_seen"]
            if recent
            else "n/a"
        )

        monitoring = (
            "paused"
            if is_paused()
            else "running"
        )

        return (
            "🩺 Vinted Agent health\n\n"
            f"Service: {service_state}\n"
            f"Monitoring: {monitoring}\n"
            f"Database: OK ({total} listings)\n"
            f"Pending Telegram alerts: {pending}\n"
            f"Latest listing seen: {last_seen}"
        )

    @staticmethod
    def _status_message() -> str:
        state = (
            "PAUSED ⏸"
            if is_paused()
            else "RUNNING ▶️"
        )

        return (
            "Vinted catalogue monitoring: "
            f"{state}\n\n"
            "Scheduled statistics: daily at 20:00 Europe/London."
        )

    @staticmethod
    def _systemd_state(
        unit: str,
    ) -> str:
        try:
            result = subprocess.run(
                [
                    "systemctl",
                    "is-active",
                    unit,
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (
            OSError,
            subprocess.SubprocessError,
        ):
            return "unknown"

        state = (
            result.stdout
            or result.stderr
            or "unknown"
        ).strip()

        return state or "unknown"

    @staticmethod
    def _truncate(
        text: str,
        limit: int,
    ) -> str:
        if len(
            text
        ) <= limit:
            return text

        suffix = "\n…"

        return (
            text[
                : limit - len(
                    suffix
                )
            ].rstrip()
            + suffix
        )


def _handle_shutdown(
    signum: int,
    frame: object,
) -> None:
    LOGGER.info(
        "Telegram command shutdown signal: %s",
        signum,
    )
    STOP_EVENT.set()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s | %(levelname)s | %(message)s"
        ),
    )

    signal.signal(
        signal.SIGINT,
        _handle_shutdown,
    )
    signal.signal(
        signal.SIGTERM,
        _handle_shutdown,
    )

    bot = TelegramCommandBot()

    try:
        bot.run_forever()
    finally:
        bot.close()


if __name__ == "__main__":
    main()
