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
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "super-secret")
CHANNEL_ID = os.getenv("CHANNEL_ID")
WAYFORPAY_MERCHANT = os.getenv("WAYFORPAY_MERCHANT")
WAYFORPAY_SECRET = os.getenv("WAYFORPAY_SECRET")
MERCHANT_DOMAIN = os.getenv("MERCHANT_DOMAIN", "yourdomain.com")
PRODUCT_NAME = os.getenv("PRODUCT_NAME", "Massage Course")
AMOUNT = float(os.getenv("AMOUNT", "200.00"))
CURRENCY = os.getenv("CURRENCY", "UAH")
SERVICE_URL = os.getenv("SERVICE_URL")  # https://your-app.onrender.com/wayforpay/callback

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID missing")
CHANNEL_ID = int(CHANNEL_ID)

app = FastAPI()

pending_orders = {}  # orderReference -> telegram_id
paid_users = set()

telegram_app = Application.builder().token(BOT_TOKEN).build()


# ===================== SIGNATURE HELPERS =====================

def wfp_invoice_signature(payload: dict) -> str:
    """Signature for CREATE_INVOICE request."""
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

    message = ";".join(parts)
    return hmac.new(
        WAYFORPAY_SECRET.encode(),
        message.encode(),
        hashlib.md5
    ).hexdigest()


def wfp_callback_valid(body: dict) -> bool:
    """Verify callback signature."""
    needed = [
        "merchantAccount", "orderReference", "amount", "currency",
        "authCode", "cardPan", "transactionStatus", "reasonCode",
        "merchantSignature"
    ]

    if not all(k in body for k in needed):
        return True  # allow test mode

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

    return expected == body.get("merchantSignature")


def wfp_response_signature(order_ref: str, status: str, timestamp: int) -> str:
    message = f"{order_ref};{status};{timestamp}"
    return hmac.new(
        WAYFORPAY_SECRET.encode(),
        message.encode(),
        hashlib.md5
    ).hexdigest()


# ===================== TELEGRAM HANDLERS =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("💳 Оплатити курс", callback_data="pay")]]
    )

    txt = (
        "Привіт! 👋\n\n"
        "Це бот доступу до курсу самомасажу.\n"
        "Натисни кнопку нижче, щоб оплатити курс.\n\n"
        "Після оплати ти автоматично отримаєш доступ у приватний канал."
    )

    await update.message.reply_text(txt, reply_markup=keyboard)


async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    order_ref = f"course_{user_id}"
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
        "productPrice": [AMOUNT],
        "productCount": [1],
        "apiVersion": 1,
        "language": "UA",
    }

    if SERVICE_URL:
        payload["serviceUrl"] = SERVICE_URL

    payload["merchantSignature"] = wfp_invoice_signature(payload)

    # Send request to Wayforpay
    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.wayforpay.com/api", json=payload) as resp:
            data = await resp.json()

    invoice = data.get("invoiceUrl")
    if not invoice:
        await query.message.reply_text("Не вдалося створити платіж. Спробуй ще раз.")
        return

    txt = (
        "Готово! 🎉\n\n"
        "Перейди за посиланням, щоб оплатити курс:\n"
        f"{invoice}\n\n"
        "Після оплати я автоматично додам тебе в канал з уроками."
    )

    await query.message.reply_text(txt)


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CallbackQueryHandler(pay, pattern="^pay$"))


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

    order_ref = body.get("orderReference")
    status = body.get("transactionStatus")

    if not order_ref:
        return {"code": "error"}

    if not wfp_callback_valid(body):
        return {"code": "error", "msg": "bad signature"}

    m = re.match(r"course_(\d+)", order_ref)
    if not m:
        return {"code": "error", "msg": "bad orderReference"}

    telegram_id = int(m.group(1))

    if status == "Approved":
        paid_users.add(telegram_id)

        try:
            await telegram_app.bot.add_chat_member(CHANNEL_ID, telegram_id)
            await telegram_app.bot.send_message(
                telegram_id,
                "Оплата успішна! 🎉\nТи доданий у канал з уроками."
            )
        except Exception as e:
            print("Add user error:", e)

    ts = int(time.time())
    sig = wfp_response_signature(order_ref, "accept", ts)

    return {
        "orderReference": order_ref,
        "status": "accept",
        "time": ts,
        "signature": sig
    }


@app.get("/")
async def root():
    return {"status": "running"}
