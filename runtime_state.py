"""Small file-based runtime state shared by Vinted Agent processes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DATA_DIR


PAUSE_FILE = DATA_DIR / "monitoring.paused"
HEALTH_FILE = DATA_DIR / "health.json"


def is_paused() -> bool:
    """Return True when catalogue monitoring is paused."""

    return PAUSE_FILE.exists()


def set_paused(paused: bool) -> None:
    """Enable or disable the persistent monitoring pause flag."""

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if paused:
        PAUSE_FILE.write_text(
            "paused\n",
            encoding="utf-8",
        )
        return

    try:
        PAUSE_FILE.unlink()
    except FileNotFoundError:
        pass


def write_health(data: dict[str, Any]) -> None:
    """Atomically write the latest agent health snapshot."""

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    payload = dict(data)
    payload["updated_at"] = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    temporary = HEALTH_FILE.with_suffix(
        ".json.tmp"
    )

    temporary.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    temporary.replace(
        HEALTH_FILE
    )


def read_health() -> dict[str, Any]:
    """Read the last health snapshot, returning an empty dict if absent."""

    try:
        payload = json.loads(
            HEALTH_FILE.read_text(
                encoding="utf-8"
            )
        )
    except (
        FileNotFoundError,
        OSError,
        json.JSONDecodeError,
    ):
        return {}

    if not isinstance(
        payload,
        dict,
    ):
        return {}

    return payload
