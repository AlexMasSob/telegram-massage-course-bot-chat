import os
import hmac
import hashlib
import time
import re

import aiohttp
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ===================== CONFIG =====================

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "supersecret")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
WAYFORPAY_MERCHANT = os.getenv("WAYFORPAY_MERCHANT")
WAYFORPAY_SECRET = os.getenv("WAYFORPAY_SECRET")
MERCHANT_DOMAIN = os.getenv("MERCHANT_DOMAIN", "yourdomain.com")
PRODUCT_NAME = os.getenv("PRODUCT_NAME", "Massage Course")
AMOUNT = float(os.getenv("AMOUNT", "200.00"))
CURRENCY = os.getenv("CURRENCY", "UAH")
SERVICE_URL = os.getenv("SERVICE_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing!")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID missing!")

app = FastAPI()

pending_orders = {}
paid_users = set()

telegram_app = Application.builder().token(BOT_TOKEN).build()

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

    parts += [str(x) for x in payload["productName"]]
    parts += [str(x) for x in payload["productCount"]]
    parts += [str(x) for x in payload["productPrice"]]

    message = ";".join(parts)

    return hmac.new(
        WAYFORPAY_SECRET.encode(),
        message.encode(),
        hashlib.md5
    ).hexdigest()


def wfp_callback_valid(body: dict) -> bool:
    fields = [
        "merchantAccount", "orderReference", "amount", "currency",
        "authCode", "cardPan", "transactionStatus", "reasonCode",
        "merchantSignature"
    ]
    if not all(k in body for k in fields):
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

    message = ";".join(parts)
    expected = hmac.new(
        WAYFORPAY_SECRET.encode(),
        message.encode(),
        hashlib.md5
    ).hexdigest()

    return expected == body["merchantSignature"]


def wfp_response_signature(order_ref: str, status: str, timestamp: int) -> str:
    message = f"{order_ref};{status};{timestamp}"
    return hmac.new(
        WAYFORPAY_SECRET.encode(),
        message.encode(),
        hashlib.md5
    ).hexdigest()


# ===================== TELEGRAM STARTUP =====================

@app.on_event("startup")
async def startup_event():
    await telegram_app.initialize()
    await telegram_app.start()


# ===================== TELEGRAM HANDLERS =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Оплатити курс", callback_data="pay")],
        [InlineKeyboardButton("🧪 Тестова оплата", callback_data="testpay")],
    ])

    text = (
        "Привіт! 👋\n\n"
        "Це бот доступу до курсу самомасажу.\n"
        "Натисни кнопку нижче, щоб оплатити.\n\n"
        "Після оплати ти автоматично отримаєш доступ у приватний канал."
    )

    await update.message.reply_text(text, reply_markup=keyboard)


# ---------- REAL WORKING: ADD USER TO CHANNEL ----------
async def add_user_to_channel(user_id: int):
    """Workaround: direct Telegram Bot API request (PTB21 removed addChatMember)"""
    async with aiohttp.ClientSession() as session:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/inviteChatMember"
        payload = {
            "chat_id": CHANNEL_ID,
            "user_id": user_id
        }
        async with session.post(url, json=payload) as resp:
            result = await resp.json()
            return result


# ---------- TEST PAYMENT ----------
async def testpay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    result = await add_user_to_channel(user_id)

    if not result.get("ok"):
        await query.message.reply_text(
            f"Помилка при додаванні в канал:\n`{result}`",
            parse_mode="Markdown"
        )
        return

    await telegram_app.bot.send_message(
        chat_id=user_id,
        text="🧪 Тестова оплата успішна!\nТебе додано у приватний канал 🎉"
    )

    await query.message.reply_text("Готово! Ти в каналі 🎉")


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

    invoice_url = data.get("invoiceUrl")
    if not invoice_url:
        await query.message.reply_text("Не вдалося створити інвойс. Спробуйте ще раз.")
        return

    await query.message.reply_text(
        f"Перейдіть за посиланням, щоб оплатити:\n{invoice_url}"
    )


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
        return {"code": "error", "message": "Invalid signature"}

    order_ref = body.get("orderReference")
    status = body.get("transactionStatus")

    match = re.match(r"order_(\d+)", order_ref)
    if not match:
        return {"code": "error"}

    user_id = int(match.group(1))

    if status == "Approved":
        result = await add_user_to_channel(user_id)
        if result.get("ok"):
            await telegram_app.bot.send_message(
                chat_id=user_id,
                text="Оплата успішна! 🎉\nТебе додано у приватний канал."
            )

    timestamp = int(time.time())
    sig = wfp_response_signature(order_ref, "accept", timestamp)

    return {
        "orderReference": order_ref,
        "status": "accept",
        "time": timestamp,
        "signature": sig
    }


@app.get("/")
async def root():
    return {"status": "running"}
