"""
ChromeDriver setup with guaranteed cleanup.
"""

import atexit
import logging
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from config import CHROMEDRIVER_PATH

logger = logging.getLogger(__name__)

# Track all drivers created so we can clean them up on exit
_active_drivers: list[webdriver.Chrome] = []


def initialize_driver() -> webdriver.Chrome:
    """Create and return a configured ChromeDriver instance.
    The driver is registered for automatic cleanup on process exit.
    """
    options = Options()
    options.add_argument("--ignore-certificate-errors")
    # NOTE: 'detach' is intentionally NOT used here.
    # detach=True keeps Chrome alive even after the Python process exits,
    # which prevents Ctrl+C from closing the browser.
    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})

    logger.info("Initializing ChromeDriver from %s", CHROMEDRIVER_PATH)
    service = Service(executable_path=CHROMEDRIVER_PATH)
    driver = webdriver.Chrome(service=service, options=options)
    driver.maximize_window()

    _active_drivers.append(driver)
    atexit.register(_quit_driver, driver)
    logger.info("Driver initialized and cleanup registered.")
    return driver


def _quit_driver(driver: webdriver.Chrome):
    """Safely quit a single driver instance."""
    try:
        driver.quit()
        logger.info("Driver closed cleanly.")
    except Exception as e:
        logger.warning("Error closing driver: %s", e)


def cleanup_all():
    """Force-close all tracked drivers. Call this for immediate cleanup."""
    for d in _active_drivers:
        _quit_driver(d)
    _active_drivers.clear()
