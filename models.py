import re


class Listing:

    def __init__(
        self,
        id,
        title,
        subtitle,
        price,
        total_price,
        url,
        image
    ):

        self.id = id

        self.title = title
        self.subtitle = subtitle

        self.price = price
        self.total_price = total_price

        self.price_value = self._extract_price(price)
        self.total_price_value = self._extract_price(total_price)

        self.url = url
        self.image = image

        self.search_id = None
        self.search_name = None

    def _extract_price(self, text):
        """
        Convert:
            £74.20 -> 74.20

        Returns float.
        """

        if not text:
            return 0.0

        text = text.replace(",", ".")

        match = re.search(r"(\d+(?:\.\d+)?)", text)

        if not match:
            return 0.0

        return float(match.group(1))

    def __str__(self):

        return (
            f"{self.title} | "
            f"{self.price} | "
            f"{self.subtitle}"
        )