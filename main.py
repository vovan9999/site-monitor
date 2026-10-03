import os
import asyncio
import logging
from datetime import datetime

import aiohttp
from playwright.async_api import async_playwright

# ============================================================
# НАЛАШТУВАННЯ
# ============================================================

SITE_URL = "https://cherga.dmsu.gov.ua/"

# Яку область перевіряти.
# Можна змінити на потрібну тобі.
CHECK_REGION = os.getenv(
    "CHECK_REGION",
    "Вінницька область"
)

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "15"))
PAGE_TIMEOUT = int(os.getenv("PAGE_TIMEOUT", "45000"))

# Скільки успішних/невдалих перевірок поспіль потрібно
# для зміни стану.
SUCCESS_REQUIRED = int(os.getenv("SUCCESS_REQUIRED", "2"))
FAIL_REQUIRED = int(os.getenv("FAIL_REQUIRED", "2"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("dmsu-monitor")

# ============================================================
# ГЛОБАЛЬНІ ЗМІННІ
# ============================================================

browser = None

current_state = None

success_count = 0
fail_count = 0

last_check_result = None
last_check_time = None


# ============================================================
# TELEGRAM
# ============================================================

async def send_telegram(message, chat_id=None, keyboard=False):
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не заданий!")
        return False

    target_chat_id = chat_id or CHAT_ID

    if not target_chat_id:
        logger.error("CHAT_ID не заданий!")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    data = {
        "chat_id": target_chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    if keyboard:
        data["reply_markup"] = {
            "inline_keyboard": [
                [
                    {
                        "text": "🚀 ВІДКРИТИ ЕЛЕКТРОННУ ЧЕРГУ",
                        "url": SITE_URL,
                    }
                ],
                [
                    {
                        "text": "🔍 Перевірити зараз",
                        "callback_data": "check",
                    }
                ],
            ]
        }

    try:
        timeout = aiohttp.ClientTimeout(total=15)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=data) as response:

                if response.status == 200:
                    logger.info("Telegram повідомлення відправлено.")
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


# ============================================================
# ДОПОМІЖНІ ФУНКЦІЇ ДЛЯ СЕЛЕКТІВ
# ============================================================

async def find_region_select(page):
    """
    Шукаємо select, який відповідає області.
    """

    selects = page.locator("select")

    count = await selects.count()

    logger.info(
        "Знайдено select елементів: %s",
        count,
    )

    for i in range(count):

        select = selects.nth(i)

        try:
            options = await select.locator("option").all_inner_texts()

            options_text = " | ".join(options).lower()

            logger.info(
                "SELECT #%s: %s",
                i,
                options[:10],
            )

            if (
                "область" in options_text
                or "регіон" in options_text
                or "вінниць" in options_text
            ):
                return select

        except Exception:
            continue

    # Якщо не вдалося визначити — беремо перший select
    if count > 0:
        logger.info(
            "Не вдалося визначити select області. "
            "Використовую перший."
        )

        return selects.nth(0)

    return None


async def find_department_select(page):
    """
    Шукаємо select територіального підрозділу.
    """

    selects = page.locator("select")

    count = await selects.count()

    for i in range(count):

        select = selects.nth(i)

        try:

            # Перевіряємо aria-label
            aria = await select.get_attribute("aria-label")

            if aria and (
                "територ" in aria.lower()
                or "підрозділ" in aria.lower()
            ):
                return select

            # Перевіряємо placeholder
            placeholder = await select.get_attribute(
                "placeholder"
            )

            if placeholder and (
                "територ" in placeholder.lower()
                or "підрозділ" in placeholder.lower()
            ):
                return select

        except Exception:
            continue

    # Якщо є мінімум два select —
    # другий зазвичай є підрозділом.
    if count >= 2:
        return selects.nth(1)

    return None


async def get_real_options(select):
    """
    Повертає реальні доступні опції select,
    виключаючи службові placeholder-и.
    """

    if select is None:
        return []

    try:
        options = await select.locator("option").evaluate_all(
            """
            options => options.map(o => ({
                text: (o.textContent || '').trim(),
                value: o.value,
                disabled: o.disabled
            }))
            """
        )

        real_options = []

        for option in options:

            text = option["text"].strip()
            value = option["value"].strip()

            if option["disabled"]:
                continue

            if not text:
                continue

            text_lower = text.lower()

            # Виключаємо стандартні placeholder-и
            placeholders = [
                "оберіть",
                "обрати",
                "виберіть",
                "вибрати",
                "територіальний підрозділ",
                "область",
                "регіон",
            ]

            if any(
                p in text_lower
                for p in placeholders
            ):
                continue

            # Порожні значення також не потрібні
            if not value:
                continue

            real_options.append(
                {
                    "text": text,
                    "value": value,
                }
            )

        return real_options

    except Exception as e:

        logger.warning(
            "Не вдалося прочитати options: %s",
            e,
        )

        return []


# ============================================================
# ОСНОВНА ПЕРЕВІРКА САЙТУ
# ============================================================

async def check_site():
    global browser

    if browser is None:

        logger.error(
            "Browser ще не запущений."
        )

        return {
            "working": False,
            "status": None,
            "region": CHECK_REGION,
            "departments": [],
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
            "=================================================="
        )

        logger.info(
            "Перевірка електронної черги ДМСУ"
        )

        logger.info(
            "Область: %s",
            CHECK_REGION,
        )

        logger.info(
            "Відкриваю %s",
            SITE_URL,
        )

        response = await page.goto(
            SITE_URL,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT,
        )

        if response is None:

            return {
                "working": False,
                "status": None,
                "region": CHECK_REGION,
                "departments": [],
                "error": "HTTP response відсутній",
            }

        status = response.status

        logger.info(
            "HTTP status: %s",
            status,
        )

        if status >= 400:

            return {
                "working": False,
                "status": status,
                "region": CHECK_REGION,
                "departments": [],
                "error": f"HTTP {status}",
            }

        # Чекаємо завантаження JS
        try:

            await page.wait_for_load_state(
                "networkidle",
                timeout=15000,
            )

        except Exception:

            logger.info(
                "networkidle не настав."
            )

        # Невелика додаткова пауза
        await page.wait_for_timeout(2000)

        # ----------------------------------------------------
        # ШУКАЄМО SELECT ОБЛАСТІ
        # ----------------------------------------------------

        region_select = await find_region_select(page)

        if region_select is None:

            logger.warning(
                "Select області не знайдений."
            )

            return {
                "working": False,
                "status": status,
                "region": CHECK_REGION,
                "departments": [],
                "error": (
                    "Не знайдено поле вибору області"
                ),
            }

        logger.info(
            "Select області знайдений."
        )

        # ----------------------------------------------------
        # ВИБИРАЄМО ОБЛАСТЬ
        # ----------------------------------------------------

        selected = False

        try:

            await region_select.select_option(
                label=CHECK_REGION
            )

            selected = True

            logger.info(
                "Область вибрана через label: %s",
                CHECK_REGION,
            )

        except Exception as e:

            logger.info(
                "Не вдалося вибрати label: %s",
                e,
            )

            # Спроба знайти option вручну
            try:

                options = await region_select.locator(
                    "option"
                ).all()

                for option in options:

                    text = (
                        await option.inner_text()
                    ).strip()

                    if CHECK_REGION.lower() in text.lower():

                        value = await option.get_attribute(
                            "value"
                        )

                        if value:

                            await region_select.select_option(
                                value=value
                            )

                            selected = True

                            logger.info(
                                "Область вибрана через value: %s",
                                value,
                            )

                            break

            except Exception as e2:

                logger.warning(
                    "Помилка вибору області: %s",
                    e2,
                )

        if not selected:

            return {
                "working": False,
                "status": status,
                "region": CHECK_REGION,
                "departments": [],
                "error": (
                    f"Не вдалося вибрати область "
                    f"«{CHECK_REGION}»"
                ),
            }

        # ----------------------------------------------------
        # ЧЕКАЄМО ОНОВЛЕННЯ ПІДРОЗДІЛІВ
        # ----------------------------------------------------

        logger.info(
            "Чекаю завантаження територіальних підрозділів..."
        )

        department_select = await find_department_select(
            page
        )

        if department_select is None:

            logger.warning(
                "Select територіального підрозділу "
                "не знайдений."
            )

            return {
                "working": False,
                "status": status,
                "region": CHECK_REGION,
                "departments": [],
                "error": (
                    "Поле територіального "
                    "підрозділу не знайдене"
                ),
            }

        departments = []

        # Чекаємо максимум 15 секунд
        for attempt in range(15):

            await page.wait_for_timeout(1000)

            departments = await get_real_options(
                department_select
            )

            logger.info(
                "Спроба %d/15: знайдено підрозділів: %d",
                attempt + 1,
                len(departments),
            )

            if departments:
                break

        # ----------------------------------------------------
        # ФІНАЛЬНА ПЕРЕВІРКА
        # ----------------------------------------------------

        if not departments:

            logger.warning(
                "🔴 ПІДРОЗДІЛИ НЕ ЗАВАНТАЖИЛИСЯ"
            )

            return {
                "working": False,
                "status": status,
                "region": CHECK_REGION,
                "departments": [],
                "error": (
                    "Після вибору області "
                    "територіальні підрозділи "
                    "не завантажилися"
                ),
            }

        logger.info(
            "🟢 СЕРВІС ПРАЦЮЄ!"
        )

        logger.info(
            "Знайдено підрозділів: %d",
            len(departments),
        )

        for department in departments[:10]:

            logger.info(
                "  • %s",
                department["text"],
            )

        return {
            "working": True,
            "status": status,
            "region": CHECK_REGION,
            "departments": departments,
            "error": None,
        }

    except Exception as e:

        logger.exception(
            "Помилка перевірки сайту: %s",
            e,
        )

        return {
            "working": False,
            "status": None,
            "region": CHECK_REGION,
            "departments": [],
            "error": str(e),
        }

    finally:

        await page.close()


# ============================================================
# ФОРМУВАННЯ ПОВІДОМЛЕННЯ
# ============================================================

def format_check_result(result):

    now = datetime.now().strftime(
        "%d.%m.%Y %H:%M:%S"
    )

    status = result.get("status")
    region = result.get("region")
    departments = result.get(
        "departments",
        []
    )

    # --------------------------------------------------------
    # ПРАЦЮЄ
    # --------------------------------------------------------

    if result["working"]:

        message = (
            "🟢 <b>ЕЛЕКТРОННА ЧЕРГА ДМСУ ПРАЦЮЄ!</b>\n\n"
            "Сервіс реально завантажив "
            "територіальні підрозділи.\n\n"
            f"🌐 <a href=\"{SITE_URL}\">Відкрити електронну чергу</a>\n"
            f"📍 Область: <b>{region}</b>\n"
            f"🏢 Підрозділів: <b>{len(departments)}</b>\n"
            f"🕐 {now}\n"
        )

        if status is not None:

            message += (
                f"📡 HTTP: <code>{status}</code>\n"
            )

        if departments:

            message += "\n<b>Перші доступні підрозділи:</b>\n"

            for department in departments[:10]:

                message += (
                    f"• {department['text']}\n"
                )

        return message

    # --------------------------------------------------------
    # НЕ ПРАЦЮЄ
    # --------------------------------------------------------

    message = (
        "🔴 <b>ЕЛЕКТРОННА ЧЕРГА ДМСУ НЕДОСТУПНА</b>\n\n"
        f"🌐 {SITE_URL}\n"
        f"📍 Область: <b>{region}</b>\n"
        f"🕐 {now}\n"
    )

    if status is not None:

        message += (
            f"📡 HTTP: <code>{status}</code>\n"
        )

    if result.get("error"):

        message += (
            f"⚠️ {result['error']}\n"
        )

    return message


# ============================================================
# START
# ============================================================

async def handle_start(chat_id):

    message = (
        "🤖 <b>Монітор електронної черги ДМСУ</b>\n\n"
        "Я автоматично перевіряю не просто "
        "завантаження сайту, а реальну роботу "
        "електронної черги.\n\n"
        f"📍 Перевіряється: <b>{CHECK_REGION}</b>\n"
        f"⏱ Перевірка кожні <b>{CHECK_INTERVAL} сек.</b>\n\n"
        "Як тільки після вибору області "
        "з'являться територіальні підрозділи, "
        "я одразу повідомлю тебе.\n\n"
        "Також можна виконати ручну перевірку."
    )

    await send_telegram(
        message,
        chat_id=chat_id,
        keyboard=True,
    )


# ============================================================
# РУЧНА ПЕРЕВІРКА
# ============================================================

async def handle_check(chat_id):

    logger.info(
        "Отримано ручну перевірку."
    )

    await send_telegram(
        "🔄 <b>Перевіряю електронну чергу...</b>\n\n"
        f"📍 Область: {CHECK_REGION}\n"
        "Зачекай кілька секунд.",
        chat_id=chat_id,
    )

    result = await check_site()

    await send_telegram(
        format_check_result(result),
        chat_id=chat_id,
        keyboard=True,
    )


# ============================================================
# TELEGRAM LISTENER
# ============================================================

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
                        "message",
                        "callback_query",
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

                    # ----------------------------------------
                    # CALLBACK BUTTON
                    # ----------------------------------------

                    callback = update.get(
                        "callback_query"
                    )

                    if callback:

                        callback_chat_id = (
                            callback
                            .get("message", {})
                            .get("chat", {})
                            .get("id")
                        )

                        callback_id = callback.get(
                            "id"
                        )

                        if callback_chat_id:

                            # Підтверджуємо натискання
                            try:

                                answer_url = (
                                    f"https://api.telegram.org/"
                                    f"bot{BOT_TOKEN}/answerCallbackQuery"
                                )

                                await session.post(
                                    answer_url,
                                    json={
                                        "callback_query_id":
                                            callback_id
                                    },
                                )

                            except Exception:
                                pass

                            await handle_check(
                                callback_chat_id
                            )

                        continue

                    # ----------------------------------------
                    # MESSAGE
                    # ----------------------------------------

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
                        "Telegram: %s",
                        text,
                    )

                    if text == "/start":

                        await handle_start(
                            chat_id
                        )

                    elif text == "/check":

                        await handle_check(
                            chat_id
                        )

                    elif text == "🔍 Перевірити зараз":

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


# ============================================================
# АВТОМАТИЧНИЙ МОНІТОРИНГ
# ============================================================

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

        started = (
            asyncio.get_event_loop().time()
        )

        try:

            result = await check_site()

            last_check_result = result
            last_check_time = datetime.now()

            # ================================================
            # СЕРВІС ПРАЦЮЄ
            # ================================================

            if result["working"]:

                success_count += 1
                fail_count = 0

                logger.info(
                    "🟢 Успішна перевірка %d/%d",
                    success_count,
                    SUCCESS_REQUIRED,
                )

                if (
                    success_count >= SUCCESS_REQUIRED
                    and current_state is not True
                ):

                    previous_state = (
                        current_state
                    )

                    current_state = True

                    now = datetime.now().strftime(
                        "%d.%m.%Y %H:%M:%S"
                    )

                    if previous_state is False:

                        message = (
                            "🚨🚨🚨 <b>ЕЛЕКТРОННА ЧЕРГА "
                            "ДМСУ ЗАПРАЦЮВАЛА!</b> 🚨🚨🚨\n\n"
                            f"📍 Область: <b>{CHECK_REGION}</b>\n"
                            f"🏢 Знайдено підрозділів: "
                            f"<b>{len(result['departments'])}</b>\n"
                            f"🕐 {now}\n\n"
                            "🔥 <b>МОЖНА ЗАХОДИТИ І РЕЄСТРУВАТИСЯ!</b>\n\n"
                            f"👉 <a href=\"{SITE_URL}\">"
                            "ВІДКРИТИ ЕЛЕКТРОННУ ЧЕРГУ"
                            "</a>"
                        )

                    else:

                        message = (
                            "🟢 <b>ЕЛЕКТРОННА ЧЕРГА "
                            "ДМСУ ДОСТУПНА</b>\n\n"
                            f"📍 {CHECK_REGION}\n"
                            f"🕐 {now}\n\n"
                            f"👉 <a href=\"{SITE_URL}\">"
                            "Відкрити електронну чергу"
                            "</a>"
                        )

                    await send_telegram(
                        message,
                        keyboard=True,
                    )

            # ================================================
            # СЕРВІС НЕ ПРАЦЮЄ
            # ================================================

            else:

                fail_count += 1
                success_count = 0

                logger.info(
                    "🔴 Невдала перевірка %d/%d",
                    fail_count,
                    FAIL_REQUIRED,
                )

                if (
                    fail_count >= FAIL_REQUIRED
                    and current_state is not False
                ):

                    previous_state = (
                        current_state
                    )

                    current_state = False

                    now = datetime.now().strftime(
                        "%d.%m.%Y %H:%M:%S"
                    )

                    # Повідомляємо про падіння,
                    # тільки якщо перед цим сайт працював.
                    if previous_state is True:

                        message = (
                            "🔴 <b>ЕЛЕКТРОННА ЧЕРГА "
                            "ДМСУ НЕДОСТУПНА</b>\n\n"
                            f"📍 {CHECK_REGION}\n"
                            f"🕐 {now}\n\n"
                            "Територіальні підрозділи "
                            "перестали завантажуватися.\n\n"
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


# ============================================================
# MAIN
# ============================================================

async def main():

    global browser

    logger.info("")
    logger.info(
        "=" * 60
    )
    logger.info(
        "DMSU QUEUE MONITOR ЗАПУСКАЄТЬСЯ"
    )
    logger.info(
        "=" * 60
    )

    logger.info(
        "URL: %s",
        SITE_URL,
    )

    logger.info(
        "Область: %s",
        CHECK_REGION,
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

    logger.info(
        "=" * 60
    )

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


# ============================================================
# START APPLICATION
# ============================================================

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
            e,
        )