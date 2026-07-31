"""Load and validate Vinted search configuration."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Final

from search import Search

LOGGER = logging.getLogger(__name__)

DEFAULT_SEARCHES_FILE: Final[Path] = Path("searches.json")


class SearchConfigurationError(RuntimeError):
    """Raised when the searches configuration cannot be loaded."""


class SearchManager:
    """Load Vinted searches from a JSON configuration file."""

    def __init__(
        self,
        filename: str | Path = DEFAULT_SEARCHES_FILE,
    ) -> None:
        self.filename = Path(filename).expanduser()

    def load(self) -> list[Search]:
        """Load, validate, and return configured searches."""

        data = self._read_file()

        if not isinstance(data, list):
            raise SearchConfigurationError(
                f"{self.filename} must contain a JSON list"
            )

        searches: list[Search] = []
        search_ids: set[str] = set()

        for index, item in enumerate(data, start=1):
            search = self._create_search(item, index)

            if search.id in search_ids:
                raise SearchConfigurationError(
                    f"Duplicate search id '{search.id}' "
                    f"in {self.filename}"
                )

            search_ids.add(search.id)
            searches.append(search)

        LOGGER.info(
            "Loaded %s searches from %s",
            len(searches),
            self.filename,
        )

        return searches

    def _read_file(self) -> Any:
        if not self.filename.exists():
            raise SearchConfigurationError(
                f"Search configuration file not found: {self.filename}"
            )

        if not self.filename.is_file():
            raise SearchConfigurationError(
                f"Search configuration path is not a file: {self.filename}"
            )

        try:
            with self.filename.open("r", encoding="utf-8") as file:
                return json.load(file)

        except json.JSONDecodeError as exc:
            raise SearchConfigurationError(
                f"Invalid JSON in {self.filename} "
                f"at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from exc

        except OSError as exc:
            raise SearchConfigurationError(
                f"Unable to read {self.filename}: {exc}"
            ) from exc

    def _create_search(
        self,
        item: object,
        index: int,
    ) -> Search:
        if not isinstance(item, dict):
            raise SearchConfigurationError(
                f"Search entry {index} must be a JSON object"
            )

        try:
            search = Search(
                id=self._required_value(item, "id", index),
                name=self._required_value(item, "name", index),
                url=self._required_value(item, "url", index),
                max_price=item.get("max_price"),
                keywords=self._list_value(item, "keywords", index),
                sizes=self._list_value(item, "sizes", index),
                conditions=self._list_value(
                    item,
                    "conditions",
                    index,
                ),
            )
        except (TypeError, ValueError) as exc:
            raise SearchConfigurationError(
                f"Invalid search entry {index}: {exc}"
            ) from exc

        return search

    @staticmethod
    def _required_value(
        item: dict[str, Any],
        key: str,
        index: int,
    ) -> Any:
        if key not in item:
            raise SearchConfigurationError(
                f"Search entry {index} is missing '{key}'"
            )

        value = item[key]

        if value is None or not str(value).strip():
            raise SearchConfigurationError(
                f"Search entry {index} has an empty '{key}'"
            )

        return value

    @staticmethod
    def _list_value(
        item: dict[str, Any],
        key: str,
        index: int,
    ) -> list[Any]:
        value = item.get(key, [])

        if value is None:
            return []

        if not isinstance(value, list):
            raise SearchConfigurationError(
                f"Search entry {index} field '{key}' must be a list"
            )

        return value