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
PAGE_TIMEOUT = int(os.getenv("PAGE_TIMEOUT", "45000"))

SUCCESS_REQUIRED = int(os.getenv("SUCCESS_REQUIRED", "2"))
FAIL_REQUIRED = int(os.getenv("FAIL_REQUIRED", "2"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("dmsu-monitor")


# =========================================================
# ГЛОБАЛЬНІ ЗМІННІ
# =========================================================

browser = None

current_state = None

success_count = 0
fail_count = 0

last_check_result = None
last_check_time = None


# =========================================================
# TELEGRAM
# =========================================================

async def send_telegram(message, chat_id=None, keyboard=False):

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не заданий!")
        return False

    target_chat_id = chat_id or CHAT_ID

    if not target_chat_id:
        logger.error("CHAT_ID не заданий!")
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    data = {
        "chat_id": target_chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    # Кнопка "Перевірити зараз"
    if keyboard:

        data["reply_markup"] = {
            "keyboard": [
                [
                    {
                        "text": "🔍 Перевірити зараз"
                    }
                ]
            ],
            "resize_keyboard": True,
            "is_persistent": True,
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

                    return True

                text = await response.text()

                logger.error(
                    "Telegram HTTP %s: %s",
                    response.status,
                    text,
                )

                return False

    except Exception as e:

        logger.error(
            "Помилка Telegram: %s",
            e,
        )

        return False


# =========================================================
# ПЕРЕВІРКА САЙТУ
# =========================================================

async def check_site():

    global browser

    if browser is None:

        logger.error(
            "Browser ще не запущений."
        )

        return {
            "working": False,
            "status": None,
            "indicators": [],
            "error": "Browser не запущений",
        }

    page = await browser.new_page(

        viewport={
            "width": 1440,
            "height": 900,
        },

        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
    )

    try:

        logger.info(
            "Відкриваю %s",
            SITE_URL,
        )

        response = await page.goto(
            SITE_URL,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT,
        )

        # -------------------------------------------------
        # HTTP RESPONSE
        # -------------------------------------------------

        if response is None:

            logger.warning(
                "Сайт не повернув HTTP response."
            )

            return {
                "working": False,
                "status": None,
                "indicators": [],
                "error": "HTTP response відсутній",
            }

        status = response.status

        logger.info(
            "HTTP status: %s",
            status,
        )

        # -------------------------------------------------
        # HTTP ERROR
        # -------------------------------------------------

        if status >= 400:

            logger.warning(
                "HTTP помилка: %s",
                status,
            )

            return {
                "working": False,
                "status": status,
                "indicators": [],
                "error": f"HTTP {status}",
            }

        # -------------------------------------------------
        # ЧЕКАЄМО JAVASCRIPT
        # -------------------------------------------------

        try:

            await page.wait_for_load_state(
                "networkidle",
                timeout=15000,
            )

        except Exception:

            logger.info(
                "networkidle не настав."
            )

        # Додатковий час для JavaScript

        await page.wait_for_timeout(3000)

        # -------------------------------------------------
        # ОТРИМУЄМО ТЕКСТ
        # -------------------------------------------------

        body_text = await page.locator(
            "body"
        ).inner_text()

        body_text_lower = body_text.lower()

        logger.info(
            "Текст сторінки: %d символів",
            len(body_text),
        )

        # -------------------------------------------------
        # ІНДИКАТОРИ
        # -------------------------------------------------

        indicators_list = [

            "електронна черга",

            "запис онлайн",

            "оберіть регіон",

            "оберіть область",

            "територіальний підрозділ",

            "дата",

            "час",

        ]

        found = []

        for indicator in indicators_list:

            if indicator in body_text_lower:

                found.append(indicator)

        logger.info(
            "Знайдені індикатори: %s",
            found,
        )

        # -------------------------------------------------
        # ПЕРЕВІРКА
        # -------------------------------------------------

        if len(found) >= 2:

            logger.info(
                "🟢 СЕРВІС ЧЕРГИ ПРАЦЮЄ"
            )

            return {
                "working": True,
                "status": status,
                "indicators": found,
                "error": None,
            }

        # -------------------------------------------------
        # ДОДАТКОВА ПЕРЕВІРКА HTML
        # -------------------------------------------------

        html = await page.content()

        html_lower = html.lower()

        html_indicators = [

            "cherga",

            "dmsu",

            "queue",

            "region",

        ]

        html_found = []

        for indicator in html_indicators:

            if indicator in html_lower:

                html_found.append(indicator)

        logger.info(
            "HTML індикатори: %s",
            html_found,
        )

        if len(html_found) >= 2:

            logger.info(
                "🟢 Сервіс підтверджено через HTML."
            )

            return {
                "working": True,
                "status": status,
                "indicators": (
                    found +
                    [
                        f"HTML:{x}"
                        for x in html_found
                    ]
                ),
                "error": None,
            }

        # -------------------------------------------------
        # НЕ ПІДТВЕРДЖЕНО
        # -------------------------------------------------

        logger.warning(
            "🔴 Сервіс черги не підтверджено."
        )

        return {
            "working": False,
            "status": status,
            "indicators": found,
            "error": (
                "Не знайдено достатньо "
                "індикаторів сервісу"
            ),
        }

    except Exception as e:

        logger.warning(
            "Помилка відкриття сайту: %s",
            e,
        )

        return {
            "working": False,
            "status": None,
            "indicators": [],
            "error": str(e),
        }

    finally:

        await page.close()


# =========================================================
# ФОРМУВАННЯ РЕЗУЛЬТАТУ РУЧНОЇ ПЕРЕВІРКИ
# =========================================================

def format_check_result(result):

    now = datetime.now().strftime(
        "%d.%m.%Y %H:%M:%S"
    )

    status = result["status"]

    indicators = result["indicators"]

    if result["working"]:

        message = (
            "🟢 <b>ЕЛЕКТРОННА ЧЕРГА ДМСУ ДОСТУПНА</b>\n\n"
            f"🌐 {SITE_URL}\n"
            f"🕐 {now}\n"
        )

        if status is not None:

            message += (
                f"📡 HTTP: <code>{status}</code>\n"
            )

        message += (
            f"🔎 Індикаторів знайдено: "
            f"<code>{len(indicators)}</code>\n"
        )

        if indicators:

            message += "\n<b>Знайдено:</b>\n"

            for item in indicators:

                message += f"• {item}\n"

        return message

    else:

        message = (
            "🔴 <b>ЕЛЕКТРОННА ЧЕРГА ДМСУ "
            "НЕДОСТУПНА</b>\n\n"
            f"🌐 {SITE_URL}\n"
            f"🕐 {now}\n"
        )

        if status is not None:

            message += (
                f"📡 HTTP: <code>{status}</code>\n"
            )

        if result["error"]:

            message += (
                f"⚠️ {result['error']}\n"
            )

        if indicators:

            message += (
                "\nЗнайдені індикатори:\n"
            )

            for item in indicators:

                message += f"• {item}\n"

        return message


# =========================================================
# /START
# =========================================================

async def handle_start(chat_id):

    message = (
        "🤖 <b>Монітор електронної черги ДМСУ</b>\n\n"
        "Я автоматично перевіряю доступність "
        "електронної черги.\n\n"
        f"🌐 {SITE_URL}\n"
        f"⏱ Перевірка кожні {CHECK_INTERVAL} сек.\n\n"
        "Натисни кнопку нижче або використай команду:\n"
        "<code>/check</code>"
    )

    await send_telegram(
        message,
        chat_id=chat_id,
        keyboard=True,
    )


# =========================================================
# /CHECK
# =========================================================

async def handle_check(chat_id):

    logger.info(
        "Отримано ручну команду /check"
    )

    # Повідомляємо, що перевірка почалася

    await send_telegram(
        "🔄 <b>Перевіряю електронну чергу ДМСУ...</b>\n"
        "Зачекай кілька секунд.",
        chat_id=chat_id,
    )

    result = await check_site()

    await send_telegram(
        format_check_result(result),
        chat_id=chat_id,
        keyboard=True,
    )


# =========================================================
# TELEGRAM POLLING
# =========================================================

async def telegram_listener():

    if not BOT_TOKEN:

        logger.error(
            "BOT_TOKEN не заданий!"
        )

        return

    logger.info(
        "Telegram listener запущений."
    )

    offset = None

    timeout = aiohttp.ClientTimeout(
        total=40
    )

    async with aiohttp.ClientSession(
        timeout=timeout
    ) as session:

        while True:

            try:

                params = {
                    "timeout": 30,
                    "allowed_updates": [
                        "message"
                    ],
                }

                if offset is not None:

                    params["offset"] = offset

                url = (
                    f"https://api.telegram.org/"
                    f"bot{BOT_TOKEN}/getUpdates"
                )

                async with session.get(
                    url,
                    params=params,
                ) as response:

                    if response.status != 200:

                        logger.error(
                            "Telegram getUpdates HTTP %s",
                            response.status,
                        )

                        await asyncio.sleep(5)

                        continue

                    data = await response.json()

                if not data.get("ok"):

                    logger.error(
                        "Telegram API error: %s",
                        data,
                    )

                    await asyncio.sleep(5)

                    continue

                updates = data.get(
                    "result",
                    []
                )

                for update in updates:

                    offset = (
                        update["update_id"] + 1
                    )

                    message = update.get(
                        "message"
                    )

                    if not message:

                        continue

                    chat = message.get(
                        "chat",
                        {}
                    )

                    chat_id = chat.get(
                        "id"
                    )

                    text = message.get(
                        "text",
                        ""
                    ).strip()

                    if not chat_id:

                        continue

                    logger.info(
                        "Telegram команда: %s",
                        text,
                    )

                    # -------------------------------------
                    # /START
                    # -------------------------------------

                    if text == "/start":

                        await handle_start(
                            chat_id
                        )

                    # -------------------------------------
                    # /CHECK
                    # -------------------------------------

                    elif text == "/check":

                        await handle_check(
                            chat_id
                        )

                    # -------------------------------------
                    # КНОПКА
                    # -------------------------------------

                    elif (
                        text ==
                        "🔍 Перевірити зараз"
                    ):

                        await handle_check(
                            chat_id
                        )

            except asyncio.CancelledError:

                raise

            except Exception as e:

                logger.error(
                    "Помилка Telegram listener: %s",
                    e,
                )

                await asyncio.sleep(5)


# =========================================================
# АВТОМАТИЧНИЙ МОНІТОРИНГ
# =========================================================

async def automatic_monitor():

    global current_state
    global success_count
    global fail_count
    global last_check_result
    global last_check_time

    logger.info(
        "Автоматичний моніторинг запущений."
    )

    while True:

        started = asyncio.get_event_loop().time()

        try:

            result = await check_site()

            last_check_result = result

            last_check_time = datetime.now()

            # =============================================
            # САЙТ ПРАЦЮЄ
            # =============================================

            if result["working"]:

                success_count += 1
                fail_count = 0

                logger.info(
                    "Успішна перевірка: %d/%d",
                    success_count,
                    SUCCESS_REQUIRED,
                )

                if (
                    success_count >= SUCCESS_REQUIRED
                    and current_state is not True
                ):

                    previous_state = current_state

                    current_state = True

                    now = datetime.now().strftime(
                        "%d.%m.%Y %H:%M:%S"
                    )

                    if previous_state is False:

                        message = (
                            "🟢 <b>ЕЛЕКТРОННА ЧЕРГА "
                            "ДМСУ ЗАПРАЦЮВАЛА!</b>\n\n"
                            f"🌐 {SITE_URL}\n"
                            f"🕐 {now}\n\n"
                            "Сервіс знову доступний."
                        )

                    else:

                        message = (
                            "🟢 <b>ЕЛЕКТРОННА ЧЕРГА "
                            "ДМСУ ДОСТУПНА</b>\n\n"
                            f"🌐 {SITE_URL}\n"
                            f"🕐 {now}"
                        )

                    await send_telegram(
                        message,
                        keyboard=True,
                    )

            # =============================================
            # САЙТ НЕ ПРАЦЮЄ
            # =============================================

            else:

                fail_count += 1
                success_count = 0

                logger.info(
                    "Невдала перевірка: %d/%d",
                    fail_count,
                    FAIL_REQUIRED,
                )

                if (
                    fail_count >= FAIL_REQUIRED
                    and current_state is not False
                ):

                    previous_state = current_state

                    current_state = False

                    now = datetime.now().strftime(
                        "%d.%m.%Y %H:%M:%S"
                    )

                    # Не повідомляємо про падіння,
                    # якщо монітор просто стартував,
                    # а сайт уже був недоступний.

                    if previous_state is True:

                        message = (
                            "🔴 <b>ЕЛЕКТРОННА ЧЕРГА "
                            "ДМСУ НЕДОСТУПНА</b>\n\n"
                            f"🌐 {SITE_URL}\n"
                            f"🕐 {now}\n\n"
                            "Моніторинг продовжується."
                        )

                        await send_telegram(
                            message,
                            keyboard=True,
                        )

        except Exception as e:

            logger.exception(
                "Помилка автоматичного моніторингу: %s",
                e,
            )

        elapsed = (
            asyncio.get_event_loop().time()
            - started
        )

        sleep_time = max(
            1,
            CHECK_INTERVAL - elapsed
        )

        logger.info(
            "Наступна автоматична перевірка "
            "через %.1f сек.",
            sleep_time,
        )

        await asyncio.sleep(
            sleep_time
        )


# =========================================================
# MAIN
# =========================================================

async def main():

    global browser

    logger.info("")
    logger.info("=" * 60)
    logger.info(
        "DMSU QUEUE MONITOR ЗАПУСКАЄТЬСЯ"
    )
    logger.info("=" * 60)
    logger.info(
        "URL: %s",
        SITE_URL,
    )
    logger.info(
        "Інтервал: %s сек.",
        CHECK_INTERVAL,
    )
    logger.info(
        "Успішних перевірок: %s",
        SUCCESS_REQUIRED,
    )
    logger.info(
        "Невдалих перевірок: %s",
        FAIL_REQUIRED,
    )
    logger.info("=" * 60)
    logger.info("")

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

        logger.info(
            "Chromium успішно запущений."
        )

        # Запускаємо одночасно:
        #
        # 1. Автоматичний моніторинг
        # 2. Telegram listener

        monitor_task = asyncio.create_task(
            automatic_monitor()
        )

        telegram_task = asyncio.create_task(
            telegram_listener()
        )

        try:

            await asyncio.gather(
                monitor_task,
                telegram_task,
            )

        finally:

            monitor_task.cancel()
            telegram_task.cancel()

            await asyncio.gather(
                monitor_task,
                telegram_task,
                return_exceptions=True,
            )

            await browser.close()


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
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