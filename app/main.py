import os
import time
import asyncio
import aiohttp
import aiosqlite
import secrets

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

CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID", "0"))

PAYMENT_BUTTON_URL = os.getenv("PAYMENT_BUTTON_URL")
KEEP_ALIVE_URL = os.getenv("KEEP_ALIVE_URL")

PRODUCT_ID = int(os.getenv("PRODUCT_ID", "1"))
PRODUCT_NAME = os.getenv("PRODUCT_NAME", "Курс самомасажу")
AMOUNT = float(os.getenv("AMOUNT", "290"))
CURRENCY = os.getenv("CURRENCY", "UAH")

BOT_USERNAME = os.getenv("BOT_USERNAME")

# Optional (не обов'язково; в цьому коді не потрібен)
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip()

missing = []
if not BOT_TOKEN: missing.append("BOT_TOKEN")
if not WEBHOOK_TOKEN: missing.append("WEBHOOK_TOKEN")
if not CHANNEL_ID: missing.append("CHANNEL_ID")
if not ADMIN_ID: missing.append("ADMIN_ID")
if not SUPPORT_CHAT_ID: missing.append("SUPPORT_CHAT_ID")
if not PAYMENT_BUTTON_URL: missing.append("PAYMENT_BUTTON_URL")
if not KEEP_ALIVE_URL: missing.append("KEEP_ALIVE_URL")
if not BOT_USERNAME: missing.append("BOT_USERNAME")

if missing:
    raise RuntimeError("Missing ENV variables: " + ", ".join(missing))

# ===================== APP =====================

app = FastAPI()
telegram_app = Application.builder().token(BOT_TOKEN).build()

DB_PATH = "database.db"
db: aiosqlite.Connection | None = None


# ===================== DB =====================

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
            has_access INTEGER DEFAULT 0,
            awaiting_payment INTEGER DEFAULT 0,
            support_mode INTEGER DEFAULT 0
        )
    """)

    # На випадок якщо таблиця існувала без колонок:
    for stmt in [
        "ALTER TABLE users ADD COLUMN has_access INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN awaiting_payment INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN support_mode INTEGER DEFAULT 0",
    ]:
        try:
            await conn.execute(stmt)
        except Exception:
            pass

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

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS gifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            buyer_telegram_id INTEGER,
            gift_code TEXT UNIQUE,
            is_used INTEGER DEFAULT 0,
            created_at INTEGER,
            used_at INTEGER
        )
    """)

    await conn.commit()


async def upsert_user(user):
    conn = await get_db()
    now = int(time.time())

    await conn.execute("""
        INSERT OR IGNORE INTO users
        (telegram_id, username, first_name, joined_at, last_activity, has_access, awaiting_payment, support_mode)
        VALUES (?, ?, ?, ?, ?, 0, 0, 0)
    """, (user.id, user.username, user.first_name, now, now))

    await conn.execute("""
        UPDATE users
        SET username = ?, first_name = ?, last_activity = ?
        WHERE telegram_id = ?
    """, (user.username, user.first_name, now, user.id))

    await conn.commit()


async def set_support_mode(user_id: int, mode: int):
    conn = await get_db()
    await conn.execute("UPDATE users SET support_mode = ? WHERE telegram_id = ?", (mode, user_id))
    await conn.commit()


async def get_user_row(user_id: int):
    conn = await get_db()
    cur = await conn.execute("SELECT * FROM users WHERE telegram_id = ?", (user_id,))
    return await cur.fetchone()


async def create_invite_link(user_id: int) -> str:
    invite = await telegram_app.bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        member_limit=1
    )

    conn = await get_db()
    await conn.execute("""
        INSERT INTO access_links (telegram_id, invite_link, created_at, used)
        VALUES (?, ?, ?, 0)
    """, (user_id, invite.invite_link, int(time.time())))

    await conn.commit()
    return invite.invite_link

async def create_gift(buyer_id: int) -> str:
    conn = await get_db()
    code = secrets.token_urlsafe(16)
    now = int(time.time())

    await conn.execute("""
        INSERT INTO gifts (buyer_telegram_id, gift_code, created_at)
        VALUES (?, ?, ?)
    """, (buyer_id, code, now))

    await conn.commit()
    return code


def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID


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


# ===================== WEBHOOK ENDPOINT (ВАЖЛИВО) =====================

@app.post("/telegram/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")

    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}


# ===================== /start =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await upsert_user(user)

    conn = await get_db()
    args = context.args or []

    # якщо користувач був у режимі "інше питання" — вимикаємо при /start
    await set_support_mode(user.id, 0)

   # === RETURN FROM GIFT LINK ===
if args and args[0].startswith("gift_"):
    gift_code = args[0].replace("gift_", "")
    conn = await get_db()

    cur = await conn.execute("""
        SELECT id, is_used FROM gifts WHERE gift_code = ?
    """, (gift_code,))
    gift = await cur.fetchone()

    if not gift:
        await update.message.reply_text("❌ Цей подарунок недійсний.")
        return

    if gift["is_used"] == 1:
        await update.message.reply_text("⚠️ Цей подарунок вже був використаний.")
        return

    # видаємо доступ
    link = await create_invite_link(user.id)
    now = int(time.time())

    await conn.execute("""
        UPDATE gifts SET is_used = 1, used_at = ?
        WHERE id = ?
    """, (now, gift["id"]))

    await conn.execute("""
        UPDATE users SET has_access = 1, last_activity = ?
        WHERE telegram_id = ?
    """, (now, user.id))

    await conn.commit()

    await update.message.reply_text(
        "🎁 <b>Вам зробили подарунок!</b>\n\n"
        "Ви отримали доступ до курсу\n"
        "<b>«Сам Собі Масажист»</b> 💙\n\n"
        "🔑 Ось ваш персональний доступ:\n"
        f"{link}",
        parse_mode="HTML"
    )
    return
    
    if args and args[0] == "paid":
        row = await get_user_row(user.id)

        if not row or row["awaiting_payment"] == 0:
            await update.message.reply_text(
                "Я не бачу активної оплати для Вашого акаунту.\n\n"
                "Якщо Ви оплатили, але не отримали доступ — натисніть 🆘 <b>Підтримка</b> нижче 🙏",
                parse_mode="HTML"
            )
            return

        if row["has_access"] == 1:
            await update.message.reply_text(
                "✅ У Вас вже є доступ.\n\n"
                "Якщо загубили посилання — натисніть 🆘 <b>Підтримка</b> → «Загубив посилання».",
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
            SET has_access = 1, awaiting_payment = 0, last_activity = ?
            WHERE telegram_id = ?
        """, (now, user.id))

        await conn.commit()

        link = await create_invite_link(user.id)

        await update.message.reply_text(
            "🎉 <b>Оплата успішна!</b>\n\n"
            "🔑 Ваш доступ:\n"
            f"{link}",
            parse_mode="HTML"
        )
        return

    # === NORMAL START ===
    await conn.execute(
        "UPDATE users SET awaiting_payment = 1, last_activity = ? WHERE telegram_id = ?",
        (int(time.time()), user.id)
    )
    await conn.commit()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Оплатити курс для себе", url=PAYMENT_BUTTON_URL)],
        [InlineKeyboardButton("🎁 Купити курс в подарунок", callback_data="buy_gift")],
        [InlineKeyboardButton("✉️ Написати в підтримку", callback_data="support:menu")]
    ])

    if args and args[0] == "site":
        txt = (
            "Вітаю! 👋\n\n"
            "Ви перейшли з сайту <b>Сам Собі Масажист</b>.\n\n"
            "Тут ви можете:\n"
            "• придбати курс для себе\n"
            "• або зробити корисний подарунок близькій людині 🎁\n\n"
            "Оберіть потрібний варіант нижче, щоб оплатити курс і отримати доступ "
            "у приватний канал з відеоуроками ❤️👇"
        )
    else:
        txt = (
            "Вітаю! 👋\n\n"
            "Це бот доступу до курсу самомасажу.\n\n"
            "Тут ви можете:\n"
            "• придбати курс для себе\n"
            "• або зробити корисний подарунок близькій людині 🎁\n\n"
            "Оберіть потрібний варіант нижче, щоб оплатити курс і отримати доступ "
            "у приватний канал з відеоуроками ❤️👇"
        )

    await update.message.reply_text(txt, reply_markup=keyboard, parse_mode="HTML")


telegram_app.add_handler(CommandHandler("start", start))


# ===================== SUPPORT MENU (callback) =====================

async def support_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user = q.from_user
    await upsert_user(user)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❗ Не прийшло посилання на курс", callback_data="support:nolink")],
        [InlineKeyboardButton("🔁 Загубив посилання", callback_data="support:lost")],
        [InlineKeyboardButton("💬 Інше питання", callback_data="support:other")],
    ])

    await q.message.reply_text(
        "🆘 <b>Підтримка</b>\n\n"
        "Оберіть, що сталося:",
        reply_markup=kb,
        parse_mode="HTML"
    )


async def support_no_link_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user = q.from_user
    await upsert_user(user)

    row = await get_user_row(user.id)

    # Якщо доступ вже є — просто видаємо новий лінк (надійніше і швидше)
    if row and row["has_access"] == 1:
        link = await create_invite_link(user.id)
        await q.message.reply_text(
            "✅ Бачу, що доступ вже активний.\n\n"
            "🔑 Ось нове посилання:\n" + link,
            parse_mode="HTML"
        )
        return

    # Якщо очікував оплату — пояснюємо, що треба натиснути кнопку "Отримати доступ" на сторінці успіху
    if row and row["awaiting_payment"] == 1:
        await q.message.reply_text(
            "Якщо Ви вже оплатили, але закрили сторінку після оплати — це ок.\n\n"
            "✅ Відкрийте підтвердження оплати у WayForPay і натисніть кнопку <b>«Отримати доступ»</b>.\n"
            "Вона поверне Вас у бота з позначкою оплати.\n\n"
            "Якщо не виходить — натисніть «Інше питання» і напишіть, що оплатили (додайте час оплати).",
            parse_mode="HTML"
        )
        return

    await q.message.reply_text(
        "Я поки не бачу активного платежу, пов'язаного з Вашим акаунтом.\n\n"
        "Якщо Ви оплатили — натисніть «Інше питання» і напишіть деталі (час/сума).",
        parse_mode="HTML"
    )


async def support_lost_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user = q.from_user
    await upsert_user(user)

    row = await get_user_row(user.id)

    if row and row["has_access"] == 1:
        link = await create_invite_link(user.id)
        await q.message.reply_text(
            "🔁 Оновив доступ.\n\n"
            "🔑 Ваше нове посилання:\n" + link,
            parse_mode="HTML"
        )
    else:
        await q.message.reply_text(
            "❌ У Вас поки немає активного доступу.\n\n"
            "Якщо Ви оплатили — оберіть «Не прийшло посилання» або «Інше питання».",
            parse_mode="HTML"
        )


async def support_other_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user = q.from_user
    await upsert_user(user)

    await set_support_mode(user.id, 1)

    await q.message.reply_text(
        "✍️ Напишіть Ваше питання одним повідомленням.\n\n"
        "Я передам його у підтримку, і Вам дадуть відповідь 🙏",
        parse_mode="HTML"
    )


telegram_app.add_handler(CallbackQueryHandler(support_menu_cb, pattern=r"^support:menu$"))
telegram_app.add_handler(CallbackQueryHandler(support_no_link_cb, pattern=r"^support:nolink$"))
telegram_app.add_handler(CallbackQueryHandler(support_lost_cb, pattern=r"^support:lost$"))
telegram_app.add_handler(CallbackQueryHandler(support_other_cb, pattern=r"^support:other$"))


# ===================== /access =====================

async def access_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await upsert_user(user)

    row = await get_user_row(user.id)

    if not row or row["has_access"] == 0:
        await update.message.reply_text("❌ У Вас немає активного доступу.", parse_mode="HTML")
        return

    link = await create_invite_link(user.id)
    await update.message.reply_text("🔑 Ваш доступ:\n" + link, parse_mode="HTML")


telegram_app.add_handler(CommandHandler("access", access_cmd))


# ===================== /stats =====================

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    conn = await get_db()
    now = int(time.time())

    def since(days: int) -> int:
        return now - days * 86400

    cur = await conn.execute("SELECT COUNT(*) AS c FROM users")
    total_users = (await cur.fetchone())["c"]

    cur = await conn.execute("SELECT COUNT(*) AS c FROM purchases WHERE status='approved'")
    total_paid = (await cur.fetchone())["c"]

    cur = await conn.execute("SELECT COALESCE(SUM(amount),0) AS s FROM purchases WHERE status='approved'")
    total_revenue = (await cur.fetchone())["s"]

    async def period_stats(days: int):
        cur = await conn.execute("""
            SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s
            FROM purchases
            WHERE status='approved' AND paid_at >= ?
        """, (since(days),))
        row = await cur.fetchone()
        return row["c"], row["s"]

    day_c, day_s = await period_stats(1)
    week_c, week_s = await period_stats(7)
    month_c, month_s = await period_stats(30)
    q_c, q_s = await period_stats(90)

    txt = (
        "<b>Статистика бота</b>\n\n"
        f"👥 Усього користувачів: <b>{total_users}</b>\n"
        f"💳 Усього покупців: <b>{total_paid}</b>\n"
        f"💰 Загальний дохід: <b>{round(total_revenue, 2)} UAH</b>\n\n"
        "<b>Продажі по періодах:</b>\n"
        f"📅 За 24 години: <b>{day_c}</b> купівель – <b>{round(day_s, 2)} UAH</b>\n"
        f"📆 За 7 днів: <b>{week_c}</b> купівель – <b>{round(week_s, 2)} UAH</b>\n"
        f"🗓 За 30 днів: <b>{month_c}</b> купівель – <b>{round(month_s, 2)} UAH</b>\n"
        f"📈 За 90 днів: <b>{q_c}</b> купівель – <b>{round(q_s, 2)} UAH</b>\n"
    )

    await update.message.reply_text(txt, parse_mode="HTML")


telegram_app.add_handler(CommandHandler("stats", stats_cmd))


# ===================== SUPPORT: USER TEXT FORWARDING =====================

async def user_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if update.effective_chat and update.effective_chat.type != "private":
        return

    user = update.effective_user
    if not user:
        return

    # адмін не пересилаємо
    if user.id == ADMIN_ID:
        return

    # не чіпаємо команди
    if update.message.text and update.message.text.startswith("/"):
        return

    await upsert_user(user)

    row = await get_user_row(user.id)
    if not row:
        return

    # пересилаємо тільки якщо користувач натиснув "Інше питання"
    if row["support_mode"] != 1:
        return

    text = update.message.text or update.message.caption or "(медіа без тексту)"

    try:
        await telegram_app.bot.send_message(
            SUPPORT_CHAT_ID,
            "💬 <b>Нове звернення в підтримку</b>\n\n"
            f"👤 ID: <code>{user.id}</code>\n"
            f"🔗 Username: @{user.username if user.username else 'немає'}\n"
            f"🙍‍♂️ Ім'я: <b>{user.first_name}</b>\n\n"
            f"📝 Текст:\n<code>{text}</code>",
            parse_mode="HTML"
        )

        # якщо це медіа — копіюємо
        if update.message.photo or update.message.video or update.message.document or update.message.audio or update.message.voice:
            await telegram_app.bot.copy_message(
                chat_id=SUPPORT_CHAT_ID,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id
            )

        await update.message.reply_text(
            "✅ Дякую! Передав у підтримку. Скоро Вам дадуть відповідь 🙏",
            parse_mode="HTML"
        )

        # Вимикаємо режим після одного звернення (щоб не спамило)
        await set_support_mode(user.id, 0)

    except Exception:
        # якщо не вдалось відправити в SUPPORT_CHAT_ID
        await update.message.reply_text(
            "❌ Не вдалося передати повідомлення в підтримку.\n"
            "Спробуйте ще раз або напишіть пізніше.",
            parse_mode="HTML"
        )


telegram_app.add_handler(MessageHandler(filters.ALL, user_messages))


# ===================== GIFT CALLBACK =====================

async def gift_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    gift_code = await create_gift(user.id)

    await query.message.reply_text(
        "🎁 Дякуємо за покупку подарунка!\n\n"
        "Ви придбали курс\n"
        "«Сам Собі Масажист»\n"
        "для близької людини 💙\n\n"
        "⛔️ Будь ласка, не натискайте кнопку доступу самостійно.\n\n"
        "👉 Перешліть наступне повідомлення людині,\n"
        "якій хочете зробити подарунок."
    )

    await query.message.reply_text(
        "🎁 Вам зробили подарунок!\n\n"
        "Для вас придбали курс\n"
        "«Сам Собі Масажист» 💆‍♀️\n\n"
        "Натисніть кнопку нижче,\n"
        "щоб отримати доступ до курсу 👇",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🔓 Отримати доступ",
                url=f"https://t.me/{BOT_USERNAME}?start=gift_{gift_code}"
            )]
        ])
    )


telegram_app.add_handler(
    CallbackQueryHandler(gift_callback, pattern="^buy_gift$")
)


# ===================== PAYMENT SUCCESS PAGE =====================

@app.get("/payment/success", response_class=HTMLResponse)
async def payment_success():
    return f"""
<!DOCTYPE html>
<html lang="uk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Оплата успішна</title>
    <style>
        body {{
            margin: 0;
            padding: 0;
            background: #f4f6f8;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                         Roboto, Helvetica, Arial, sans-serif;
        }}
        .card {{
            max-width: 420px;
            margin: 80px auto;
            background: #ffffff;
            padding: 32px 24px;
            border-radius: 18px;
            box-shadow: 0 12px 30px rgba(0,0,0,0.08);
            text-align: center;
        }}
        h1 {{
            font-size: 26px;
            margin: 0 0 12px 0;
        }}
        p {{
            font-size: 17px;
            line-height: 1.5;
            color: #333;
        }}
        a.button {{
            display: inline-block;
            margin-top: 24px;
            padding: 18px 34px;
            background: #0088cc;
            color: #ffffff;
            text-decoration: none;
            border-radius: 999px;
            font-size: 18px;
            font-weight: 600;
        }}
        a.button:active {{
            transform: scale(0.97);
        }}
        .hint {{
            margin-top: 20px;
            font-size: 14px;
            color: #666;
        }}
    </style>
</head>
<body>
    <div class="card">
        <h1>Оплата успішна ✅</h1>
        <p>
            Дякуємо за оплату!<br>
            Натисніть кнопку нижче, щоб отримати доступ до курсу.
        </p>
        <a class="button" href="https://t.me/{BOT_USERNAME}?start=paid">Отримати доступ</a>
        <div class="hint">
            Якщо кнопка не відкрилась — відкрийте Telegram<br>
            та напишіть боту <b>@{BOT_USERNAME}</b>
        </div>
    </div>
</body>
</html>
"""


# ===================== ROOT =====================

@app.get("/")
async def root():
    return {"status": "running"}
