import os
import time
import asyncio
import aiohttp
import aiosqlite

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
ADMIN_ID = int(os.getenv("ADMIN_ID"))
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID"))

PAYMENT_BUTTON_URL = os.getenv("PAYMENT_BUTTON_URL")
KEEP_ALIVE_URL = os.getenv("KEEP_ALIVE_URL")

PRODUCT_ID = int(os.getenv("PRODUCT_ID", "1"))
PRODUCT_NAME = os.getenv("PRODUCT_NAME", "Курс самомасажу")
AMOUNT = float(os.getenv("AMOUNT", "290"))
CURRENCY = os.getenv("CURRENCY", "UAH")

BOT_USERNAME = os.getenv("BOT_USERNAME")

if not all([BOT_TOKEN, CHANNEL_ID, PAYMENT_BUTTON_URL, KEEP_ALIVE_URL, BOT_USERNAME]):
    raise RuntimeError("Missing ENV variables")

# ===================== APP =====================

app = FastAPI()
telegram_app = Application.builder().token(BOT_TOKEN).build()

DB_PATH = "database.db"
db = None

# ===================== DB =====================

async def get_db():
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
            has_access INTEGER DEFAULT 0,
            awaiting_payment INTEGER DEFAULT 0
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
            created_at INTEGER,
            paid_at INTEGER
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS access_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            invite_link TEXT,
            created_at INTEGER,
            used INTEGER DEFAULT 0
        )
    """)

    await conn.commit()


async def upsert_user(user):
    conn = await get_db()
    now = int(time.time())

    await conn.execute("""
        INSERT OR IGNORE INTO users
        (telegram_id, username, first_name, joined_at, last_activity)
        VALUES (?, ?, ?, ?, ?)
    """, (user.id, user.username, user.first_name, now, now))

    await conn.execute("""
        UPDATE users SET last_activity = ?
        WHERE telegram_id = ?
    """, (now, user.id))

    await conn.commit()


async def create_invite_link(user_id: int) -> str:
    invite = await telegram_app.bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        member_limit=1
    )

    conn = await get_db()
    await conn.execute("""
        INSERT INTO access_links (telegram_id, invite_link, created_at)
        VALUES (?, ?, ?)
    """, (user_id, invite.invite_link, int(time.time())))

    await conn.commit()
    return invite.invite_link


def is_admin(update: Update) -> bool:
    return update.effective_user.id == ADMIN_ID

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

# ===================== /start =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await upsert_user(user)

    conn = await get_db()
    args = context.args or []

    # === RETURN FROM PAYMENT ===
    if args and args[0] == "paid":
        cur = await conn.execute(
            "SELECT awaiting_payment, has_access FROM users WHERE telegram_id = ?",
            (user.id,)
        )
        row = await cur.fetchone()

        if not row or row["awaiting_payment"] == 0:
            await update.message.reply_text(
                "Я не бачу активної оплати для Вашого акаунту.\n"
                "Скористайтесь кнопкою підтримки нижче 🙏",
                parse_mode="HTML"
            )
            return

        if row["has_access"] == 1:
            await update.message.reply_text(
                "✅ У Вас вже є доступ.\nСкористайтесь /access",
                parse_mode="HTML"
            )
            return

        now = int(time.time())

        await conn.execute("""
            INSERT INTO purchases
            (telegram_id, product_id, amount, currency, status, created_at, paid_at)
            VALUES (?, ?, ?, ?, 'approved', ?, ?)
        """, (user.id, PRODUCT_ID, AMOUNT, CURRENCY, now, now))

        await conn.execute("""
            UPDATE users
            SET has_access = 1, awaiting_payment = 0
            WHERE telegram_id = ?
        """, (user.id,))

        await conn.commit()

        link = await create_invite_link(user.id)

        await update.message.reply_text(
            "🎉 <b>Оплата успішна!</b>\n\nОсь Ваш доступ:\n" + link,
            parse_mode="HTML"
        )
        return

    # === NORMAL START ===
    await conn.execute(
        "UPDATE users SET awaiting_payment = 1 WHERE telegram_id = ?",
        (user.id,)
    )
    await conn.commit()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Оплатити курс", url=PAYMENT_BUTTON_URL)],
        [InlineKeyboardButton("🆘 Написати в підтримку", callback_data="support_menu")]
    ])

    if args and args[0] == "site":
        txt = (
            "Вітаю! 👋\n\n"
            "Ви перейшли з сайту <b>Сам Собі Масажист</b>.\n\n"
            "Натисніть кнопку нижче, щоб оплатити курс і отримати доступ "
            "у приватний канал з відеоуроками ❤️\n\n"
            
        )
    else:
        txt = (
            "Вітаю! 👋\n\n"
            "Це бот доступу до курсу самомасажу.\n\n"
            "Натисніть кнопку <b>“Оплатити курс”</b>\n"
            "<b>Після оплати Ви автоматично отримаєте особистий доступ у приватний канал❤️</b>"
        )

telegram_app.add_handler(CommandHandler("start", start))

# ===================== SUPPORT MENU =====================

async def support_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Не прийшло посилання", callback_data="support_no_link")],
        [InlineKeyboardButton("🔁 Загубив посилання", callback_data="support_lost_link")],
        [InlineKeyboardButton("✍️ Інше питання", callback_data="support_other")]
    ])

    await query.message.reply_text(
        "❓ <b>Оберіть проблему:</b>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

telegram_app.add_handler(CallbackQueryHandler(support_menu_cb, pattern="^support_menu$"))

# ===================== SUPPORT: NO LINK =====================

async def support_no_link_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    conn = await get_db()

    cur = await conn.execute(
        "SELECT awaiting_payment, has_access FROM users WHERE telegram_id = ?",
        (user.id,)
    )
    row = await cur.fetchone()

    if not row or row["awaiting_payment"] == 0:
        await query.message.reply_text(
            "❌ Я не бачу активної оплати.\nНапишіть, будь ласка, деталі.",
            parse_mode="HTML"
        )
        return

    if row["has_access"] == 1:
        link = await create_invite_link(user.id)
        await query.message.reply_text("🔑 Ось Ваш доступ:\n" + link)
        return

    now = int(time.time())

    await conn.execute("""
        INSERT INTO purchases
        (telegram_id, product_id, amount, currency, status, created_at, paid_at)
        VALUES (?, ?, ?, ?, 'approved', ?, ?)
    """, (user.id, PRODUCT_ID, AMOUNT, CURRENCY, now, now))

    await conn.execute("""
        UPDATE users SET has_access = 1, awaiting_payment = 0
        WHERE telegram_id = ?
    """, (user.id,))

    await conn.commit()

    link = await create_invite_link(user.id)
    await query.message.reply_text("🎉 Оплату підтверджено!\n" + link)

telegram_app.add_handler(CallbackQueryHandler(support_no_link_cb, pattern="^support_no_link$"))

# ===================== SUPPORT: LOST LINK =====================

async def support_lost_link_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    conn = await get_db()

    cur = await conn.execute(
        "SELECT has_access FROM users WHERE telegram_id = ?",
        (user.id,)
    )
    row = await cur.fetchone()

    if not row or row["has_access"] == 0:
        await query.message.reply_text("❌ У Вас ще немає доступу.")
        return

    link = await create_invite_link(user.id)
    await query.message.reply_text("🔁 Нове посилання:\n" + link)

telegram_app.add_handler(CallbackQueryHandler(support_lost_link_cb, pattern="^support_lost_link$"))

# ===================== SUPPORT: OTHER =====================

async def support_other_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("✍️ Напишіть Ваше питання повідомленням.")

telegram_app.add_handler(CallbackQueryHandler(support_other_cb, pattern="^support_other$"))

# ===================== /access =====================

async def access_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await upsert_user(user)

    conn = await get_db()
    cur = await conn.execute(
        "SELECT has_access FROM users WHERE telegram_id = ?",
        (user.id,)
    )
    row = await cur.fetchone()

    if not row or row["has_access"] == 0:
        await update.message.reply_text("❌ У Вас немає доступу.")
        return

    link = await create_invite_link(user.id)
    await update.message.reply_text("🔑 Ваш доступ:\n" + link)

telegram_app.add_handler(CommandHandler("access", access_cmd))

# ===================== /stats =====================

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    conn = await get_db()
    now = int(time.time())

    def since(days): return now - days * 86400

    cur = await conn.execute("SELECT COUNT(*) c FROM users")
    users = (await cur.fetchone())["c"]

    cur = await conn.execute("SELECT COUNT(*) c FROM purchases WHERE status='approved'")
    paid = (await cur.fetchone())["c"]

    cur = await conn.execute("SELECT COALESCE(SUM(amount),0) s FROM purchases WHERE status='approved'")
    revenue = (await cur.fetchone())["s"]

    async def p(days):
        cur = await conn.execute("""
            SELECT COUNT(*) c, COALESCE(SUM(amount),0) s
            FROM purchases
            WHERE status='approved' AND paid_at >= ?
        """, (since(days),))
        r = await cur.fetchone()
        return r["c"], r["s"]

    d, ds = await p(1)
    w, ws = await p(7)
    m, ms = await p(30)
    q, qs = await p(90)

    await update.message.reply_text(
        f"<b>Статистика</b>\n\n"
        f"👥 Користувачі: <b>{users}</b>\n"
        f"💳 Покупці: <b>{paid}</b>\n"
        f"💰 Дохід: <b>{revenue} UAH</b>\n\n"
        f"📅 24 год: {d} – {ds} UAH\n"
        f"📆 7 днів: {w} – {ws} UAH\n"
        f"🗓 30 днів: {m} – {ms} UAH\n"
        f"📈 90 днів: {q} – {qs} UAH",
        parse_mode="HTML"
    )

telegram_app.add_handler(CommandHandler("stats", stats_cmd))

# ===================== USER MESSAGES =====================

async def user_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        return
    if not update.message or update.message.text.startswith("/"):
        return

    await telegram_app.bot.send_message(
        SUPPORT_CHAT_ID,
        f"💬 Від {update.effective_user.id}:\n{update.message.text}"
    )

telegram_app.add_handler(MessageHandler(filters.TEXT, user_messages))

# ===================== PAYMENT SUCCESS PAGE =====================

@app.get("/payment/success", response_class=HTMLResponse)
async def payment_success():
    return f"""
<!DOCTYPE html>
<html>
<body style="font-family:sans-serif;text-align:center;padding:40px">
<h2>Оплата успішна ✅</h2>
<p>Натисніть кнопку нижче, щоб отримати доступ</p>
<a href="https://t.me/{BOT_USERNAME}?start=paid"
style="display:inline-block;padding:18px 36px;
background:#0088cc;color:white;
border-radius:30px;text-decoration:none;font-size:18px">
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
