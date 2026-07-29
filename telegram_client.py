import os
import tempfile

import requests

from config import BOT_TOKEN, CHAT_ID


class TelegramClient:

    def __init__(self):

        self.api = f"https://api.telegram.org/bot{BOT_TOKEN}"

    # ----------------------------------------------------
    # Internal helper
    # ----------------------------------------------------

    def _send_photo(self, image_url, caption):

        image_path = None

        try:

            response = requests.get(
                image_url,
                timeout=30
            )

            if response.status_code != 200:
                raise Exception("Couldn't download image")

            fd, image_path = tempfile.mkstemp(
                suffix=".jpg"
            )

            os.close(fd)

            with open(image_path, "wb") as f:
                f.write(response.content)

            with open(image_path, "rb") as photo:

                response = requests.post(

                    f"{self.api}/sendPhoto",

                    data={
                        "chat_id": CHAT_ID,
                        "caption": caption
                    },

                    files={
                        "photo": photo
                    },

                    timeout=30
                )

            return response.status_code == 200

        except Exception as e:

            print("Telegram photo error:", e)

            return False

        finally:

            if image_path and os.path.exists(image_path):
                os.remove(image_path)

    def _send_text(self, text):

        try:

            response = requests.post(

                f"{self.api}/sendMessage",

                data={
                    "chat_id": CHAT_ID,
                    "text": text,
                    "disable_web_page_preview": False
                },

                timeout=30
            )

            return response.status_code == 200

        except Exception as e:

            print("Telegram text error:", e)

            return False

    # ----------------------------------------------------
    # New listing
    # ----------------------------------------------------

    def send_listing(self, listing):

        caption = f"""🏍 NEW LISTING

Search:
{listing.search_name}

Brand:
{listing.title}

Info:
{listing.subtitle}

Price:
{listing.price}

Total:
{listing.total_price}

{listing.url}
"""

        if not self._send_photo(
            listing.image,
            caption
        ):
            self._send_text(caption)

    # ----------------------------------------------------
    # Price drop
    # ----------------------------------------------------

    def send_price_drop(
        self,
        listing,
        old_price
    ):

        saved = old_price - listing.price_value

        caption = f"""💰 PRICE DROP

Search:
{listing.search_name}

Brand:
{listing.title}

Info:
{listing.subtitle}

Old Price:
£{old_price:.2f}

New Price:
{listing.price}

You Save:
£{saved:.2f}

{listing.url}
"""

        if not self._send_photo(
            listing.image,
            caption
        ):
            self._send_text(caption)