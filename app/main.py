import os
import hmac
import hashlib
import time
import re
import asyncio
import aiohttp
import aiosqlite

from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ===================== CONFIG =====================

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
WAYFORPAY_MERCHANT = os.getenv("WAYFORPAY_MERCHANT")
WAYFORPAY_SECRET = os.getenv("WAYFORPAY_SECRET")
MERCHANT_DOMAIN = os.getenv("MERCHANT_DOMAIN", "yourdomain.com")

# базові налаштування продукту №1
PRODUCT_ID = int(os.getenv("PRODUCT_ID", "1"))
PRODUCT_NAME = os.getenv("PRODUCT_NAME", "Курс самомасажу")
AMOUNT = float(os.getenv("AMOUNT", "290.00"))   # 290 грн
CURRENCY = os.getenv("CURRENCY", "UAH")

SERVICE_URL = os.getenv("SERVICE_URL")
KEEP_ALIVE_URL = os.getenv("KEEP_ALIVE_URL")

ADMIN_ID = 268351523  # твій Telegram ID

# група підтримки (бот вже доданий туди)
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID", "-5032163085"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID missing")
if not KEEP_ALIVE_URL:
    raise RuntimeError("KEEP_ALIVE_URL missing")

app = FastAPI()

DB_PATH = "database.db"
db: aiosqlite.Connection | None = None

telegram_app = Application.builder().token(BOT_TOKEN).build()

# ===================== DB INIT =====================

async def get_db() -> aiosqlite.Connection:
    global db
    if db is None:
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
    return db


async def init_db():
    conn = await get_db()
    # users
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at INTEGER,
            last_activity INTEGER,
            has_access INTEGER DEFAULT 0
        )
    """)
    # products
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY,
            name TEXT,
            price REAL,
            currency TEXT,
            is_active INTEGER DEFAULT 1
        )
    """)
    # purchases
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            product_id INTEGER,
            amount REAL,
            currency TEXT,
            status TEXT,
            order_ref TEXT UNIQUE,
            created_at INTEGER,
            paid_at INTEGER
        )
    """)
    # access_links
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS access_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            product_id INTEGER,
            invite_link TEXT,
            created_at INTEGER,
            used INTEGER DEFAULT 0
        )
    """)
    # messages (лог саппорта)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            is_admin INTEGER,
            direction TEXT,
            content_type TEXT,
            text TEXT,
            timestamp INTEGER
        )
    """)

    # вставляємо продукт №1, якщо його ще немає
    cur = await conn.execute("SELECT id FROM products WHERE id = ?", (PRODUCT_ID,))
    row = await cur.fetchone()
    if row is None:
        await conn.execute(
            "INSERT INTO products (id, name, price, currency, is_active) VALUES (?, ?, ?, ?, 1)",
            (PRODUCT_ID, PRODUCT_NAME, AMOUNT, CURRENCY)
        )

    await conn.commit()


async def upsert_user(telegram_id: int, username: str | None, first_name: str | None):
    conn = await get_db()
    now = int(time.time())

    await conn.execute("""
        INSERT OR IGNORE INTO users (telegram_id, username, first_name, joined_at, last_activity, has_access)
        VALUES (?, ?, ?, ?, ?, 0)
    """, (telegram_id, username, first_name, now, now))

    await conn.execute("""
        UPDATE users
        SET username = ?, first_name = ?, last_activity = ?
        WHERE telegram_id = ?
    """, (username, first_name, now, telegram_id))

    await conn.commit()


async def mark_access(telegram_id: int, product_id: int, invite_link: str | None = None):
    conn = await get_db()
    now = int(time.time())

    await conn.execute(
        "UPDATE users SET has_access = 1, last_activity = ? WHERE telegram_id = ?",
        (now, telegram_id)
    )

    if invite_link:
        await conn.execute("""
            INSERT INTO access_links (telegram_id, product_id, invite_link, created_at, used)
            VALUES (?, ?, ?, ?, 0)
        """, (telegram_id, product_id, invite_link, now))

    await conn.commit()


async def create_purchase_pending(telegram_id: int, product_id: int, amount: float, currency: str, order_ref: str):
    conn = await get_db()
    now = int(time.time())
    await conn.execute("""
        INSERT INTO purchases (telegram_id, product_id, amount, currency, status, order_ref, created_at, paid_at)
        VALUES (?, ?, ?, ?, 'pending', ?, ?, NULL)
    """, (telegram_id, product_id, amount, currency, order_ref, now))
    await conn.commit()


async def mark_purchase_paid(order_ref: str):
    conn = await get_db()
    now = int(time.time())
    await conn.execute("""
        UPDATE purchases
        SET status = 'approved', paid_at = ?
        WHERE order_ref = ?
    """, (now, order_ref))
    await conn.commit()


async def log_message(telegram_id: int, is_admin: int, direction: str, content_type: str, text: str | None):
    conn = await get_db()
    now = int(time.time())
    await conn.execute("""
        INSERT INTO messages (telegram_id, is_admin, direction, content_type, text, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (telegram_id, is_admin, direction, content_type, text, now))
    await conn.commit()


# ===================== KEEP-ALIVE =====================

async def keep_alive():
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                await session.get(KEEP_ALIVE_URL)
                print("Keep-alive OK")
        except Exception as e:
            print("Keep-alive error:", e)
        await asyncio.sleep(300)


# ===================== WAYFORPAY SIGNATURE HELPERS =====================

def wfp_invoice_signature(payload: dict) -> str:
    parts = [
        payload["merchantAccount"],
        payload["merchantDomainName"],
        payload["orderReference"],
        str(payload["orderDate"]),
        str(payload["amount"]),
        payload["currency"],
    ]

    for n in payload["productName"]:
        parts.append(str(n))
    for c in payload["productCount"]:
        parts.append(str(c))
    for p in payload["productPrice"]:
        parts.append(str(p))

    msg = ";".join(parts)

    return hmac.new(
        WAYFORPAY_SECRET.encode(),
        msg.encode(),
        hashlib.md5
    ).hexdigest()


def wfp_callback_valid(body: dict) -> bool:
    required = [
        "merchantAccount", "orderReference", "amount", "currency",
        "authCode", "cardPan", "transactionStatus", "reasonCode",
        "merchantSignature"
    ]

    if not all(k in body for k in required):
        return False

    parts = [
        body["merchantAccount"],
        body["orderReference"],
        str(body["amount"]),
        body["currency"],
        body["authCode"],
        body["cardPan"],
        body["transactionStatus"],
        body["reasonCode"],
    ]

    msg = ";".join(parts)

    expected = hmac.new(
        WAYFORPAY_SECRET.encode(),
        msg.encode(),
        hashlib.md5
    ).hexdigest()

    return expected == body["merchantSignature"]


def wfp_response_signature(order_ref: str, status: str, ts: int) -> str:
    msg = f"{order_ref};{status};{ts}"
    return hmac.new(
        WAYFORPAY_SECRET.encode(),
        msg.encode(),
        hashlib.md5
    ).hexdigest()


# ===================== STARTUP =====================

@app.on_event("startup")
async def startup_event():
    await telegram_app.initialize()
    await telegram_app.start()
    await init_db()
    asyncio.create_task(keep_alive())


# ===================== HELPERS =====================

async def create_one_time_link(telegram_id: int, product_id: int) -> str:
    invite = await telegram_app.bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        member_limit=1
    )
    link = invite.invite_link
    await mark_access(telegram_id, product_id, link)
    return link


def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID


# ===================== TELEGRAM HANDLERS: /start =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await upsert_user(user.id, user.username, user.first_name)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Оплатити курс", callback_data=f"pay:{PRODUCT_ID}")],
        [InlineKeyboardButton("🧪 Тестова оплата", callback_data=f"testpay:{PRODUCT_ID}")],
    ])

    txt = (
        "Привіт! 👋\n\n"
        "Це бот доступу до курсу самомасажу.\n"
        "Натисни кнопку нижче, щоб отримати доступ.\n\n"
        "<b>Після оплати ти автоматично отримаєш особистий доступ у приватний канал.</b>"
    )

    await update.message.reply_text(txt, reply_markup=keyboard, parse_mode="HTML")


# ===================== /access (повторний доступ) =====================

async def access_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = user.id

    await upsert_user(user.id, user.username, user.first_name)

    conn = await get_db()

    cur = await conn.execute("""
        SELECT COUNT(*) AS c
        FROM purchases
        WHERE telegram_id = ? AND status='approved'
    """, (telegram_id,))
    row = await cur.fetchone()

    if row["c"] == 0:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Оплатити курс", callback_data=f"pay:{PRODUCT_ID}")]
        ])
        await update.message.reply_text(
            "<b>У тебе ще немає активного доступу.</b>\n"
            "Щоб отримати його — оплати курс нижче 👇",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        return

    try:
        link = await create_one_time_link(telegram_id, PRODUCT_ID)

        await update.message.reply_text(
            "🔑 <b>Ось твій новий особистий доступ у канал:</b>\n"
            f"{link}\n\n"
            "Якщо не зможеш зайти — просто повтори /access 🙂",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(
            f"Помилка під час створення доступу:\n<code>{e}</code>",
            parse_mode="HTML"
        )


# ===================== TEST PAYMENT =====================

async def testpay_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    await upsert_user(user.id, user.username, user.first_name)

    data = query.data.split(":")
    product_id = int(data[1]) if len(data) > 1 else PRODUCT_ID

    try:
        link = await create_one_time_link(user.id, product_id)

        await telegram_app.bot.send_message(
            chat_id=user.id,
            text=(
                "🧪 <b>Тестова оплата успішна!</b>\n\n"
                "Ось твій <b>особистий доступ</b> у канал з уроками:\n"
                f"{link}"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        await query.message.reply_text(
            f"Помилка:\n<code>{e}</code>",
            parse_mode="HTML"
        )
        return

    await query.message.reply_text("Готово! 🎉")


# ===================== REAL PAYMENT =====================

async def pay_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    await upsert_user(user.id, user.username, user.first_name)

    data = query.data.split(":")
    product_id = int(data[1]) if len(data) > 1 else PRODUCT_ID

    order_ref = f"order_{product_id}_{user.id}"
    await create_purchase_pending(user.id, product_id, AMOUNT, CURRENCY, order_ref)

    order_date = int(time.time())

    payload = {
        "transactionType": "CREATE_INVOICE",
        "merchantAccount": WAYFORPAY_MERCHANT,
        "merchantDomainName": MERCHANT_DOMAIN,
        "orderReference": order_ref,
        "orderDate": order_date,
        "amount": AMOUNT,
        "currency": CURRENCY,
        "productName": [PRODUCT_NAME],
        "productCount": [1],
        "productPrice": [AMOUNT],
        "language": "UA",
    }

    if SERVICE_URL:
        payload["serviceUrl"] = SERVICE_URL

    payload["merchantSignature"] = wfp_invoice_signature(payload)

    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.wayforpay.com/api", json=payload) as resp:
            data = await resp.json()

    invoice = data.get("invoiceUrl")
    if not invoice:
        await query.message.reply_text("Помилка при створенні інвойсу.")
        return

    txt = (
        "<b>Готово!</b> 🎉\n\n"
        "Оплатіть за посиланням:\n"
        f"{invoice}\n\n"
        "Після оплати бот автоматично видасть особистий доступ."
    )

    await query.message.reply_text(txt, parse_mode="HTML")


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("access", access_cmd))
telegram_app.add_handler(CallbackQueryHandler(pay_cb, pattern=r"^pay:"))
telegram_app.add_handler(CallbackQueryHandler(testpay_cb, pattern=r"^testpay:"))


# ===================== ADMIN: /stats =====================

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    conn = await get_db()
    now = int(time.time())
    day_ago = now - 86400

    cur = await conn.execute("SELECT COUNT(*) AS c FROM users")
    total_users = (await cur.fetchone())["c"]

    cur = await conn.execute("SELECT COUNT(*) AS c FROM purchases WHERE status='approved'")
    total_paid = (await cur.fetchone())["c"]

    cur = await conn.execute("SELECT COALESCE(SUM(amount),0) AS s FROM purchases WHERE status='approved'")
    total_revenue = (await cur.fetchone())["s"]

    cur = await conn.execute("SELECT COUNT(*) AS c FROM users WHERE last_activity >= ?", (day_ago,))
    active_24h = (await cur.fetchone())["c"]

    cur = await conn.execute("""
        SELECT COUNT(*) AS c
        FROM purchases
        WHERE status='approved' AND product_id=?
    """, (PRODUCT_ID,))
    product_buyers = (await cur.fetchone())["c"]

    avg_check = total_revenue / total_paid if total_paid > 0 else 0

    txt = (
        "<b>Статистика бота</b>\n\n"
        f"👥 Усього користувачів: <b>{total_users}</b>\n"
        f"💳 Усього покупців: <b>{total_paid}</b>\n"
        f"💰 Загальний дохід: <b>{round(total_revenue, 2)} {CURRENCY}</b>\n"
        f"📊 Середній чек: <b>{round(avg_check, 2)} {CURRENCY}</b>\n\n"
        f"🔥 Покупців курсу “{PRODUCT_NAME}”: <b>{product_buyers}</b>\n"
        f"⚡ Активних за 24 години: <b>{active_24h}</b>"
    )

    await update.message.reply_text(txt, parse_mode="HTML")


telegram_app.add_handler(CommandHandler("stats", stats_cmd))


# ===================== ADMIN: BROADCASTS (текст) =====================

async def broadcast_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    text = update.message.text.split(" ", 1)
    if len(text) < 2:
        await update.message.reply_text("Напиши текст після команди, наприклад:\n/broadcast_all Привіт! ❤️")
        return
    msg = text[1]

    conn = await get_db()
    cur = await conn.execute("SELECT telegram_id FROM users")
    rows = await cur.fetchall()

    sent = 0
    for row in rows:
        try:
            await telegram_app.bot.send_message(chat_id=row["telegram_id"], text=msg)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            print("broadcast_all error:", e)

    await update.message.reply_text(f"Розсилка надіслана {sent} користувачам.")


async def broadcast_paid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    text = update.message.text.split(" ", 1)
    if len(text) < 2:
        await update.message.reply_text("Напиши текст після команди, наприклад:\n/broadcast_paid Привіт, дякую за покупку! ❤️")
        return
    msg = text[1]

    conn = await get_db()
    cur = await conn.execute("""
        SELECT DISTINCT telegram_id
        FROM purchases
        WHERE status='approved'
    """)
    rows = await cur.fetchall()

    sent = 0
    for row in rows:
        try:
            await telegram_app.bot.send_message(chat_id=row["telegram_id"], text=msg)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            print("broadcast_paid error:", e)

    await update.message.reply_text(f"Розсилка покупцям надіслана {sent} користувачам.")


async def broadcast_nonbuyers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    text = update.message.text.split(" ", 1)
    if len(text) < 2:
        await update.message.reply_text("Напиши текст після команди, наприклад:\n/broadcast_nonbuyers Привіт! Ось спецпропозиція для тебе 💛")
        return
    msg = text[1]

    conn = await get_db()
    cur = await conn.execute("""
        SELECT u.telegram_id
        FROM users u
        LEFT JOIN purchases p ON u.telegram_id = p.telegram_id AND p.status='approved'
        WHERE p.id IS NULL
    """)
    rows = await cur.fetchall()

    sent = 0
    for row in rows:
        try:
            await telegram_app.bot.send_message(chat_id=row["telegram_id"], text=msg)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            print("broadcast_nonbuyers error:", e)

    await update.message.reply_text(f"Розсилка не-покупцям надіслана {sent} користувачам.")


async def broadcast_by_dates_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    # формат: /broadcast_by_dates YYYY-MM-DD YYYY-MM-DD текст...
    parts = update.message.text.split(" ", 3)
    if len(parts) < 4:
        await update.message.reply_text(
            "Формат:\n"
            "/broadcast_by_dates 2023-12-01 2023-12-31 Привіт, це офер для покупців грудня ❤️"
        )
        return

    start_date_str = parts[1]
    end_date_str = parts[2]
    msg = parts[3]

    try:
        start_ts = int(time.mktime(time.strptime(start_date_str, "%Y-%m-%d")))
        end_ts = int(time.mktime(time.strptime(end_date_str, "%Y-%m-%d"))) + 86400
    except Exception:
        await update.message.reply_text("Некоректний формат дати. Використовуй YYYY-MM-DD.")
        return

    conn = await get_db()
    cur = await conn.execute("""
        SELECT DISTINCT telegram_id
        FROM purchases
        WHERE status='approved'
          AND paid_at IS NOT NULL
          AND paid_at >= ? AND paid_at < ?
    """, (start_ts, end_ts))
    rows = await cur.fetchall()

    sent = 0
    for row in rows:
        try:
            await telegram_app.bot.send_message(chat_id=row["telegram_id"], text=msg)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            print("broadcast_by_dates error:", e)

    await update.message.reply_text(
        f"Розсилка покупцям у період {start_date_str}–{end_date_str} надіслана {sent} користувачам."
    )


async def broadcast_inactive_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    # формат: /broadcast_inactive 30 Текст
    parts = update.message.text.split(" ", 2)
    if len(parts) < 3:
        await update.message.reply_text(
            "Формат:\n/broadcast_inactive 30 Привіт! Давно тебе не було 🙂"
        )
        return

    try:
        days = int(parts[1])
    except ValueError:
        await update.message.reply_text("Кількість днів має бути числом.")
        return

    msg = parts[2]
    now = int(time.time())
    threshold = now - days * 86400

    conn = await get_db()
    cur = await conn.execute("""
        SELECT telegram_id
        FROM users
        WHERE last_activity < ?
    """, (threshold,))
    rows = await cur.fetchall()

    sent = 0
    for row in rows:
        try:
            await telegram_app.bot.send_message(chat_id=row["telegram_id"], text=msg)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            print("broadcast_inactive error:", e)

    await update.message.reply_text(
        f"Розсилка неактивним за {days} днів надіслана {sent} користувачам."
    )


telegram_app.add_handler(CommandHandler("broadcast_all", broadcast_all_cmd))
telegram_app.add_handler(CommandHandler("broadcast_paid", broadcast_paid_cmd))
telegram_app.add_handler(CommandHandler("broadcast_nonbuyers", broadcast_nonbuyers_cmd))
telegram_app.add_handler(CommandHandler("broadcast_by_dates", broadcast_by_dates_cmd))
telegram_app.add_handler(CommandHandler("broadcast_inactive", broadcast_inactive_cmd))


# ===================== SUPPORT: /reply =====================

async def reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    # формат: /reply user_id текст...
    parts = update.message.text.split(" ", 2)
    if len(parts) < 3:
        await update.message.reply_text(
            "Формат:\n/reply 123456789 Твоя відповідь користувачу"
        )
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await update.message.reply_text("user_id має бути числом.")
        return

    reply_text = parts[2]

    try:
        await telegram_app.bot.send_message(chat_id=target_id, text=reply_text)
        await log_message(target_id, is_admin=1, direction="out", content_type="text", text=reply_text)
        await update.message.reply_text("✔ Відповідь надіслана.")
    except Exception as e:
        await update.message.reply_text(
            f"Помилка при відправці відповіді:\n<code>{e}</code>",
            parse_mode="HTML"
        )


telegram_app.add_handler(CommandHandler("reply", reply_cmd))


# ===================== SUPPORT: перехоплення всіх повідомлень юзерів =====================

async def user_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Ловимо всі звичайні повідомлення користувачів (не команди),
    логимо в БД і шлемо в групу підтримки.
    """
    if update.effective_chat is None or update.message is None:
        return

    chat = update.effective_chat
    msg = update.message
    user = update.effective_user

    # тільки приватні чати з користувачами
    if chat.type != "private":
        return

    # не чіпаємо повідомлення адміна
    if user and user.id == ADMIN_ID:
        return

    # не чіпаємо команди (/start, /access тощо)
    if msg.text and msg.text.startswith("/"):
        return

    telegram_id = user.id
    username = user.username or "-"
    first_name = user.first_name or "-"

    await upsert_user(telegram_id, username, first_name)

    # визначаємо тип контенту
    content_type = "text"
    text_content = msg.text or msg.caption or ""

    if msg.photo:
        content_type = "photo"
    elif msg.video:
        content_type = "video"
    elif msg.audio:
        content_type = "audio"
    elif msg.voice:
        content_type = "voice"
    elif msg.document:
        content_type = "document"
    elif msg.sticker:
        content_type = "sticker"

    await log_message(telegram_id, is_admin=0, direction="in", content_type=content_type, text=text_content)

    # формуємо красиве повідомлення в групу підтримки
    has_username = f"@{username}" if user.username else "немає"
    summary = (
        "💬 <b>Нове повідомлення від користувача</b>\n\n"
        f"👤 ID: <code>{telegram_id}</code>\n"
        f"🙍‍♂️ Ім'я: <b>{first_name}</b>\n"
        f"🔗 Username: {has_username}\n"
        f"📦 Тип: <b>{content_type}</b>\n"
    )

    if text_content:
        summary += f"\n📝 Текст:\n<code>{text_content}</code>"

    try:
        await telegram_app.bot.send_message(
            chat_id=SUPPORT_CHAT_ID,
            text=summary,
            parse_mode="HTML"
        )

        # якщо є медіа — дублюємо саме повідомлення (фото/відео/голос) в групу
        if content_type != "text":
            await telegram_app.bot.copy_message(
                chat_id=SUPPORT_CHAT_ID,
                from_chat_id=chat.id,
                message_id=msg.message_id
            )
    except Exception as e:
        print("support forward error:", e)


telegram_app.add_handler(MessageHandler(filters.ALL, user_message_handler))


# ===================== TELEGRAM WEBHOOK =====================

@app.post("/telegram/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403)

    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}


# ===================== WAYFORPAY CALLBACK =====================

@app.post("/wayforpay/callback")
async def wayforpay_callback(request: Request):
    body = await request.json()

    if not wfp_callback_valid(body):
        return {"code": "error", "msg": "bad signature"}

    order_ref = body.get("orderReference")
    status = body.get("transactionStatus")

    # order_{product_id}_{telegram_id}
    m = re.match(r"order_(\d+)_(\d+)", order_ref or "")
    if not m:
        return {"code": "error"}

    product_id = int(m.group(1))
    telegram_id = int(m.group(2))

    if status == "Approved":
        await mark_purchase_paid(order_ref)
        link = await create_one_time_link(telegram_id, product_id)

        await telegram_app.bot.send_message(
            chat_id=telegram_id,
            text=(
                "🎉 <b>Оплата успішна!</b>\n\n"
                "Ось твій <b>особистий доступ</b> у приватний канал з уроками:\n"
                f"{link}"
            ),
            parse_mode="HTML"
        )

    ts = int(time.time())
    sig = wfp_response_signature(order_ref, "accept", ts)

    return {
        "orderReference": order_ref,
        "status": "accept",
        "time": ts,
        "signature": sig
    }


# ===================== ROOT =====================

@app.get("/")
async def root():
    return {"status": "running"}
