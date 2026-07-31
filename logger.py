"""Central logging configuration for Vinted Agent."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Final

DEFAULT_LOG_FILE: Final[Path] = Path("logs/vinted.log")
DEFAULT_LOG_LEVEL: Final[int] = logging.INFO
MAX_LOG_SIZE_BYTES: Final[int] = 5 * 1024 * 1024
LOG_BACKUP_COUNT: Final[int] = 5

LOG_FORMAT: Final[str] = (
    "[%(asctime)s] %(levelname)s | %(name)s | %(message)s"
)
DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"


def configure_logging(
    log_file: str | Path = DEFAULT_LOG_FILE,
    level: int = DEFAULT_LOG_LEVEL,
) -> logging.Logger:
    """Configure console and rotating-file logging.

    Calling this function more than once does not add duplicate handlers.
    """

    log_path = Path(log_file).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    formatter = logging.Formatter(
        fmt=LOG_FORMAT,
        datefmt=DATE_FORMAT,
    )

    _add_console_handler(
        logger=root_logger,
        formatter=formatter,
        level=level,
    )

    _add_file_handler(
        logger=root_logger,
        formatter=formatter,
        log_path=log_path,
        level=level,
    )

    return root_logger


class Logger:
    """Backward-compatible logging wrapper.

    Existing code can continue using:

        logger = Logger()
        logger.log("Message")
    """

    def __init__(
        self,
        filename: str | Path = DEFAULT_LOG_FILE,
        name: str = "vinted_agent",
    ) -> None:
        configure_logging(filename)
        self._logger = logging.getLogger(name)

    def log(
        self,
        message: object,
        level: int = logging.INFO,
    ) -> None:
        """Write a message using the requested logging level."""

        self._logger.log(level, "%s", message)

    def debug(self, message: object) -> None:
        """Write a debug message."""

        self._logger.debug("%s", message)

    def info(self, message: object) -> None:
        """Write an informational message."""

        self._logger.info("%s", message)

    def warning(self, message: object) -> None:
        """Write a warning message."""

        self._logger.warning("%s", message)

    def error(self, message: object) -> None:
        """Write an error message."""

        self._logger.error("%s", message)

    def exception(self, message: object) -> None:
        """Write an error message with the active exception traceback."""

        self._logger.exception("%s", message)


def _add_console_handler(
    logger: logging.Logger,
    formatter: logging.Formatter,
    level: int,
) -> None:
    has_console_handler = any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        for handler in logger.handlers
    )

    if has_console_handler:
        return

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    logger.addHandler(console_handler)


def _add_file_handler(
    logger: logging.Logger,
    formatter: logging.Formatter,
    log_path: Path,
    level: int,
) -> None:
    resolved_path = log_path.resolve()

    for handler in logger.handlers:
        if not isinstance(handler, logging.FileHandler):
            continue

        handler_path = Path(handler.baseFilename).resolve()

        if handler_path == resolved_path:
            return

    file_handler = RotatingFileHandler(
        filename=resolved_path,
        maxBytes=MAX_LOG_SIZE_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    logger.addHandler(file_handler)