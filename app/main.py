import os
import time
import asyncio
import aiohttp
import aiosqlite
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
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

PAYMENT_BUTTON_URL = os.getenv(
    "PAYMENT_BUTTON_URL",
    "https://secure.wayforpay.com/button/XXXXXXX"
)

PRODUCT_ID = int(os.getenv("PRODUCT_ID", "1"))
PRODUCT_NAME = os.getenv("PRODUCT_NAME", "Massage Course")
AMOUNT = float(os.getenv("AMOUNT", "290.00"))
CURRENCY = os.getenv("CURRENCY", "UAH")

KEEP_ALIVE_URL = os.getenv("KEEP_ALIVE_URL")

ADMIN_ID = int(os.getenv("ADMIN_ID"))
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID"))

BOT_USERNAME = os.getenv("BOT_USERNAME")  # без @

if not BOT_TOKEN or not CHANNEL_ID or not KEEP_ALIVE_URL:
    raise RuntimeError("Missing ENV variables")

# ===================== APP =====================

app = FastAPI()
DB_PATH = "database.db"
db = None

telegram_app = Application.builder().token(BOT_TOKEN).build()

# ===================== DB =====================

async def get_db():
    global db
    if not db:
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
            has_access INTEGER DEFAULT 0,
            awaiting_payment INTEGER DEFAULT 0
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            amount REAL,
            currency TEXT,
            status TEXT,
            created_at INTEGER
        )
    """)

    await conn.commit()


# ===================== KEEP ALIVE =====================

async def keep_alive():
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                await s.get(KEEP_ALIVE_URL)
        except Exception:
            pass
        await asyncio.sleep(300)


# ===================== STARTUP =====================

@app.on_event("startup")
async def startup():
    await telegram_app.initialize()
    await telegram_app.start()
    await init_db()
    asyncio.create_task(keep_alive())


# ===================== HELPERS =====================

async def upsert_user(user):
    conn = await get_db()
    now = int(time.time())

    await conn.execute("""
        INSERT OR IGNORE INTO users
        (telegram_id, username, first_name, joined_at, last_activity)
        VALUES (?, ?, ?, ?, ?)
    """, (user.id, user.username, user.first_name, now, now))

    await conn.execute("""
        UPDATE users SET last_activity=? WHERE telegram_id=?
    """, (now, user.id))

    await conn.commit()


async def create_invite(telegram_id):
    invite = await telegram_app.bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        member_limit=1
    )
    return invite.invite_link


def is_admin(update: Update):
    return update.effective_user.id == ADMIN_ID


# ===================== /start =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await upsert_user(user)

    conn = await get_db()
    args = context.args or []

    # === Повернення після оплати ===
    if args and args[0] == "paid":
        cur = await conn.execute(
            "SELECT awaiting_payment FROM users WHERE telegram_id=?",
            (user.id,)
        )
        row = await cur.fetchone()

        if not row or row["awaiting_payment"] == 0:
            await update.message.reply_text(
                "Я не бачу підтвердженої оплати. Якщо щось пішло не так — напишіть у підтримку 🙏"
            )
            return

        await conn.execute("""
            UPDATE users SET has_access=1, awaiting_payment=0 WHERE telegram_id=?
        """, (user.id,))

        await conn.execute("""
            INSERT INTO purchases (telegram_id, amount, currency, status, created_at)
            VALUES (?, ?, ?, 'approved', ?)
        """, (user.id, AMOUNT, CURRENCY, int(time.time())))

        await conn.commit()

        link = await create_invite(user.id)
        await update.message.reply_text(
            f"🎉 Оплата успішна!\n\nВаш доступ:\n{link}"
        )
        return

    # === Звичайний старт ===
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Оплатити курс", callback_data="pay")]
    ])

    await update.message.reply_text(
        "Вітаю! 👋\n\nНатисніть кнопку нижче, щоб оплатити курс і отримати доступ 👇",
        reply_markup=keyboard
    )


telegram_app.add_handler(CommandHandler("start", start))


# ===================== PAY =====================

async def pay_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    await upsert_user(user)

    conn = await get_db()
    await conn.execute(
        "UPDATE users SET awaiting_payment=1 WHERE telegram_id=?",
        (user.id,)
    )
    await conn.commit()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Перейти до оплати", url=PAYMENT_BUTTON_URL)]
    ])

    await query.message.reply_text(
        "Перейдіть за посиланням та оплатіть курс 👇",
        reply_markup=keyboard
    )


telegram_app.add_handler(CallbackQueryHandler(pay_cb, pattern="^pay$"))


# ===================== /access =====================

async def access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = await get_db()

    cur = await conn.execute(
        "SELECT has_access FROM users WHERE telegram_id=?",
        (user.id,)
    )
    row = await cur.fetchone()

    if not row or row["has_access"] == 0:
        await update.message.reply_text("У вас ще немає доступу ❌")
        return

    link = await create_invite(user.id)
    await update.message.reply_text(f"Ваш доступ:\n{link}")


telegram_app.add_handler(CommandHandler("access", access))


# ===================== STATS =====================

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    conn = await get_db()

    cur = await conn.execute("SELECT COUNT(*) c FROM users")
    users = (await cur.fetchone())["c"]

    cur = await conn.execute("SELECT COUNT(*) c FROM purchases")
    sales = (await cur.fetchone())["c"]

    cur = await conn.execute("SELECT COALESCE(SUM(amount),0) s FROM purchases")
    revenue = (await cur.fetchone())["s"]

    await update.message.reply_text(
        f"👥 Користувачі: {users}\n"
        f"💳 Оплати: {sales}\n"
        f"💰 Дохід: {revenue} UAH"
    )


telegram_app.add_handler(CommandHandler("stats", stats))


# ===================== SUPPORT =====================

async def reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    try:
        uid = int(context.args[0])
        text = " ".join(context.args[1:])
        await telegram_app.bot.send_message(uid, text)
        await update.message.reply_text("Відповідь надіслано ✅")
    except Exception as e:
        await update.message.reply_text(str(e))


telegram_app.add_handler(CommandHandler("reply", reply_cmd))


# ===================== MEDIA =====================

async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass  # залишено місце для розширення


telegram_app.add_handler(MessageHandler(filters.ALL, media_handler))


# ===================== SUCCESS PAGE =====================

@app.get("/payment/success", response_class=HTMLResponse)
async def payment_success():
    return f"""
<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<title>Оплата успішна</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body style="font-family:Arial;text-align:center;padding:40px">
<h2>Оплата успішна ✅</h2>
<p>Дякуємо за оплату курсу <b>{PRODUCT_NAME}</b></p>
<a href="https://t.me/{BOT_USERNAME}?start=paid"
style="display:inline-block;padding:14px 26px;background:#0088cc;color:#fff;
text-decoration:none;border-radius:30px;font-weight:bold">
Отримати доступ
</a>
</body>
</html>
"""


# ===================== WEBHOOK =====================

@app.post("/telegram/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403)

    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}


@app.get("/")
async def root():
    return {"status": "running"}
