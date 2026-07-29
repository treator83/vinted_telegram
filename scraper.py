import re
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from webdriver_manager.chrome import ChromeDriverManager

from config import HEADLESS
from models import Listing


class VintedScraper:

    def __init__(self):
        self.driver = None
        self.wait = None

    def start(self):
        options = Options()

        if HEADLESS:
            options.add_argument("--headless=new")

        options.add_argument("--window-size=1600,1200")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-infobars")
        options.add_argument("--start-maximized")

        self.driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=options
        )

        self.wait = WebDriverWait(self.driver, 20)

    def stop(self):
        if self.driver:
            self.driver.quit()
            self.driver = None

    def open(self, url):
        print(f"\nOpening:\n{url}\n")

        self.driver.get(url)

        self.wait.until(
            EC.presence_of_element_located(
                (By.TAG_NAME, "body")
            )
        )

        time.sleep(2)

    def accept_cookies(self):
        try:
            buttons = self.driver.find_elements(By.TAG_NAME, "button")

            for button in buttons:
                try:
                    text = button.text.strip().lower()

                    if text == "accept all":
                        self.driver.execute_script(
                            "arguments[0].click();",
                            button
                        )

                        print("✅ Cookies accepted")
                        time.sleep(1)
                        return

                except Exception:
                    pass

            print("ℹ️ Cookie banner not found")

        except Exception as e:
            print("Cookie error:", e)

    def fetch(self):
        """
        Read all listings from the current Vinted page.
        Returns a list of Listing objects.
        """

        self.wait.until(
            EC.presence_of_all_elements_located(
                (By.CSS_SELECTOR, '[data-testid="grid-item"]')
            )
        )

        cards = self.driver.find_elements(
            By.CSS_SELECTOR,
            '[data-testid="grid-item"]'
        )

        print(f"Found {len(cards)} listings")

        listings = []

        for card in cards:

            try:

                html = card.get_attribute("innerHTML")

                # Listing ID
                match = re.search(r'product-item-id-(\d+)', html)

                if not match:
                    continue

                listing_id = match.group(1)

                # Brand / title
                title = ""

                try:
                    title = card.find_element(
                        By.CSS_SELECTOR,
                        '[data-testid$="description-title"]'
                    ).text.strip()
                except:
                    pass

                # Subtitle (size + condition)
                subtitle = ""

                try:
                    subtitle = card.find_element(
                        By.CSS_SELECTOR,
                        '[data-testid$="description-subtitle"]'
                    ).text.strip()
                except:
                    pass

                # Price
                price = ""

                try:
                    price = card.find_element(
                        By.CSS_SELECTOR,
                        '[data-testid$="price-text"]'
                    ).text.strip()
                except:
                    pass

                # Total price
                total_price = ""

                try:
                    total_price = card.find_element(
                        By.CSS_SELECTOR,
                        '[data-testid="total-combined-price"]'
                    ).text.strip()
                except:
                    pass

                # URL
                url = ""

                try:
                    url = card.find_element(
                        By.CSS_SELECTOR,
                        'a[href*="/items/"]'
                    ).get_attribute("href")
                except:
                    pass

                # Image
                image = ""

                try:
                    image = card.find_element(
                        By.TAG_NAME,
                        "img"
                    ).get_attribute("src")
                except:
                    pass

                listings.append(
                    Listing(
                        id=listing_id,
                        title=title,
                        subtitle=subtitle,
                        price=price,
                        total_price=total_price,
                        url=url,
                        image=image
                    )
                )

            except Exception as e:
                print("Listing parse error:", e)

        return listings