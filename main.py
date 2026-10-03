import os
import asyncio
import logging
from datetime import datetime

import aiohttp
from playwright.async_api import async_playwright


# =========================================================
# НАЛАШТУВАННЯ
# =========================================================

SITE_URL = "https://cherga.dmsu.gov.ua/"

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))

PAGE_TIMEOUT = int(
    os.getenv("PAGE_TIMEOUT", "45000")
)

SUCCESS_REQUIRED = int(
    os.getenv("SUCCESS_REQUIRED", "2")
)

FAIL_REQUIRED = int(
    os.getenv("FAIL_REQUIRED", "2")
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")


# =========================================================
# ЛОГИ
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("dmsu-monitor")


# =========================================================
# TELEGRAM
# =========================================================

async def send_telegram(message):

    if not BOT_TOKEN or not CHAT_ID:
        logger.error(
            "BOT_TOKEN або CHAT_ID не задані!"
        )
        return

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    data = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:

        timeout = aiohttp.ClientTimeout(total=15)

        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            async with session.post(
                url,
                json=data
            ) as response:

                if response.status == 200:

                    logger.info(
                        "Telegram повідомлення відправлено."
                    )

                else:

                    text = await response.text()

                    logger.error(
                        "Telegram HTTP %s: %s",
                        response.status,
                        text,
                    )

    except Exception as e:

        logger.error(
            "Помилка Telegram: %s",
            e,
        )


# =========================================================
# ПЕРЕВІРКА САЙТУ
# =========================================================

async def check_site(browser):

    page = await browser.new_page(
        viewport={
            "width": 1440,
            "height": 900
        },

        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        )
    )

    try:

        logger.info(
            "Відкриваю %s",
            SITE_URL
        )

        response = await page.goto(
            SITE_URL,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT,
        )

        if response is None:

            logger.warning(
                "Сайт не повернув HTTP response."
            )

            return False

        status = response.status

        logger.info(
            "HTTP status: %s",
            status
        )

        # -------------------------------------------------
        # HTTP ПОМИЛКА
        # -------------------------------------------------

        if status >= 400:

            logger.warning(
                "HTTP помилка: %s",
                status
            )

            return False

        # -------------------------------------------------
        # ЧЕКАЄМО JAVASCRIPT
        # -------------------------------------------------

        try:

            await page.wait_for_load_state(
                "networkidle",
                timeout=15000
            )

        except Exception:

            logger.info(
                "networkidle не настав. "
                "Перевіряємо сторінку далі."
            )

        # -------------------------------------------------
        # ДОДАТКОВО ДАЄМО JS ЧАС ВІДПРАЦЮВАТИ
        # -------------------------------------------------

        await page.wait_for_timeout(3000)

        # -------------------------------------------------
        # ОТРИМУЄМО ТЕКСТ СТОРІНКИ
        # -------------------------------------------------

        body_text = await page.locator(
            "body"
        ).inner_text()

        body_text_lower = body_text.lower()

        logger.info(
            "Отримано текст сторінки: %d символів",
            len(body_text)
        )

        # -------------------------------------------------
        # ПЕРЕВІРКА, ЩО ЦЕ САМЕ СЕРВІС ЧЕРГИ
        # -------------------------------------------------

        indicators = [

            "електронна черга",

            "запис онлайн",

            "оберіть регіон",

            "оберіть область",

            "територіальний підрозділ",

            "дата",

            "час",

        ]

        found = []

        for indicator in indicators:

            if indicator in body_text_lower:

                found.append(indicator)

        logger.info(
            "Знайдені індикатори: %s",
            found
        )

        # -------------------------------------------------
        # ГОЛОВНА УМОВА
        # -------------------------------------------------

        # Вважаємо сайт працюючим, якщо знайдено
        # достатню кількість характерних елементів.

        if len(found) >= 2:

            logger.info(
                "🟢 СЕРВІС ЧЕРГИ ПРАЦЮЄ"
            )

            return True

        # -------------------------------------------------
        # ДОДАТКОВА ПЕРЕВІРКА HTML
        # -------------------------------------------------

        html = await page.content()

        html_lower = html.lower()

        # Якщо сторінка містить характерні елементи
        # електронної черги.

        html_indicators = [

            "cherga",

            "dmsu",

            "queue",

            "region",

        ]

        html_found = sum(
            1
            for x in html_indicators
            if x in html_lower
        )

        if html_found >= 2:

            logger.info(
                "🟢 Сторінка містить індикатори сервісу."
            )

            return True

        logger.warning(
            "🔴 Сервіс черги не підтверджено."
        )

        return False

    except Exception as e:

        logger.warning(
            "Помилка відкриття сайту: %s",
            e
        )

        return False

    finally:

        await page.close()


# =========================================================
# МОНІТОРИНГ
# =========================================================

async def monitor():

    logger.info("")
    logger.info("=" * 60)
    logger.info("DMSU QUEUE MONITOR")
    logger.info("=" * 60)
    logger.info(
        "URL: %s",
        SITE_URL
    )
    logger.info(
        "Інтервал: %s сек.",
        CHECK_INTERVAL
    )
    logger.info(
        "Успішних перевірок: %s",
        SUCCESS_REQUIRED
    )
    logger.info(
        "Невдалих перевірок: %s",
        FAIL_REQUIRED
    )
    logger.info("=" * 60)
    logger.info("")

    current_state = None

    success_count = 0
    fail_count = 0

    async with async_playwright() as p:

        browser = await p.chromium.launch(

            headless=True,

            args=[

                "--no-sandbox",

                "--disable-setuid-sandbox",

                "--disable-dev-shm-usage",

                "--disable-gpu",

                "--disable-blink-features=AutomationControlled",

            ],
        )

        try:

            while True:

                started = asyncio.get_event_loop().time()

                result = await check_site(
                    browser
                )

                # =========================================
                # САЙТ ПРАЦЮЄ
                # =========================================

                if result:

                    success_count += 1
                    fail_count = 0

                    logger.info(
                        "Успішна перевірка %d/%d",
                        success_count,
                        SUCCESS_REQUIRED
                    )

                    if (
                        success_count >= SUCCESS_REQUIRED
                        and current_state is not True
                    ):

                        old_state = current_state

                        current_state = True

                        now = datetime.now().strftime(
                            "%d.%m.%Y %H:%M:%S"
                        )

                        if old_state is False:

                            message = (
                                "🟢 <b>ЕЛЕКТРОННА ЧЕРГА ДМСУ "
                                "ЗАПРАЦЮВАЛА!</b>\n\n"
                                f"🌐 {SITE_URL}\n"
                                f"🕐 {now}\n\n"
                                "Сервіс успішно завантажується."
                            )

                        else:

                            message = (
                                "🟢 <b>ЕЛЕКТРОННА ЧЕРГА ДМСУ "
                                "ДОСТУПНА</b>\n\n"
                                f"🌐 {SITE_URL}\n"
                                f"🕐 {now}"
                            )

                        await send_telegram(
                            message
                        )

                # =========================================
                # САЙТ НЕ ПРАЦЮЄ
                # =========================================

                else:

                    fail_count += 1
                    success_count = 0

                    logger.info(
                        "Невдала перевірка %d/%d",
                        fail_count,
                        FAIL_REQUIRED
                    )

                    if (
                        fail_count >= FAIL_REQUIRED
                        and current_state is not False
                    ):

                        old_state = current_state

                        current_state = False

                        now = datetime.now().strftime(
                            "%d.%m.%Y %H:%M:%S"
                        )

                        # Не надсилаємо "впав" одразу після
                        # запуску монітора, якщо сайт вже був
                        # недоступний.

                        if old_state is True:

                            message = (
                                "🔴 <b>ЕЛЕКТРОННА ЧЕРГА "
                                "ДМСУ НЕДОСТУПНА</b>\n\n"
                                f"🌐 {SITE_URL}\n"
                                f"🕐 {now}\n\n"
                                "Моніторинг продовжується."
                            )

                            await send_telegram(
                                message
                            )

                # =========================================
                # ЧЕКАЄМО ДО НАСТУПНОЇ ПЕРЕВІРКИ
                # =========================================

                elapsed = (
                    asyncio.get_event_loop().time()
                    - started
                )

                sleep_time = max(
                    1,
                    CHECK_INTERVAL - elapsed
                )

                logger.info(
                    "Наступна перевірка через %.1f сек.",
                    sleep_time
                )

                await asyncio.sleep(
                    sleep_time
                )

        finally:

            await browser.close()


# =========================================================
# ЗАПУСК
# =========================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            monitor()
        )

    except KeyboardInterrupt:

        logger.info(
            "Моніторинг зупинено."
        )

    except Exception as e:

        logger.exception(
            "Критична помилка: %s",
            e
        )