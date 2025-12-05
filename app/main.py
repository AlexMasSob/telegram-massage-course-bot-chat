import os
import logging
import hashlib
import hmac
import aiohttp
from datetime import datetime
from aiohttp import web
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
import aiosqlite

# -------------------------------------------------------
# ЛОГИ
# -------------------------------------------------------
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# -------------------------------------------------------
# ENV ЗМІННІ
# -------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
ADMIN_ID = int(os.getenv("ADMIN_ID"))
AMOUNT = float(os.getenv("AMOUNT", "290"))
MERCHANT_LOGIN = os.getenv("MERCHANT_LOGIN")
MERCHANT_SECRET = os.getenv("MERCHANT_SECRET")
MERCHANT_DOMAIN = os.getenv("MERCHANT_DOMAIN")
SERVICE_URL = os.getenv("SERVICE_URL")
WAYFORPAY_BUTTON_URL = "https://secure.wayforpay.com/button/ba6a191c6ba56"
KEEP_ALIVE = os.getenv("KEEP_ALIVE", "True") == "True"

# -------------------------------------------------------
# ІНІЦІАЛІЗАЦІЯ БАЗИ
# -------------------------------------------------------
async def init_db():
    async with aiosqlite.connect("database.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                registered_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                order_ref TEXT UNIQUE,
                amount REAL,
                created_at TEXT
            )
        """)
        await db.commit()

# -------------------------------------------------------
# ЗБЕРЕГТИ КОРИСТУВАЧА
# -------------------------------------------------------
async def save_user(update: Update):
    async with aiosqlite.connect("database.db") as db:
        user = update.effective_user
        await db.execute("""
            INSERT OR IGNORE INTO users (user_id, username, first_name, last_name, registered_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            user.id, user.username, user.first_name, user.last_name,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))
        await db.commit()

# -------------------------------------------------------
# КНОПКИ
# -------------------------------------------------------
def main_menu(from_site=False):
    if from_site:
        text = (
            "Вітаю! 👋\n\n"
            "Ви перейшли з сайту *Сам Собі Масажист*.\n"
            "Натисніть кнопку нижче, щоб оплатити курс і отримати доступ у приватний канал з відеоуроками ❤️"
        )
    else:
        text = (
            "Вітаю! 👋\n\n"
            "Це бот доступу до курсу самомасажу.\n"
            "Натисніть кнопку нижче, щоб отримати доступ.\n\n"
            "*Після оплати Ви автоматично отримаєте особистий доступ у приватний канал.*"
        )

    keyboard = [
        [InlineKeyboardButton("💳 Оплатити курс", url=WAYFORPAY_BUTTON_URL)],
        [InlineKeyboardButton("🧪 Тестова оплата", callback_data="test_pay")]
    ]
    return text, InlineKeyboardMarkup(keyboard)

# -------------------------------------------------------
# /START
# -------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await save_user(update)
    ref = context.args[0] if context.args else ""
    from_site = (ref == "site")
    text, keyboard = main_menu(from_site)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

# -------------------------------------------------------
# ТЕСТОВА ОПЛАТА
# -------------------------------------------------------
async def test_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    link = f"https://t.me/+testTestTestLink"
    await query.message.reply_text(
        "🧪 *Тестова оплата успішна!*\n\n"
        "Ось Ваш особистий доступ у канал з уроками:\n"
        f"{link}",
        parse_mode="Markdown"
    )

# -------------------------------------------------------
# CALLBACK WAYFORPAY
# -------------------------------------------------------
async def wayforpay_callback(request):
    try:
        data = await request.json()
        logging.info(f"WFP CALLBACK: {data}")

        order_ref = data.get("orderReference")
        amount = float(data.get("amount", 0))
        user_id = int(data.get("clientId"))

        async with aiosqlite.connect("database.db") as db:
            await db.execute("""
                INSERT OR IGNORE INTO purchases (user_id, order_ref, amount, created_at)
                VALUES (?, ?, ?, ?)
            """, (
                user_id, order_ref, amount,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ))
            await db.commit()

        # ВИДАЧА ОСОБИСТОГО ЛІНКУ
        invite_link = await request.app['bot'].create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,
            expires_date=None
        )

        await request.app['bot'].send_message(
            chat_id=user_id,
            text=(
                "🎉 *Оплата успішна!*\n\n"
                "Ось Ваш особистий доступ у канал з уроками:\n"
                f"{invite_link.invite_link}"
            ),
            parse_mode="Markdown"
        )

        return web.Response(text="OK")

    except Exception as e:
        logging.error(f"ERROR CALLBACK: {e}")
        return web.Response(status=500, text="ERROR")

# -------------------------------------------------------
# СТАТИСТИКА
# -------------------------------------------------------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    async with aiosqlite.connect("database.db") as db:
        users = await db.execute_fetchall("SELECT COUNT(*) FROM users")
        purchases = await db.execute_fetchall("SELECT COUNT(*) FROM purchases")
        total = await db.execute_fetchall("SELECT SUM(amount) FROM purchases")

    await update.message.reply_text(
        f"📊 *Статистика*\n\n"
        f"👥 Користувачів: {users[0][0]}\n"
        f"💰 Продажів: {purchases[0][0]}\n"
        f"📦 Дохід: {total[0][0] or 0} грн",
        parse_mode="Markdown"
    )

# -------------------------------------------------------
# СТАРТ БОТА
# -------------------------------------------------------
async def main():
    await init_db()

    app = web.Application()
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Telegram handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(test_pay, pattern="test_pay"))
    application.add_handler(CommandHandler("stats", stats))

    # WayForPay callback route
    bot = application.bot
    app['bot'] = bot
    app.router.add_post("/wayforpay/callback", wayforpay_callback)

    # Run bot
    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", 10000)
    await site.start()

    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    await application.updater.wait_for_stop()

import asyncio
asyncio.run(main())
