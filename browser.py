"""Reliable Selenium browser lifecycle management.

This module is intentionally Vinted-agnostic. It owns Chromium startup,
page navigation, health checks, retries, temporary-profile management, and
clean shutdown.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
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

# /tmp is a tmpfs on the production host and can be exhausted by repeated
# crashed Chrome profiles. /var/tmp is disk-backed on a normal Ubuntu install
# and is therefore a safer place for Selenium's disposable browser profile.
DEFAULT_CHROME_TEMP_ROOT: Final[Path] = Path("/var/tmp")
PROFILE_PREFIX: Final[str] = "vinted-agent-chrome-"


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
    temp_root: str | None = os.getenv("CHROME_TEMP_ROOT") or None


class Browser:
    """Manage one reusable Selenium Chromium session.

    The class automatically recreates the driver when navigation fails because
    of a dead or invalid Selenium session. Each browser session gets an owned
    temporary profile that is removed when the session ends.
    """

    def __init__(self, settings: BrowserSettings | None = None) -> None:
        self.settings = settings or BrowserSettings()
        self._driver: WebDriver | None = None
        self._wait: WebDriverWait | None = None
        self._profile_dir: Path | None = None

    @property
    def driver(self) -> WebDriver:
        """Return the active driver, starting Chromium when necessary."""
        if self._driver is None:
            self.start()

        if self._driver is None:
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

        profile_dir = self._create_profile_dir()
        self._profile_dir = profile_dir

        options = self._build_options(profile_dir)
        service = self._build_service()

        try:
            LOGGER.info(
                "Starting Chromium with temporary profile %s",
                profile_dir,
            )
            self._driver = webdriver.Chrome(
                service=service,
                options=options,
            )
            self._driver.set_page_load_timeout(
                self.settings.page_load_timeout
            )
            self._wait = WebDriverWait(
                self._driver,
                self.settings.element_timeout,
            )
            LOGGER.info("Chromium started successfully")
        except WebDriverException as exc:
            self._driver = None
            self._wait = None
            self._cleanup_profile_dir()
            raise BrowserError(
                f"Unable to start Chromium: {exc}"
            ) from exc

    def stop(self) -> None:
        """Close Chromium and remove its disposable profile safely."""
        driver = self._driver
        self._driver = None
        self._wait = None

        try:
            if driver is not None:
                try:
                    driver.quit()
                    LOGGER.info("Chromium stopped")
                except WebDriverException as exc:
                    LOGGER.warning(
                        "Chromium did not close cleanly: %s",
                        exc,
                    )
        finally:
            self._cleanup_profile_dir()

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
                LOGGER.info(
                    "Opening %s (attempt %s/%s)",
                    url,
                    attempt,
                    attempts,
                )
                self.driver.get(url)
                self.wait.until(
                    EC.presence_of_element_located(
                        (By.TAG_NAME, "body")
                    )
                )
                elapsed = time.monotonic() - started_at
                LOGGER.info("Page loaded in %.2f seconds", elapsed)
                return

            except (
                TimeoutException,
                InvalidSessionIdException,
                WebDriverException,
            ) as exc:
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

        raise BrowserError(
            f"Unable to open {url}: {last_error}"
        ) from last_error

    def __enter__(self) -> "Browser":
        self.start()
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        self.stop()

    def _create_profile_dir(self) -> Path:
        root = self._temporary_root()

        # ChromeDriver and Chrome inherit TMPDIR. This prevents helper files
        # from silently returning to the RAM-backed /tmp filesystem.
        os.environ["TMPDIR"] = str(root)
        tempfile.tempdir = None

        try:
            path = Path(
                tempfile.mkdtemp(
                    prefix=PROFILE_PREFIX,
                    dir=str(root),
                )
            )
        except OSError as exc:
            raise BrowserError(
                f"Unable to create Chromium temporary profile in {root}: {exc}"
            ) from exc

        return path

    def _temporary_root(self) -> Path:
        configured = self.settings.temp_root
        root = (
            Path(configured).expanduser()
            if configured
            else DEFAULT_CHROME_TEMP_ROOT
        )

        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BrowserError(
                f"Unable to prepare Chromium temporary directory {root}: {exc}"
            ) from exc

        if not os.access(root, os.W_OK | os.X_OK):
            raise BrowserError(
                f"Chromium temporary directory is not writable: {root}"
            )

        return root

    def _cleanup_profile_dir(self) -> None:
        profile_dir = self._profile_dir
        self._profile_dir = None

        if profile_dir is None:
            return

        try:
            shutil.rmtree(profile_dir)
            LOGGER.debug(
                "Removed Chromium temporary profile %s",
                profile_dir,
            )
        except FileNotFoundError:
            return
        except OSError as exc:
            LOGGER.warning(
                "Unable to remove Chromium temporary profile %s: %s",
                profile_dir,
                exc,
            )

    def _build_options(self, profile_dir: Path) -> Options:
        options = Options()

        if self.settings.chrome_binary:
            options.binary_location = self.settings.chrome_binary

        if self.settings.headless:
            options.add_argument("--headless=new")

        options.add_argument(
            f"--window-size={self.settings.window_width},"
            f"{self.settings.window_height}"
        )
        options.add_argument(
            f"--user-data-dir={profile_dir}"
        )

        options.add_argument("--no-sandbox")

        # Do not use --disable-dev-shm-usage on this host. /dev/shm is large
        # and available, while /tmp is RAM-backed and was being exhausted by
        # repeated crashed Chrome sessions.
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-default-apps")
        options.add_argument("--disable-sync")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-popup-blocking")
        options.add_argument(
            "--disable-blink-features=AutomationControlled"
        )
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--password-store=basic")
        options.add_argument("--use-mock-keychain")
        options.add_argument("--remote-debugging-port=0")
        options.add_argument("--lang=en-GB")

        # Keep disposable-profile cache growth bounded during long-running
        # scraping sessions.
        options.add_argument("--disk-cache-size=67108864")
        options.add_argument("--media-cache-size=33554432")

        options.add_experimental_option(
            "excludeSwitches",
            ["enable-automation", "enable-logging"],
        )
        options.add_experimental_option(
            "useAutomationExtension",
            False,
        )

        return options

    def _build_service(self) -> Service:
        if self.settings.chromedriver_path:
            return Service(
                executable_path=self.settings.chromedriver_path
            )

        return Service()
