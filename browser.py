"""Reliable Selenium browser lifecycle management.

This module is intentionally Vinted-agnostic.  It owns Chromium startup,
page navigation, health checks, retries, and clean shutdown.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Final

from selenium import webdriver
from selenium.common.exceptions import (
    InvalidSessionIdException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from config import HEADLESS

LOGGER = logging.getLogger(__name__)

DEFAULT_PAGE_LOAD_TIMEOUT: Final[int] = 45
DEFAULT_ELEMENT_TIMEOUT: Final[int] = 20
DEFAULT_NAVIGATION_RETRIES: Final[int] = 2


class BrowserError(RuntimeError):
    """Raised when Chromium cannot complete a browser operation."""


@dataclass(frozen=True, slots=True)
class BrowserSettings:
    """Runtime settings for the Chromium browser."""

    headless: bool = HEADLESS
    page_load_timeout: int = DEFAULT_PAGE_LOAD_TIMEOUT
    element_timeout: int = DEFAULT_ELEMENT_TIMEOUT
    navigation_retries: int = DEFAULT_NAVIGATION_RETRIES
    window_width: int = 1600
    window_height: int = 1200
    chrome_binary: str | None = os.getenv("CHROME_BINARY") or None
    chromedriver_path: str | None = os.getenv("CHROMEDRIVER_PATH") or None


class Browser:
    """Manage one reusable Selenium Chromium session.

    The class automatically recreates the driver when navigation fails because
    of a dead or invalid Selenium session.
    """

    def __init__(self, settings: BrowserSettings | None = None) -> None:
        self.settings = settings or BrowserSettings()
        self._driver: WebDriver | None = None
        self._wait: WebDriverWait | None = None

    @property
    def driver(self) -> WebDriver:
        """Return the active driver, starting Chromium when necessary."""
        if self._driver is None:
            self.start()

        if self._driver is None:  # Defensive check for type checkers.
            raise BrowserError("Chromium did not start")

        return self._driver

    @property
    def wait(self) -> WebDriverWait:
        """Return the wait helper attached to the current driver."""
        _ = self.driver
        if self._wait is None:
            raise BrowserError("Browser wait helper is unavailable")
        return self._wait

    def start(self) -> None:
        """Start Chromium unless a healthy session is already running."""
        if self.is_healthy():
            return

        self.stop()
        options = self._build_options()
        service = self._build_service()

        try:
            LOGGER.info("Starting Chromium")
            self._driver = webdriver.Chrome(service=service, options=options)
            self._driver.set_page_load_timeout(self.settings.page_load_timeout)
            self._wait = WebDriverWait(
                self._driver,
                self.settings.element_timeout,
            )
            LOGGER.info("Chromium started successfully")
        except WebDriverException as exc:
            self._driver = None
            self._wait = None
            raise BrowserError(f"Unable to start Chromium: {exc}") from exc

    def stop(self) -> None:
        """Close Chromium safely. Calling this repeatedly is harmless."""
        driver = self._driver
        self._driver = None
        self._wait = None

        if driver is None:
            return

        try:
            driver.quit()
            LOGGER.info("Chromium stopped")
        except WebDriverException as exc:
            LOGGER.warning("Chromium did not close cleanly: %s", exc)

    def restart(self) -> None:
        """Replace the current Selenium session with a fresh one."""
        LOGGER.warning("Restarting Chromium session")
        self.stop()
        self.start()

    def is_healthy(self) -> bool:
        """Return whether the current driver still responds to commands."""
        if self._driver is None:
            return False

        try:
            _ = self._driver.current_url
            return True
        except (InvalidSessionIdException, WebDriverException):
            return False

    def get(self, url: str) -> None:
        """Open a URL, retrying with a fresh browser after recoverable errors."""
        if not url or not url.strip():
            raise ValueError("A non-empty URL is required")

        attempts = self.settings.navigation_retries + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                if not self.is_healthy():
                    self.start()

                started_at = time.monotonic()
                LOGGER.info("Opening %s (attempt %s/%s)", url, attempt, attempts)
                self.driver.get(url)
                self.wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                elapsed = time.monotonic() - started_at
                LOGGER.info("Page loaded in %.2f seconds", elapsed)
                return

            except (TimeoutException, InvalidSessionIdException, WebDriverException) as exc:
                last_error = exc
                LOGGER.warning(
                    "Navigation failed on attempt %s/%s: %s",
                    attempt,
                    attempts,
                    exc,
                )

                if attempt < attempts:
                    self.restart()
                    time.sleep(min(attempt, 2))

        raise BrowserError(f"Unable to open {url}: {last_error}") from last_error

    def __enter__(self) -> "Browser":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.stop()

    def _build_options(self) -> Options:
        options = Options()

        if self.settings.chrome_binary:
            options.binary_location = self.settings.chrome_binary

        if self.settings.headless:
            options.add_argument("--headless=new")

        options.add_argument(
            f"--window-size={self.settings.window_width},{self.settings.window_height}"
        )
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-default-apps")
        options.add_argument("--disable-sync")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--password-store=basic")
        options.add_argument("--use-mock-keychain")
        options.add_argument("--remote-debugging-port=0")
        options.add_argument("--lang=en-GB")

        options.add_experimental_option(
            "excludeSwitches",
            ["enable-automation", "enable-logging"],
        )
        options.add_experimental_option("useAutomationExtension", False)

        return options

    def _build_service(self) -> Service:
        if self.settings.chromedriver_path:
            return Service(executable_path=self.settings.chromedriver_path)