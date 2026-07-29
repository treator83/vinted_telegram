"""Vinted search-page interaction and listing extraction."""

from __future__ import annotations

import logging
import re
import time
from typing import Final

from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC

from browser import Browser
from models import Listing

LOGGER = logging.getLogger(__name__)

GRID_ITEM_SELECTOR: Final[str] = '[data-testid="grid-item"]'
LISTING_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"product-item-id-(\d+)")


class VintedScraper:
    """Scrape Vinted listings through one reusable browser session."""

    def __init__(self, browser: Browser | None = None) -> None:
        self.browser = browser or Browser()
        self._cookies_checked = False

    @property
    def driver(self):
        """Expose the current Selenium driver for backward compatibility."""
        return self.browser.driver

    @property
    def wait(self):
        """Expose the current wait helper for backward compatibility."""
        return self.browser.wait

    def start(self) -> None:
        """Start the browser session."""
        self.browser.start()

    def stop(self) -> None:
        """Stop the browser session safely."""
        self.browser.stop()

    def open(self, url: str) -> None:
        """Open a Vinted search URL using browser retry and recovery logic."""
        self.browser.get(url)

    def accept_cookies(self) -> bool:
        """Accept the Vinted cookie banner once per browser session.

        Returns ``True`` when a banner was accepted and ``False`` when no
        matching banner was present.  The method avoids iterating through every
        page button, which previously caused stale-element failures.
        """
        if self._cookies_checked:
            return False

        selectors = (
            'button[data-testid="accept-all"]',
            '#onetrust-accept-btn-handler',
        )

        for selector in selectors:
            try:
                buttons = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for button in buttons:
                    if self._click_if_visible(button):
                        self._cookies_checked = True
                        LOGGER.info("Cookie banner accepted")
                        time.sleep(0.5)
                        return True
            except (StaleElementReferenceException, WebDriverException):
                continue

        try:
            xpath = (
                "//button[translate(normalize-space(.), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')="
                "'accept all']"
            )
            buttons = self.driver.find_elements(By.XPATH, xpath)
            for button in buttons:
                if self._click_if_visible(button):
                    self._cookies_checked = True
                    LOGGER.info("Cookie banner accepted")
                    time.sleep(0.5)
                    return True
        except (StaleElementReferenceException, WebDriverException):
            pass

        self._cookies_checked = True
        LOGGER.info("Cookie banner not present")
        return False

    def fetch(self) -> list[Listing]:
        """Return all parseable listings from the current search page."""
        try:
            self.wait.until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, GRID_ITEM_SELECTOR))
            )
        except TimeoutException:
            LOGGER.warning("No listing cards appeared before the timeout")
            return []

        cards = self.driver.find_elements(By.CSS_SELECTOR, GRID_ITEM_SELECTOR)
        LOGGER.info("Found %s listing cards", len(cards))

        listings: list[Listing] = []
        for card in cards:
            try:
                listing = self._parse_card(card)
                if listing is not None:
                    listings.append(listing)
            except StaleElementReferenceException:
                LOGGER.debug("Skipped a listing card that changed during parsing")
            except WebDriverException as exc:
                LOGGER.warning("Unable to parse a listing card: %s", exc)
            except Exception:
                LOGGER.exception("Unexpected listing parsing error")

        return listings

    def _parse_card(self, card: WebElement) -> Listing | None:
        listing_id = self._extract_listing_id(card)
        if listing_id is None:
            return None

        return Listing(
            id=listing_id,
            title=self._text(card, '[data-testid$="description-title"]'),
            subtitle=self._text(card, '[data-testid$="description-subtitle"]'),
            price=self._text(card, '[data-testid$="price-text"]'),
            total_price=self._text(card, '[data-testid="total-combined-price"]'),
            url=self._attribute(card, 'a[href*="/items/"]', "href"),
            image=self._attribute(card, "img", "src"),
        )

    @staticmethod
    def _extract_listing_id(card: WebElement) -> str | None:
        test_id = card.get_attribute("data-testid") or ""
        match = LISTING_ID_PATTERN.search(test_id)

        if match is None:
            html = card.get_attribute("innerHTML") or ""
            match = LISTING_ID_PATTERN.search(html)

        return match.group(1) if match else None

    @staticmethod
    def _text(card: WebElement, selector: str) -> str:
        try:
            return card.find_element(By.CSS_SELECTOR, selector).text.strip()
        except WebDriverException:
            return ""

    @staticmethod
    def _attribute(card: WebElement, selector: str, attribute: str) -> str:
        try:
            return (
                card.find_element(By.CSS_SELECTOR, selector).get_attribute(attribute)
                or ""
            ).strip()
        except WebDriverException:
            return ""

    def _click_if_visible(self, button: WebElement) -> bool:
        try:
            if not button.is_displayed() or not button.is_enabled():
                return False

            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});",
                button,
            )
            self.driver.execute_script("arguments[0].click();", button)
            return True
        except (StaleElementReferenceException, WebDriverException):
            return False
