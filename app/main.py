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
    "https://secure.wayforpay.com/button/ba6a191c6ba56"
)

PRODUCT_ID = int(os.getenv("PRODUCT_ID", "1"))
PRODUCT_NAME = os.getenv("PRODUCT_NAME", "Курс самомасажу")
AMOUNT = float(os.getenv("AMOUNT", "290.00"))
CURRENCY = os.getenv("CURRENCY", "UAH")

KEEP_ALIVE_URL = os.getenv("KEEP_ALIVE_URL")

ADMIN_ID = int(os.getenv("ADMIN_ID", "268351523"))
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID", "-5032163085"))

BOT_USERNAME = os.getenv("BOT_USERNAME", "Massagesobi_bot")

if not BOT_TOKEN or not CHANNEL_ID or not KEEP_ALIVE_URL:
    raise RuntimeError("Missing ENV variables")

app = FastAPI()

DB_PATH = "database.db"
db = None

telegram_app = Application.builder().token(BOT_TOKEN).build()

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
            created_at INTEGER
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
        UPDATE users SET last_activity = ? WHERE telegram_id = ?
    """, (now, user.id))

    await conn.commit()


# ===================== KEEP ALIVE =====================

async def keep_alive():
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                await s.get(KEEP_ALIVE_URL)
        except:
            pass
        await asyncio.sleep(300)


@app.on_event("startup")
async def startup():
    await telegram_app.initialize()
    await telegram_app.start()
    await init_db()
    asyncio.create_task(keep_alive())


# ===================== HELPERS =====================

async def create_one_time_link(user_id):
    invite = await telegram_app.bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        member_limit=1
    )
    conn = await get_db()
    await conn.execute("""
        INSERT INTO access_links (telegram_id, product_id, invite_link, created_at)
        VALUES (?, ?, ?, ?)
    """, (user_id, PRODUCT_ID, invite.invite_link, int(time.time())))
    await conn.commit()
    return invite.invite_link


# ===================== /start =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await upsert_user(user)

    args = context.args or []
    conn = await get_db()
    
    # === RETURN FROM PAYMENT ===
    if args and args[0] == "paid":
        cur = await conn.execute(
            "SELECT awaiting_payment FROM users WHERE telegram_id = ?",
            (user.id,)
        )
        row = await cur.fetchone()

        if not row or row["awaiting_payment"] == 0:
            await update.message.reply_text(
                "Я не бачу активної оплати. Якщо ви оплатили — напишіть у підтримку 🙏"
            )
            return

        now = int(time.time())
        await conn.execute("""
            INSERT INTO purchases
            (telegram_id, product_id, amount, currency, status, order_ref, created_at, paid_at)
            VALUES (?, ?, ?, ?, 'approved', ?, ?, ?)
        """, (user.id, PRODUCT_ID, AMOUNT, CURRENCY, f"order_{user.id}_{now}", now, now))

        await conn.execute("""
            UPDATE users SET awaiting_payment = 0, has_access = 1 WHERE telegram_id = ?
        """, (user.id,))
        await conn.commit()

        link = await create_one_time_link(user.id)
        await update.message.reply_text(
            f"🎉 Оплата успішна!\n\nОсь ваш доступ:\n{link}"
        )
        return

    # === NORMAL START ===
    await conn.execute(
        "UPDATE users SET awaiting_payment = 1 WHERE telegram_id = ?",
        (user.id,)
    )
    await conn.commit()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Оплатити курс", url=PAYMENT_BUTTON_URL)]
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

    await update.message.reply_text(txt, reply_markup=keyboard, parse_mode="HTML")


telegram_app.add_handler(CommandHandler("start", start))


# ===================== PAYMENT SUCCESS PAGE =====================

@app.get("/payment/success", response_class=HTMLResponse)
async def payment_success():
    return f"""
<html>
<body style="text-align:center;font-family:sans-serif">
<h2>Оплата успішна ✅</h2>
<p>Натисніть кнопку нижче, щоб отримати доступ</p>
<a href="https://t.me/{BOT_USERNAME}?start=paid"
style="padding:14px 26px;background:#0088cc;color:white;
text-decoration:none;border-radius:30px;">
Отримати доступ
</a>
</body>
</html>
"""

# ===================== /access =====================

async def access_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await upsert_user(user.id, user.username, user.first_name)

    conn = await get_db()

    # перевіряємо, чи є успішна оплата
    cur = await conn.execute("""
        SELECT COUNT(*) AS c
        FROM purchases
        WHERE telegram_id = ? AND status = 'approved'
    """, (user.id,))
    row = await cur.fetchone()

    # ❌ ще НЕ купував
    if row["c"] == 0:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Оплатити курс", url=PAYMENT_BUTTON_URL)]
        ])

        await update.message.reply_text(
            "<b>У Вас ще немає активного доступу.</b>\n\n"
            "Щоб отримати доступ до курсу — натисніть кнопку нижче та сплатіть курс 👇",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        return

    # ✅ вже купував → видаємо новий одноразовий доступ
    try:
        link = await create_one_time_link(user.id, PRODUCT_ID)

        await update.message.reply_text(
            "🔑 <b>Ось Ваш особистий доступ у приватний канал з уроками:</b>\n\n"
            f"{link}\n\n"
            "Якщо не зможете зайти — просто повторіть команду /access 🙂",
            parse_mode="HTML"
        )

    except Exception as e:
        await update.message.reply_text(
            "Сталася помилка при створенні доступу 😔\n"
            "Будь ласка, напишіть у підтримку — я все вирішу вручну.\n\n"
            f"<code>{e}</code>",
            parse_mode="HTML"
        )


telegram_app.add_handler(CommandHandler("access", access_cmd))

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


# ===================== WAYFORPAY CALLBACK (резерв, не використовується) =====================

@app.post("/wayforpay/callback")
async def wfp_callback(request: Request):
    body = await request.json()
    print("WayForPay callback (currently unused):", body)
    return {"status": "ok"}


# ===================== HTML СТОРІНКА УСПІШНОЇ ОПЛАТИ =====================

@app.get("/payment/success", response_class=HTMLResponse)
async def payment_success_get():
    """
    Approved URL для WayForPay (GET).
    Саме цю сторінку бачить користувач після успішної оплати.
    """
    html = f"""
<!DOCTYPE html>
<html lang="uk">
<head>
  <meta charset="UTF-8">
  <title>Оплата успішна</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">

  <style>
    body {{
      margin: 0;
      padding: 0;
      background: #f4f6f8;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    }}
    .card {{
      max-width: 420px;
      margin: 60px auto;
      background: #ffffff;
      padding: 28px;
      border-radius: 16px;
      box-shadow: 0 10px 30px rgba(0,0,0,0.08);
      text-align: center;
    }}
    h1 {{
      margin-top: 0;
      font-size: 22px;
    }}
    p {{
      font-size: 15px;
      line-height: 1.5;
      color: #333;
    }}
    a.button {{
      display: inline-block;
      margin-top: 18px;
      padding: 14px 26px;
      background: #0088cc;
      color: #fff;
      text-decoration: none;
      border-radius: 999px;
      font-weight: 600;
    }}
    a.button:active {{
      transform: scale(0.97);
    }}
  </style>
</head>

<body>
  <div class="card">
    <h1>Оплата успішна ✅</h1>

    <p>
      Дякую за оплату курсу<br>
      <b>{PRODUCT_NAME}</b>
    </p>

    <a class="button" href="https://t.me/{BOT_USERNAME}?start=paid">
      Отримати доступ до курсу
    </a>

    <p style="margin-top:16px;font-size:13px;color:#666;">
      Якщо кнопка не відкрилась — відкрийте Telegram і напишіть боту<br>
      <b>@{BOT_USERNAME}</b>
    </p>
  </div>
</body>
</html>
"""
    return HTMLResponse(content=html, status_code=200)


@app.post("/payment/success")
async def payment_success_post(request: Request):
    """
    POST-запит від WayForPay (якщо увімкнена галочка).
    Нам достатньо просто повернути OK.
    """
    body = await request.body()
    print("WayForPay POST to /payment/success:")
    print(body.decode("utf-8", errors="ignore"))
    return {"status": "ok"}



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


