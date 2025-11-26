import os
import hmac
import hashlib
import time
import re

import aiohttp
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ===================== CONFIG =====================

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
WAYFORPAY_MERCHANT = os.getenv("WAYFORPAY_MERCHANT")
WAYFORPAY_SECRET = os.getenv("WAYFORPAY_SECRET")
MERCHANT_DOMAIN = os.getenv("MERCHANT_DOMAIN", "yourdomain.com")
PRODUCT_NAME = os.getenv("PRODUCT_NAME", "Massage Course")
AMOUNT = float(os.getenv("AMOUNT", "200.00"))
CURRENCY = os.getenv("CURRENCY", "UAH")
SERVICE_URL = os.getenv("SERVICE_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID missing")

app = FastAPI()

pending_orders = {}  # {order_ref: user_id}
user_links = {}       # {user_id: invite_link}

telegram_app = Application.builder().token(BOT_TOKEN).build()


# ===================== SIGNATURE HELPERS =====================

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


# ===================== HELPERS =====================

async def create_one_time_link(user_id: int) -> str:
    """
    Створює одноразовий інвайт-лінк без строку дії.
    """
    invite = await telegram_app.bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        member_limit=1  # тільки 1 користувач
    )

    link = invite.invite_link
    user_links[user_id] = link
    return link


# ===================== TELEGRAM HANDLERS =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Оплатити курс", callback_data="pay")],
        [InlineKeyboardButton("🧪 Тестова оплата", callback_data="testpay")],
    ])

    txt = (
        "Привіт! 👋\n\n"
        "Це бот доступу до курсу самомасажу.\n"
        "Натисни кнопку нижче, щоб отримати доступ.\n\n"
        "Після оплати бот автоматично видасть одноразовий лінк у приватний канал."
    )

    await update.message.reply_text(txt, reply_markup=keyboard)


# ---------- TEST PAYMENT ----------
async def testpay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    try:
        link = await create_one_time_link(user_id)

        await telegram_app.bot.send_message(
            chat_id=user_id,
            text=(
                "🧪 *Тестова оплата успішна!*\n\n"
                "Ось твій одноразовий доступ у канал з уроками:\n"
                f"{link}"
            ),
            parse_mode="Markdown"
        )

    except Exception as e:
        await query.message.reply_text(
            f"Помилка при створенні інвайт-лінку:\n`{e}`",
            parse_mode="Markdown"
        )
        return

    await query.message.reply_text("Готово! Перейди за лінком вище 🎉")


# ---------- REAL PAYMENT ----------
async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    order_ref = f"order_{user_id}"
    pending_orders[order_ref] = user_id

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
        await query.message.reply_text(
            "Помилка при створенні інвойсу. Спробуйте ще раз."
        )
        return

    txt = (
        "Готово! 🎉\n\n"
        "Оплатіть за посиланням:\n"
        f"{invoice}\n\n"
        "Після оплати бот автоматично видасть доступ у канал."
    )

    await query.message.reply_text(txt)


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CallbackQueryHandler(pay, pattern="^pay$"))
telegram_app.add_handler(CallbackQueryHandler(testpay, pattern="^testpay$"))


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
async def wfp_callback(request: Request):
    body = await request.json()

    if not wfp_callback_valid(body):
        return {"code": "error", "message": "bad signature"}

    order_ref = body.get("orderReference")
    status = body.get("transactionStatus")

    m = re.match(r"order_(\d+)", order_ref)
    if not m:
        return {"code": "error"}

    user_id = int(m.group(1))

    if status == "Approved":
        link = await create_one_time_link(user_id)

        await telegram_app.bot.send_message(
            chat_id=user_id,
            text=(
                "Оплата успішна! 🎉\n\n"
                "Ось твій доступ у приватний канал:\n"
                f"{link}"
            )
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
