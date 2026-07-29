from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


def accept_cookies(driver):
    try:
        button = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, 'button[data-testid="accept-all"]')
            )
        )

        button.click()

        print("✅ Cookies accepted")

    except Exception:
        print("ℹ️ Cookie popup not found")