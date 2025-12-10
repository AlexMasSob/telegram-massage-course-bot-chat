import os
import time
import hmac
import hashlib
import aiohttp
import asyncio

from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ======== ENV ========
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN")

CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
MERCHANT_LOGIN = os.getenv("MERCHANT_LOGIN")  # freelance_user_...
MERCHANT_PASSWORD = os.getenv("MERCHANT_PASSWORD")  # 32 chars password
MERCHANT_DOMAIN = os.getenv("MERCHANT_DOMAIN", "www.massagesobi.com")

PRODUCT_NAME = os.getenv("PRODUCT_NAME", "Massage Course")
AMOUNT = float(os.getenv("AMOUNT", "290.00"))
CURRENCY = "UAH"

SERVICE_URL = os.getenv("SERVICE_URL")  # https://yourapp.onrender.com/wayforpay/callback
KEEP_ALIVE_URL = os.getenv("KEEP_ALIVE_URL")

ADMIN_ID = int(os.getenv("ADMIN_ID"))

if not all([BOT_TOKEN, WEBHOOK_TOKEN, CHANNEL_ID, MERCHANT_LOGIN, MERCHANT_PASSWORD, SERVICE_URL]):
    raise RuntimeError("Missing ENV variables")

app = FastAPI()

telegram_app = Application.builder().token(BOT_TOKEN).build()


# ======================================================================
#                     WAYFORPAY SIGNATURE HELPERS
# ======================================================================

def wfp_invoice_signature(payload: dict) -> str:
    """
    Signature format from WayForPay docs:
    merchantAccount;merchantDomainName;orderReference;orderDate;amount;currency;productName;productCount;productPrice
    """
    parts = [
        payload["merchantAccount"],
        payload["merchantDomainName"],
        payload["orderReference"],
        str(payload["orderDate"]),
        str(payload["amount"]),
        payload["currency"]
    ]

    parts += payload["productName"]
    parts += [str(x) for x in payload["productCount"]]
    parts += [str(x) for x in payload["productPrice"]]

    msg = ";".join(parts)
    return hmac.new(MERCHANT_PASSWORD.encode(), msg.encode(), hashlib.md5).hexdigest()


def wfp_callback_valid(body: dict) -> bool:
    """
    WayForPay callback signature:
    merchantAccount;orderReference;amount;currency;authCode;cardPan;transactionStatus;reasonCode
    """
    required = [
        "merchantAccount", "orderReference", "amount", "currency",
        "authCode", "cardPan", "transactionStatus", "reasonCode", "merchantSignature"
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
    expected = hmac.new(MERCHANT_PASSWORD.encode(), msg.encode(), hashlib.md5).hexdigest()
    return expected == body["merchantSignature"]


def wfp_response_signature(order_ref: str, status: str, ts: int) -> str:
    msg = f"{order_ref};{status};{ts}"
    return hmac.new(MERCHANT_PASSWORD.encode(), msg.encode(), hashlib.md5).hexdigest()


# ======================================================================
#                              TELEGRAM BOT
# ======================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Оплатити курс", callback_data="pay")]
    ])

    await update.message.reply_text(
        "Вітаю! 👋\nНатисніть кнопку, щоб оплатити курс.",
        reply_markup=keyboard
    )


telegram_app.add_handler(CommandHandler("start", start))


async def pay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Creates invoice via API and sends user a payment link."""
    query = update.callback_query
    await query.answer()

    user = query.from_user
    order_ref = f"order_{user.id}_{int(time.time())}"
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
        "serviceUrl": SERVICE_URL
    }

    payload["merchantSignature"] = wfp_invoice_signature(payload)

    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.wayforpay.com/api", json=payload) as resp:
            data = await resp.json()

    print("WFP RESPONSE:", data)

    if data.get("reasonCode") not in (1100, 1101, 1102):
        await query.message.reply_text("❌ Помилка при створенні інвойсу.")
        return

    invoice_url = data.get("invoiceUrl")
    await query.message.reply_text(
        f"Для оплати перейдіть за посиланням:\n{invoice_url}"
    )


telegram_app.add_handler(CallbackQueryHandler(pay_handler, pattern="^pay$"))


# ======================================================================
#                      WAYFORPAY CALLBACK ENDPOINT
# ======================================================================

@app.post("/wayforpay/callback")
async def wfp_callback(request: Request):
    body = await request.json()
    print("WFP CALLBACK:", body)

    if not wfp_callback_valid(body):
        return {"code": "INVALID_SIGNATURE"}

    order_ref = body["orderReference"]
    status = body.get("transactionStatus")

    # Extract Telegram ID from orderRef
    try:
        _, tg_id, _ = order_ref.split("_")
        tg_id = int(tg_id)
    except:
        return {"code": "BAD_ORDER_REF"}

    if status == "Approved":
        # create one-time link
        invite = await telegram_app.bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
        await telegram_app.bot.send_message(
            tg_id,
            f"🎉 Оплата успішна!\nОсь ваш доступ до каналу:\n{invite.invite_link}"
        )

    ts = int(time.time())
    signature = wfp_response_signature(order_ref, "accept", ts)

    return {"orderReference": order_ref, "status": "accept", "time": ts, "signature": signature}


# ======================================================================
#                        TELEGRAM WEBHOOK ENDPOINT
# ======================================================================

@app.post("/telegram/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403)

    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)

    return {"ok": True}


# ======================================================================
#                             STARTUP
# ======================================================================

@app.on_event("startup")
async def startup_event():
    await telegram_app.initialize()
    await telegram_app.start()


@app.get("/")
async def root():
    return {"status": "running"}
