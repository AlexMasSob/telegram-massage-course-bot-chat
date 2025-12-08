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

# 🔥 НОВІ ЗМІННІ:
MERCHANT_LOGIN = (os.getenv("MERCHANT_LOGIN") or "").strip()
MERCHANT_PASSWORD = (os.getenv("MERCHANT_PASSWORD") or "").strip()
MERCHANT_DOMAIN = os.getenv("MERCHANT_DOMAIN", "www.massagesobi.com").strip()

PRODUCT_ID = int(os.getenv("PRODUCT_ID", "1"))
PRODUCT_NAME = os.getenv("PRODUCT_NAME", "Massage Course")
AMOUNT = float(os.getenv("AMOUNT", "290.00"))
CURRENCY = os.getenv("CURRENCY", "UAH")

SERVICE_URL = os.getenv("SERVICE_URL")
KEEP_ALIVE_URL = os.getenv("KEEP_ALIVE_URL")

ADMIN_ID = int(os.getenv("ADMIN_ID", "268351523"))
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID", "-5032163085"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID missing")
if not KEEP_ALIVE_URL:
    raise RuntimeError("KEEP_ALIVE_URL missing")
if not MERCHANT_LOGIN or not MERCHANT_PASSWORD:
    raise RuntimeError("MERCHANT_LOGIN or MERCHANT_PASSWORD missing")
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

# 🔥 ГОЛОВНА ЗМІНА — ПІДПИС ЧЕРЕЗ MERCHANT_PASSWORD

def wfp_invoice_signature(payload: dict) -> str:
    parts = [
        payload["merchantAccount"],
        payload["merchantDomainName"],
        payload["orderReference"],
        str(payload["orderDate"]),
        str(payload["amount"]),
        payload["currency"],
    ]

    for p in payload["productName"]:
        parts.append(str(p))
    for c in payload["productCount"]:
        parts.append(str(c))
    for pr in payload["productPrice"]:
        parts.append(str(pr))

    msg = ";".join(parts)

    signature = hmac.new(
        MERCHANT_PASSWORD.encode(),
        msg.encode(),
        hashlib.md5
    ).hexdigest()

    print("SIGN STRING:", msg)
    print("SIGNATURE:", signature)

    return signature



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
        MERCHANT_PASSWORD.encode(),
        msg.encode(),
        hashlib.md5
    ).hexdigest()

    print("CALLBACK EXPECTED:", expected)
    print("CALLBACK PROVIDED:", body["merchantSignature"])

    return expected == body["merchantSignature"]



def wfp_response_signature(order_ref: str, status: str, ts: int) -> str:
    msg = f"{order_ref};{status};{ts}"
    return hmac.new(
        MERCHANT_PASSWORD.encode(),
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
            "🔑 <b>Ваш новий особистий доступ:</b>\n"
            f"{link}",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(
            f"Помилка:\n<code>{e}</code>",
            parse_mode="HTML"
        )


telegram_app.add_handler(CommandHandler("access", access_cmd))



# ===================== TESTPAY =====================

async def testpay_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    await upsert_user(user.id, user.username, user.first_name)

    try:
        link = await create_one_time_link(user.id, PRODUCT_ID)

        await telegram_app.bot.send_message(
            chat_id=user.id,
            text=(
                "🧪 <b>Тестова оплата успішна!</b>\n\n"
                "Ваш доступ:\n"
                f"{link}"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        await query.message.reply_text(f"Помилка:\n<code>{e}</code>")
        return

    await query.message.reply_text("Готово! 🎉")


telegram_app.add_handler(CallbackQueryHandler(testpay_cb, pattern=r"^testpay:"))



# ===================== PAYMENT =====================

async def pay_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    await upsert_user(user.id, user.username, user.first_name)

    order_ref = f"order_{PRODUCT_ID}_{user.id}_{int(time.time())}"
    await create_purchase_pending(user.id, PRODUCT_ID, AMOUNT, CURRENCY, order_ref)

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
    }

    if SERVICE_URL:
        payload["serviceUrl"] = SERVICE_URL

    payload["merchantSignature"] = wfp_invoice_signature(payload)

    print("Sending payload:", payload)

    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.wayforpay.com/api", json=payload) as resp:
            ct = resp.headers.get("Content-Type", "")
            if "application/json" in ct:
                data = await resp.json()
            else:
                print("Non-JSON response:", await resp.text())
                await query.message.reply_text("Помилка при створенні інвойсу.")
                return

    print("WFP response:", data)

    invoice = data.get("invoiceUrl")
    if not invoice:
        await query.message.reply_text("Помилка при створенні інвойсу.")
        return

    txt = (
        "<b>Готово!</b> 🎉\n\n"
        "Посилання на оплату:\n"
        f"{invoice}"
    )

    await query.message.reply_text(txt, parse_mode="HTML")


telegram_app.add_handler(CallbackQueryHandler(pay_cb, pattern=r"^pay:"))



# ===================== CALLBACK =====================

@app.post("/wayforpay/callback")
async def wfp_callback(request: Request):
    body = await request.json()

    print("CALLBACK BODY:", body)

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
            f"🎉 Оплата успішна!\nВаш доступ:\n{link}",
        )

    ts = int(time.time())
    signature = wfp_response_signature(order_ref, "accept", ts)

    return {
        "orderReference": order_ref,
        "status": "accept",
        "time": ts,
        "signature": signature
    }



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
