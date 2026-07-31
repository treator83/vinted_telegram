"""Telegram notifications for new listings and price drops."""

from __future__ import annotations

import io
import logging
import re
from typing import Final

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import BOT_TOKEN, CHAT_ID
from models import Listing

LOGGER = logging.getLogger(__name__)

TELEGRAM_API_URL: Final[str] = "https://api.telegram.org"
REQUEST_TIMEOUT: Final[tuple[int, int]] = (10, 30)
IMAGE_DOWNLOAD_TIMEOUT: Final[tuple[int, int]] = (10, 30)

MAX_CAPTION_LENGTH: Final[int] = 1_024
MAX_MESSAGE_LENGTH: Final[int] = 4_096
MAX_IMAGE_SIZE_BYTES: Final[int] = 10 * 1024 * 1024

IMAGE_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)

CURRENCY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s*([^\d\s.,+-]+)"
)


class TelegramClient:
    """Send Vinted listing notifications through the Telegram Bot API."""

    def __init__(
        self,
        bot_token: str = BOT_TOKEN,
        chat_id: str | int = CHAT_ID,
    ) -> None:
        self.bot_token = str(bot_token).strip()
        self.chat_id = str(chat_id).strip()

        if not self.bot_token:
            raise ValueError("Telegram BOT_TOKEN is not configured")

        if not self.chat_id:
            raise ValueError("Telegram CHAT_ID is not configured")

        self._api = f"{TELEGRAM_API_URL}/bot{self.bot_token}"
        self._session = self._create_session()

    def send_listing(self, listing: Listing) -> bool:
        """Send a notification for a newly discovered listing."""

        caption = self._build_listing_caption(listing)

        if listing.image and self._send_photo(listing.image, caption):
            LOGGER.info("Sent new-listing photo notification for %s", listing.id)
            return True

        sent = self._send_text(caption)

        if sent:
            LOGGER.info("Sent new-listing text notification for %s", listing.id)

        return sent

    def send_price_drop(
        self,
        listing: Listing,
        old_price: float,
    ) -> bool:
        """Send a notification when a listing price decreases."""

        caption = self._build_price_drop_caption(
            listing=listing,
            old_price=old_price,
        )

        if listing.image and self._send_photo(listing.image, caption):
            LOGGER.info("Sent price-drop photo notification for %s", listing.id)
            return True

        sent = self._send_text(caption)

        if sent:
            LOGGER.info("Sent price-drop text notification for %s", listing.id)

        return sent

    def close(self) -> None:
        """Close the reusable HTTP session."""

        self._session.close()

    def __enter__(self) -> TelegramClient:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        self.close()

    def _send_photo(
        self,
        image_url: str,
        caption: str,
    ) -> bool:
        image = self._download_image(image_url)

        if image is None:
            return False

        image_data, filename, content_type = image

        try:
            response = self._session.post(
                f"{self._api}/sendPhoto",
                data={
                    "chat_id": self.chat_id,
                    "caption": self._truncate(
                        caption,
                        MAX_CAPTION_LENGTH,
                    ),
                },
                files={
                    "photo": (
                        filename,
                        image_data,
                        content_type,
                    ),
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            LOGGER.warning("Telegram photo request failed: %s", exc)
            return False

        return self._response_succeeded(
            response,
            operation="sendPhoto",
        )

    def _send_text(self, text: str) -> bool:
        try:
            response = self._session.post(
                f"{self._api}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": self._truncate(
                        text,
                        MAX_MESSAGE_LENGTH,
                    ),
                    "disable_web_page_preview": False,
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            LOGGER.warning("Telegram text request failed: %s", exc)
            return False

        return self._response_succeeded(
            response,
            operation="sendMessage",
        )

    def _download_image(
        self,
        image_url: str,
    ) -> tuple[io.BytesIO, str, str] | None:
        try:
            response = self._session.get(
                image_url,
                headers={
                    "User-Agent": IMAGE_USER_AGENT,
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
                timeout=IMAGE_DOWNLOAD_TIMEOUT,
                stream=True,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            LOGGER.warning("Unable to download listing image: %s", exc)
            return None

        content_type = (
            response.headers.get("Content-Type", "image/jpeg")
            .split(";", maxsplit=1)[0]
            .strip()
            .lower()
        )

        if not content_type.startswith("image/"):
            LOGGER.warning(
                "Listing image returned unsupported content type: %s",
                content_type,
            )
            response.close()
            return None

        image_data = io.BytesIO()
        downloaded_bytes = 0

        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue

                downloaded_bytes += len(chunk)

                if downloaded_bytes > MAX_IMAGE_SIZE_BYTES:
                    LOGGER.warning(
                        "Listing image exceeds the maximum upload size"
                    )
                    return None

                image_data.write(chunk)
        except requests.RequestException as exc:
            LOGGER.warning("Listing image download was interrupted: %s", exc)
            return None
        finally:
            response.close()

        if downloaded_bytes == 0:
            LOGGER.warning("Listing image download returned no data")
            return None

        image_data.seek(0)

        extension = self._image_extension(content_type)
        filename = f"listing{extension}"

        return image_data, filename, content_type

    @staticmethod
    def _build_listing_caption(listing: Listing) -> str:
        search_name = TelegramClient._display_value(listing.search_name)
        title = TelegramClient._display_value(listing.title)
        subtitle = TelegramClient._display_value(listing.subtitle)
        price = TelegramClient._display_value(listing.price)
        total_price = TelegramClient._display_value(listing.total_price)

        return (
            "🏍 NEW LISTING\n\n"
            f"Search:\n{search_name}\n\n"
            f"Brand:\n{title}\n\n"
            f"Info:\n{subtitle}\n\n"
            f"Price:\n{price}\n\n"
            f"Total:\n{total_price}\n\n"
            f"{listing.url}"
        )

    @staticmethod
    def _build_price_drop_caption(
        listing: Listing,
        old_price: float,
    ) -> str:
        saved = max(0.0, old_price - listing.price_value)
        currency = TelegramClient._currency_symbol(listing.price)

        search_name = TelegramClient._display_value(listing.search_name)
        title = TelegramClient._display_value(listing.title)
        subtitle = TelegramClient._display_value(listing.subtitle)
        new_price = TelegramClient._display_value(listing.price)

        return (
            "💰 PRICE DROP\n\n"
            f"Search:\n{search_name}\n\n"
            f"Brand:\n{title}\n\n"
            f"Info:\n{subtitle}\n\n"
            f"Old Price:\n{currency}{old_price:.2f}\n\n"
            f"New Price:\n{new_price}\n\n"
            f"You Save:\n{currency}{saved:.2f}\n\n"
            f"{listing.url}"
        )

    @staticmethod
    def _response_succeeded(
        response: requests.Response,
        operation: str,
    ) -> bool:
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if response.ok and payload.get("ok") is True:
            return True

        description = payload.get("description")

        if not description:
            description = response.text[:300].strip() or "Unknown error"

        LOGGER.warning(
            "Telegram %s failed with HTTP %s: %s",
            operation,
            response.status_code,
            description,
        )

        return False

    @staticmethod
    def _create_session() -> requests.Session:
        session = requests.Session()

        retry_policy = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.75,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )

        adapter = HTTPAdapter(
            max_retries=retry_policy,
            pool_connections=4,
            pool_maxsize=4,
        )

        session.mount("https://", adapter)
        session.mount("http://", adapter)

        return session

    @staticmethod
    def _display_value(value: object | None) -> str:
        text = str(value or "").strip()
        return text or "Not available"

    @staticmethod
    def _currency_symbol(price_text: str) -> str:
        match = CURRENCY_PATTERN.search(price_text or "")
        return match.group(1) if match else "£"

    @staticmethod
    def _image_extension(content_type: str) -> str:
        extensions = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }

        return extensions.get(content_type, ".jpg")

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text

        suffix = "\n…"
        return text[: limit - len(suffix)].rstrip() + suffix