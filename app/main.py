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

CHANNEL_ID = int(os.getenv("CHANNEL_ID"))  # ID каналу з уроками

# WayForPay
MERCHANT_LOGIN = os.getenv("MERCHANT_LOGIN") or os.getenv("WAYFORPAY_MERCHANT")
MERCHANT_SECRET_KEY = os.getenv("MERCHANT_SECRET_KEY")  # 40-символьний SecretKey
MERCHANT_DOMAIN = os.getenv("MERCHANT_DOMAIN", "www.massagesobi.com")

PRODUCT_ID = int(os.getenv("PRODUCT_ID", "1"))
PRODUCT_NAME = os.getenv("PRODUCT_NAME", "Курс самомасажу")
AMOUNT = float(os.getenv("AMOUNT", "290.00"))
CURRENCY = os.getenv("CURRENCY", "UAH")

# URL твого сервісу для callback WayForPay
# Наприклад: https://telegram-massage-course-bot-chat.onrender.com/wayforpay/callback
SERVICE_URL = os.getenv("SERVICE_URL")

# Для антизасинання
KEEP_ALIVE_URL = os.getenv("KEEP_ALIVE_URL")

# Адмін
ADMIN_ID = int(os.getenv("ADMIN_ID", "268351523"))
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID", "-5032163085"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID missing")
if not KEEP_ALIVE_URL:
    raise RuntimeError("KEEP_ALIVE_URL missing")
if not MERCHANT_LOGIN or not MERCHANT_SECRET_KEY:
    raise RuntimeError("MERCHANT_LOGIN or MERCHANT_SECRET_KEY missing")
if not SERVICE_URL:
    raise RuntimeError("SERVICE_URL missing")

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

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY,
            name TEXT,
            price REAL,
            currency TEXT,
            is_active INTEGER DEFAULT 1
        )
    """)

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

    # Початковий продукт
    cur = await conn.execute("SELECT id FROM products WHERE id = ?", (PRODUCT_ID,))
    if await cur.fetchone() is None:
        await conn.execute("""
            INSERT INTO products (id, name, price, currency, is_active)
            VALUES (?, ?, ?, ?, 1)
        """, (PRODUCT_ID, PRODUCT_NAME, AMOUNT, CURRENCY))

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
        SET status='approved', paid_at=?
        WHERE order_ref=?
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


# ===================== KEEP ALIVE =====================

async def keep_alive():
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                await s.get(KEEP_ALIVE_URL)
        except Exception as e:
            print("keep_alive error:", e)
        await asyncio.sleep(300)


# ===================== WAYFORPAY HELPERS =====================

def wfp_sign(msg: str) -> str:
    """
    Підпис WayForPay:
    hash_hmac('md5', msg, SECRET_KEY)
    """
    return hmac.new(MERCHANT_SECRET_KEY.encode(), msg.encode(), hashlib.md5).hexdigest()


def wfp_invoice_signature(payload: dict) -> str:
    # WayForPay дуже чутливий до формату суми.
    # Насильно форматуємо amount і productPrice як 290.00 (2 знаки після крапки).
    def fmt_amount(x) -> str:
        return f"{float(x):.2f}"

    parts = [
        payload["merchantAccount"],
        payload["merchantDomainName"],
        payload["orderReference"],
        str(payload["orderDate"]),
        fmt_amount(payload["amount"]),
        payload["currency"],
    ]

    for p in payload["productName"]:
        parts.append(str(p))
    for c in payload["productCount"]:
        parts.append(str(c))
    for pr in payload["productPrice"]:
        parts.append(fmt_amount(pr))

    msg = ";".join(parts)
    sig = wfp_sign(msg)
    print("WFP INVOICE STRING:", msg)
    print("WFP INVOICE SIGNATURE:", sig)
    return sig


def wfp_callback_valid(body: dict) -> bool:
    # Параметри для підпису колбеку (serviceUrl)
    required = [
        "merchantAccount", "orderReference", "amount", "currency",
        "authCode", "cardPan", "transactionStatus", "reasonCode",
        "merchantSignature"
    ]
    if not all(k in body for k in required):
        print("WFP CALLBACK: missing fields")
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
    expected = wfp_sign(msg)
    print("WFP CALLBACK STRING:", msg)
    print("WFP CALLBACK EXPECTED:", expected)
    print("WFP CALLBACK GOT:", body["merchantSignature"])
    return expected == body["merchantSignature"]


def wfp_response_signature(order_ref: str, status: str, ts: int) -> str:
    msg = f"{order_ref};{status};{ts}"
    sig = wfp_sign(msg)
    print("WFP RESPONSE STRING:", msg)
    print("WFP RESPONSE SIGNATURE:", sig)
    return sig


# ===================== STARTUP =====================

@app.on_event("startup")
async def startup_event():
    await telegram_app.initialize()
    await telegram_app.start()
    await init_db()
    asyncio.create_task(keep_alive())


# ===================== HELPERS =====================

def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID


async def create_one_time_link(telegram_id: int, product_id: int) -> str:
    invite = await telegram_app.bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        member_limit=1
    )
    link = invite.invite_link
    await mark_access(telegram_id, product_id, link)
    return link


# ===================== /start =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await upsert_user(user.id, user.username, user.first_name)

    args = context.args

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Оплатити курс", callback_data=f"pay:{PRODUCT_ID}")],
        [InlineKeyboardButton("🧪 Тестова оплата", callback_data=f"testpay:{PRODUCT_ID}")],
    ])

    if args and args[0] == "site":
        txt = (
            "Вітаю! 👋\n\n"
            "Ви перейшли з сайту <b>Сам Собі Масажист</b>.\n\n"
            "Натисніть кнопку нижче, щоб оплатити курс і отримати доступ "
            "у приватний канал з відеоуроками ❤️"
        )
    else:
        txt = (
            "Вітаю! 👋\n\n"
            "Це бот доступу до курсу самомасажу.\n"
            "Натисніть кнопку нижче, щоб отримати доступ.\n\n"
            "<b>Після оплати Ви автоматично отримаєте особистий доступ у приватний канал.</b>"
        )

    await update.message.reply_text(txt, reply_markup=keyboard, parse_mode="HTML")


telegram_app.add_handler(CommandHandler("start", start))


# ===================== /access =====================

async def access_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await upsert_user(user.id, user.username, user.first_name)

    conn = await get_db()

    cur = await conn.execute("""
        SELECT COUNT(*) AS c
        FROM purchases
        WHERE telegram_id = ? AND status='approved'
    """, (user.id,))
    row = await cur.fetchone()

    if row["c"] == 0:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Оплатити курс", callback_data=f"pay:{PRODUCT_ID}")]
        ])
        await update.message.reply_text(
            "<b>У Вас ще немає активного доступу.</b>\n"
            "Щоб отримати його — потрібно сплатити курс нижче 👇",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        return

    try:
        link = await create_one_time_link(user.id, PRODUCT_ID)
        await update.message.reply_text(
            "🔑 <b>Ось Ваш новий особистий доступ у канал:</b>\n"
            f"{link}\n\n"
            "Якщо не зможете зайти — просто повторіть /access 🙂",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(
            f"Помилка під час створення доступу:\n<code>{e}</code>",
            parse_mode="HTML"
        )


telegram_app.add_handler(CommandHandler("access", access_cmd))


# ===================== TESTPAY =====================

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
                "Ось Ваш <b>особистий доступ</b> у канал з уроками:\n"
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


telegram_app.add_handler(CallbackQueryHandler(testpay_cb, pattern=r"^testpay:"))


# ===================== PAYMENT (WayForPay CREATE_INVOICE) =====================

async def pay_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    await upsert_user(user.id, user.username, user.first_name)

    data = query.data.split(":")
    product_id = int(data[1]) if len(data) > 1 else PRODUCT_ID

    order_ref = f"order_{product_id}_{user.id}_{int(time.time())}"
    await create_purchase_pending(user.id, product_id, AMOUNT, CURRENCY, order_ref)

    order_date = int(time.time())

    payload = {
        "transactionType": "CREATE_INVOICE",
        "merchantAccount": MERCHANT_LOGIN,
        "merchantDomainName": MERCHANT_DOMAIN,
        "orderReference": order_ref,
        "orderDate": order_date,
        "amount": AMOUNT,
        "currency": CURRENCY,
        "productName": [PRODUCT_NAME],
        "productCount": [1],
        "productPrice": [AMOUNT],
        "language": "UA",
        "apiVersion": 1,
        # важливо: щоб WayForPay шле POST на наш callback
        "serviceUrl": SERVICE_URL,
    }

    payload["merchantSignature"] = wfp_invoice_signature(payload)

    print("Sending WayForPay payload:", payload)

    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.wayforpay.com/api", json=payload) as resp:
            ct = resp.headers.get("Content-Type", "")
            if "application/json" in ct:
                data = await resp.json()
            else:
                text = await resp.text()
                print("WayForPay non-JSON response:", text)
                await query.message.reply_text("Помилка при створенні інвойсу.")
                return

    print("WayForPay response:", data)

    if data.get("reasonCode") not in (1100, 1101, 1102):
        await query.message.reply_text(
            f"Помилка при створенні інвойсу:\n<code>{data}</code>",
            parse_mode="HTML"
        )
        return

    invoice = data.get("invoiceUrl")
    if not invoice:
        await query.message.reply_text("Помилка при створенні інвойсу.")
        return

    txt = (
        "<b>Оплата курсу</b>\n\n"
        "Щоб сплатити курс, перейдіть за посиланням нижче:\n\n"
        f"{invoice}\n\n"
        "Після успішної оплати Ви автоматично отримаєте доступ у приватний канал з уроками.\n"
        "Якщо оплата не пройде, WayForPay покаже помилку на сторінці оплати."
    )

    await query.message.reply_text(txt, parse_mode="HTML")


telegram_app.add_handler(CallbackQueryHandler(pay_cb, pattern=r"^pay:"))


# ===================== /stats =====================

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    conn = await get_db()
    now = int(time.time())

    def period(ts_days):
        return now - ts_days * 86400

    cur = await conn.execute("SELECT COUNT(*) AS c FROM users")
    total_users = (await cur.fetchone())["c"]

    cur = await conn.execute("SELECT COUNT(*) AS c FROM purchases WHERE status='approved'")
    total_paid = (await cur.fetchone())["c"]

    cur = await conn.execute("SELECT COALESCE(SUM(amount),0) AS s FROM purchases WHERE status='approved'")
    total_revenue = (await cur.fetchone())["s"]

    async def count_period(sec):
        cur = await conn.execute("""
            SELECT COUNT(*) AS c,
                   COALESCE(SUM(amount),0) AS revenue
            FROM purchases
            WHERE status='approved'
              AND paid_at >= ?
        """, (sec,))
        row = await cur.fetchone()
        return row["c"], row["revenue"]

    day_c, day_rev     = await count_period(period(1))
    week_c, week_rev   = await count_period(period(7))
    month_c, month_rev = await count_period(period(30))
    q_c, q_rev         = await count_period(period(90))

    txt = (
        "<b>Статистика бота</b>\n\n"
        "👥 Усього користувачів: <b>{}</b>\n"
        "💳 Усього покупців: <b>{}</b>\n"
        "💰 Загальний дохід: <b>{} UAH</b>\n\n"
        "<b>Продажі по періодах:</b>\n"
        "📅 За 24 години: <b>{}</b> купівель – <b>{} UAH</b>\n"
        "📆 За 7 днів: <b>{}</b> купівель – <b>{} UAH</b>\n"
        "🗓 За 30 днів: <b>{}</b> купівель – <b>{} UAH</b>\n"
        "📈 За 90 днів: <b>{}</b> купівель – <b>{} UAH</b>\n"
    ).format(
        total_users, total_paid, round(total_revenue, 2),
        day_c, round(day_rev, 2),
        week_c, round(week_rev, 2),
        month_c, round(month_rev, 2),
        q_c, round(q_rev, 2)
    )

    await update.message.reply_text(txt, parse_mode="HTML")

telegram_app.add_handler(CommandHandler("stats", stats_cmd))


# ===================== TEXT BROADCASTS =====================

async def broadcast_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    text = update.message.text.split(" ", 1)
    if len(text) < 2:
        await update.message.reply_text("Напишіть текст після команди, наприклад:\n/broadcast_all Привіт! ❤️")
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
        await update.message.reply_text("Напишіть текст після команди, наприклад:\n/broadcast_paid Привіт, дякую за покупку! ❤️")
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
        await update.message.reply_text("Напишіть текст після команди, наприклад:\n/broadcast_nonbuyers Привіт! Ось спецпропозиція для Вас 💛")
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

    parts = update.message.text.split(" ", 3)
    if len(parts) < 4:
        await update.message.reply_text(
            "Формат:\n"
            "/broadcast_by_dates 2023-12-01 2023-12-31 Текст для покупців грудня ❤️"
        )
        return

    start_date_str = parts[1]
    end_date_str = parts[2]
    msg = parts[3]

    try:
        start_ts = int(time.mktime(time.strptime(start_date_str, "%Y-%m-%d")))
        end_ts = int(time.mktime(time.strptime(end_date_str, "%Y-%m-%d"))) + 86400
    except Exception:
        await update.message.reply_text("Некоректний формат дати. Використовуйте YYYY-MM-DD.")
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

    parts = update.message.text.split(" ", 2)
    if len(parts) < 3:
        await update.message.reply_text(
            "Формат:\n/broadcast_inactive 30 Привіт! Давно Вас не було 🙂"
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


# ===================== MEDIA BROADCASTS =====================

async def resolve_audience(audience: str):
    conn = await get_db()

    if audience == "all":
        cur = await conn.execute("SELECT telegram_id FROM users")
        rows = await cur.fetchall()
        return [r["telegram_id"] for r in rows]

    if audience == "paid":
        cur = await conn.execute("""
            SELECT DISTINCT telegram_id
            FROM purchases
            WHERE status='approved'
        """)
        rows = await cur.fetchall()
        return [r["telegram_id"] for r in rows]

    if audience == "nonbuyers":
        cur = await conn.execute("""
            SELECT u.telegram_id
            FROM users u
            LEFT JOIN purchases p
              ON u.telegram_id = p.telegram_id AND p.status='approved'
            WHERE p.id IS NULL
        """)
        rows = await cur.fetchall()
        return [r["telegram_id"] for r in rows]

    if audience.startswith("inactive_"):
        days = int(audience.split("_")[1])
        now = int(time.time())
        threshold = now - days * 86400

        cur = await conn.execute("""
            SELECT telegram_id
            FROM users
            WHERE last_activity < ?
        """, (threshold,))
        rows = await cur.fetchall()
        return [r["telegram_id"] for r in rows]

    if audience.startswith("dates_"):
        try:
            _, start_s, end_s = audience.split("_")
        except ValueError:
            return []

        try:
            start_ts = int(time.mktime(time.strptime(start_s, "%Y-%m-%d")))
            end_ts = int(time.mktime(time.strptime(end_s, "%Y-%m-%d"))) + 86400
        except Exception:
            return []

        cur = await conn.execute("""
            SELECT DISTINCT telegram_id
            FROM purchases
            WHERE status='approved'
              AND paid_at IS NOT NULL
              AND paid_at >= ? AND paid_at < ?
        """, (start_ts, end_ts))
        rows = await cur.fetchall()
        return [r["telegram_id"] for r in rows]

    return []


async def handle_media_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, media_type: str):
    if not is_admin(update):
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "Для медіа-розсилки:\n"
            "1) Надішліть боту медіа (фото/відео/аудіо/файл)\n"
            "2) У відповіді на це повідомлення введіть команду, наприклад:\n"
            "/broadcast_photo all"
        )
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Вкажіть сегмент, наприклад: /broadcast_photo all")
        return

    audience = parts[1]
    recipients = await resolve_audience(audience)
    if not recipients:
        await update.message.reply_text("Немає користувачів у вибраній аудиторії.")
        return

    src = update.message.reply_to_message

    sent = 0
    for uid in recipients:
        try:
            if media_type == "photo" and src.photo:
                file = src.photo[-1].file_id
                await telegram_app.bot.send_photo(chat_id=uid, photo=file, caption=src.caption or "")
            elif media_type == "video" and src.video:
                file = src.video.file_id
                await telegram_app.bot.send_video(chat_id=uid, video=file, caption=src.caption or "")
            elif media_type == "audio" and src.audio:
                file = src.audio.file_id
                await telegram_app.bot.send_audio(chat_id=uid, audio=file, caption=src.caption or "")
            elif media_type == "document" and src.document:
                file = src.document.file_id
                await telegram_app.bot.send_document(chat_id=uid, document=file, caption=src.caption or "")
            else:
                continue

            sent += 1
            await asyncio.sleep(0.05)

        except Exception as e:
            print("media_broadcast error:", e)

    await update.message.reply_text(f"Медіа-розсилку надіслано {sent} користувачам.")


async def broadcast_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_media_broadcast(update, context, "photo")


async def broadcast_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_media_broadcast(update, context, "video")


async def broadcast_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_media_broadcast(update, context, "audio")


async def broadcast_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_media_broadcast(update, context, "document")


telegram_app.add_handler(CommandHandler("broadcast_photo", broadcast_photo))
telegram_app.add_handler(CommandHandler("broadcast_video", broadcast_video))
telegram_app.add_handler(CommandHandler("broadcast_audio", broadcast_audio))
telegram_app.add_handler(CommandHandler("broadcast_doc", broadcast_doc))


# ===================== SUPPORT /reply =====================

async def reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    parts = update.message.text.split(" ", 2)
    if len(parts) < 3:
        await update.message.reply_text("Формат:\n/reply USER_ID текст відповіді")
        return

    try:
        target = int(parts[1])
    except ValueError:
        await update.message.reply_text("USER_ID має бути числом.")
        return

    text = parts[2]

    try:
        await telegram_app.bot.send_message(target, text)
        await log_message(target, 1, "out", "text", text)
        await update.message.reply_text("Відповідь надіслана ✔")
    except Exception as e:
        await update.message.reply_text(
            f"Помилка при відправці відповіді:\n<code>{e}</code>",
            parse_mode="HTML"
        )


telegram_app.add_handler(CommandHandler("reply", reply_cmd))


# ===================== SUPPORT: INCOMING MESSAGES =====================

async def user_msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat = update.effective_chat
    msg = update.message
    user = update.effective_user

    if chat.type != "private":
        return

    if user.id == ADMIN_ID:
        return

    if msg.text and msg.text.startswith("/"):
        return

    await upsert_user(user.id, user.username, user.first_name)

    content_type = "text"
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

    text_content = msg.text or msg.caption or ""

    await log_message(user.id, 0, "in", content_type, text_content)

    summary = (
        "💬 <b>Нове повідомлення від користувача</b>\n\n"
        f"👤 ID: <code>{user.id}</code>\n"
        f"🙍‍♂️ Ім'я: <b>{user.first_name}</b>\n"
        f"🔗 Username: @{user.username if user.username else 'немає'}\n"
        f"📦 Тип: <b>{content_type}</b>\n"
    )

    if text_content:
        summary += f"\n📝 Текст:\n<code>{text_content}</code>"

    try:
        await telegram_app.bot.send_message(
            SUPPORT_CHAT_ID,
            summary,
            parse_mode="HTML"
        )

        if content_type != "text":
            await telegram_app.bot.copy_message(
                SUPPORT_CHAT_ID,
                chat.id,
                msg.message_id
            )
    except Exception as e:
        print("support forward error:", e)


telegram_app.add_handler(MessageHandler(filters.ALL, user_msg_handler))


# ===================== WAYFORPAY CALLBACK =====================

@app.post("/wayforpay/callback")
async def wfp_callback(request: Request):
    body = await request.json()

    print("WayForPay callback body:", body)

    if not wfp_callback_valid(body):
        return {"code": "invalid-signature"}

    order_ref = body.get("orderReference")
    status = body.get("transactionStatus")

    match = re.match(r"order_(\d+)_(\d+)_(\d+)", order_ref or "")
    if not match:
        return {"code": "bad-order-ref"}

    product_id = int(match.group(1))
    telegram_id = int(match.group(2))

    if status == "Approved":
        await mark_purchase_paid(order_ref)
        link = await create_one_time_link(telegram_id, product_id)

        await telegram_app.bot.send_message(
            telegram_id,
            (
                "🎉 <b>Оплата успішна!</b>\n\n"
                "Ось Ваш <b>особистий доступ</b> у приватний канал з уроками:\n"
                f"{link}"
            ),
            parse_mode="HTML"
        )

    ts = int(time.time())
    signature = wfp_response_signature(order_ref, "accept", ts)

    return {
        "orderReference": order_ref,
        "status": "accept",
        "time": ts,
        "signature": signature
    }


# ===================== TEST WFP (для діагностики) =====================

@app.get("/test-wfp")
async def test_wfp():
    order_ref = f"test_{int(time.time())}"
    order_date = int(time.time())

    payload = {
        "transactionType": "CREATE_INVOICE",
        "merchantAccount": MERCHANT_LOGIN,
        "merchantDomainName": MERCHANT_DOMAIN,
        "orderReference": order_ref,
        "orderDate": order_date,
        "amount": AMOUNT,
        "currency": CURRENCY,
        "productName": [PRODUCT_NAME],
        "productCount": [1],
        "productPrice": [AMOUNT],
        "language": "UA",
        "apiVersion": 1,
        "serviceUrl": SERVICE_URL,
    }

    payload["merchantSignature"] = wfp_invoice_signature(payload)

    print("TEST WFP PAYLOAD:", payload)

    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.wayforpay.com/api", json=payload) as resp:
            ct = resp.headers.get("Content-Type", "")
            if "application/json" in ct:
                data = await resp.json()
            else:
                data = {"raw": await resp.text()}

    print("WAYFORPAY RAW RESPONSE:", data)
    return data


# ===================== TELEGRAM WEBHOOK =====================

@app.post("/telegram/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403)

    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}


# ===================== ROOT =====================

@app.get("/")
async def root():
    return {"status": "running"}
