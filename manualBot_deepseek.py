import time
from datetime import datetime, time as dt_time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException

# ---------- Configuration ----------
ONLINE_URL = "https://d.easytrader.ir/"
DEFAULT_TIMEOUT = 20
LOGIN_TIMEOUT = 20
CHROMEDRIVER_PATH = r"C:\Users\Mahdi\.cache\selenium\chromedriver\win64\148.0.7778.97\chromedriver.exe"


# ---------- Driver Setup ----------
def initialize_driver():
    """Create and return a configured ChromeDriver instance."""
    options = Options()
    options.add_argument('--ignore-certificate-errors')
    options.add_experimental_option("detach", True)
    options.set_capability('goog:loggingPrefs', {'browser': 'ALL'})

    print("Initializing ChromeDriver...")
    service = Service(executable_path=CHROMEDRIVER_PATH)
    driver = webdriver.Chrome(service=service, options=options)
    driver.maximize_window()
    print("Driver initialized.")
    return driver


# ---------- Helper Waits & Actions ----------
def _wait(driver, xpath, condition, timeout=DEFAULT_TIMEOUT):
    """Wait for an element located by XPATH to satisfy a condition."""
    return WebDriverWait(driver, timeout).until(condition((By.XPATH, xpath)))


def _click(driver, xpath, timeout=DEFAULT_TIMEOUT):
    """Wait for element to be clickable and click it."""
    el = _wait(driver, xpath, EC.element_to_be_clickable, timeout)
    el.click()
    return el


def _type(driver, xpath, text, timeout=DEFAULT_TIMEOUT):
    """Wait for element to be visible and type text."""
    el = _wait(driver, xpath, EC.visibility_of_element_located, timeout)
    el.send_keys(text)
    return el


# ---------- Login ----------
def login(driver, username, password, max_attempts=5):
    """Login with retry mechanism. Returns True on success."""
    for attempt in range(1, max_attempts + 1):
        driver.get(ONLINE_URL)

        _type(driver, '//*[@id="user-name"]', username, timeout=LOGIN_TIMEOUT)
        _type(driver, '//*[@id="password"]', password, timeout=LOGIN_TIMEOUT)
        _click(driver, '//*[@id="primary_form"]/div[3]/button', timeout=LOGIN_TIMEOUT)

        try:
            _wait(
                driver,
                '//*[@id="root"]/main/div[2]/div[1]/ul[1]/li[2]',
                EC.element_to_be_clickable,
                timeout=LOGIN_TIMEOUT,
            )
            print("Login successful.")
            return True
        except TimeoutException:
            print(f"Login attempt {attempt}/{max_attempts} failed, retrying...")
            time.sleep(1.0)   # brief pause before reload

    return False


# ---------- Dismiss Modals ----------
def dismiss_announcement(driver):
    """Close announcement modal if present."""
    try:
        el = _wait(
            driver,
            '//*[@id="cancel-action-confirm-btn"]',
            EC.element_to_be_clickable,
            timeout=5,
        )
        el.click()
        print("Announcement closed.")
    except TimeoutException:
        print("No announcement.")


# ---------- Symbol Search (improved) ----------
def find_symbol(driver, symbol):
    """
    Navigate to symbol search, enter symbol, and click the first result.
    All waits are explicit – no hard sleep.
    """
    # Click the 'Symbol' tab (li[2])
    _click(driver, '//*[@id="root"]/main/div[2]/div[1]/ul[1]/li[2]')

    # Wait for search input to appear
    _type(driver, '//*[@id="searchInputControl"]', symbol)

    # Wait for first suggestion to be clickable (id="1" may be dynamic, but kept)
    _click(driver, '//*[@id="1"]/div[1]')


# ---------- Broker Time Handling ----------
def get_broker_time(driver, clock_element=None):
    """
    Read broker clock text (HH:MM:SS) and return a time object.
    If clock_element is provided, reuse it to avoid repeated waits.
    """
    if clock_element is None:
        clock_element = WebDriverWait(driver, 5).until(
            EC.visibility_of_element_located((By.ID, "easy-clock-id"))
        )
    time_str = clock_element.text.strip()
    parts = time_str.split(":")
    if len(parts) == 3:
        return dt_time(int(parts[0]), int(parts[1]), int(parts[2]))
    raise ValueError(f"Unexpected broker time format: {time_str}")


def wait_until_broker_time(driver, target, poll_interval=0.05):
    """
    Busy-wait until the broker clock shows >= target time.
    Reuses the clock element to minimise Selenium calls.
    """
    clock_elem = WebDriverWait(driver, 10).until(
        EC.visibility_of_element_located((By.ID, "easy-clock-id"))
    )
    while True:
        now = get_broker_time(driver, clock_element=clock_elem)
        if now >= target and now<=dt_time(15,30,0):
            break
        time.sleep(poll_interval)


# ---------- Order Acceptance Checks ----------
def is_order_accepted(broker_message):
    """Determine if the broker message indicates success."""
    msg = broker_message.lower()
    if any(kw in msg for kw in ["موفق", "accept", "success"]):
        return True
    if any(kw in msg for kw in ["خطا", "reject", "ناموفق", "عدم پذیرش"]):
        return False
    return False


def capture_broker_notification(driver, timeout=1):
    """
    Wait for and return the text of a broker notification (toast/message).
    Tries several common selectors.
    """
    notif_selectors = [
        (By.XPATH, "/html/body/app-root/app-notify/div/div"),
        (By.CLASS_NAME, "notify-message"),
        (By.XPATH, "//div[contains(@class, 'toast')]"),
    ]
    for by, selector in notif_selectors:
        try:
            elem = WebDriverWait(driver, timeout).until(
                EC.visibility_of_element_located((by, selector))
            )
            return elem.text
        except TimeoutException:
            continue
    return "(no notification found)"


# ---------- Robust Buy Click (using your exact element) ----------
def buy_click_robust(driver):
    """
    Click the buy button using the most reliable locator (data-cy),
    then capture the broker's response.
    Returns (success, message).
    """
    # Primary locator – exactly the button you provided
    primary_locator = (By.CSS_SELECTOR, 'button[data-cy="oms-order-form-submit-button-buy"]')
    fallback_locators = [
        (By.XPATH, '//*[@id="root"]/main/div[3]/d-order-list/div/div[3]/order-form-tabular-container/div/order-form-actions/div/button[1]'),
        (By.XPATH, "//button[contains(@class, 'btn-success')]"),
        (By.XPATH, "//button[contains(., 'ارسال خرید')]"),
    ]
    # elemnt you need to click: <button type="button" class="btn btn-success d-flex flex-grow-1 justify-content-center rounded-3" data-cy="oms-order-form-submit-button-buy"><span class="button-text text-nowrap">ارسال خرید</span></button>

    button = None
    for by, selector in [primary_locator] + fallback_locators:
        try:
            button = WebDriverWait(driver, 2).until(
                EC.element_to_be_clickable((by, selector))
            )
            break
        except TimeoutException:
            continue

    if button is None:
        return False, "Buy button not found"

    # Click and immediately try to capture the broker response
    button.click()
    broker_message = capture_broker_notification(driver, timeout=2)
    success = is_order_accepted(broker_message)
    return success, broker_message


# ---------- Main Workflow ----------
def main():
    USERNAME = "1051047595"
    PASSWORD = "1051047595Ma"
    # SYMBOL = "فولاد"   # not used because you prepare manually
    target_time = dt_time(8, 44, 58)

    # ---- Login ----
    driver = initialize_driver()
    if not login(driver, USERNAME, PASSWORD):
        print("Login failed. Exiting.")
        return
    dismiss_announcement(driver)

    # ---- Manual Preparation ----
    print("\n" + "=" * 60)
    print(">>> NOW PREPARE THE ORDER MANUALLY <<<")
    print("1. Navigate to the symbol (e.g. فولاد)")
    print("2. Fill in price and quantity")
    print("3. Make sure the BUY button is visible (do NOT click it)")
    print("=" * 60)
    input("Press ENTER when ready ...")

    # Quick check that the primary buy button is present
    try:
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, 'button[data-cy="oms-order-form-submit-button-buy"]')
            )
        )
        print("Buy button detected – good to go.")
    except TimeoutException:
        print("Warning: buy button not found, but will still attempt later.")

    # ---- Wait for exact time ----
    print(f"Waiting for broker clock to reach {target_time.strftime('%H:%M:%S')} ...")
    wait_until_broker_time(driver, target_time)

    # ---- Rapid clicking after target time ----
    RETRY_INTERVAL = 0.15   # seconds between attempts
    MAX_ATTEMPTS = 25

    print(f"Target time reached – starting up to {MAX_ATTEMPTS} attempts every {RETRY_INTERVAL}s")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        success, msg = buy_click_robust(driver)
        print(f"Attempt {attempt}: {msg}")
        if success:
            print("✅ Order successfully accepted by broker.")
            break
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_INTERVAL)
    else:
        print("❌ Order not accepted after all retries. Check manually.")

    # Keep browser open for inspection
    print("Keeping browser open for 10 seconds...")
    time.sleep(10)
    # driver.quit()


if __name__ == "__main__":
    main()