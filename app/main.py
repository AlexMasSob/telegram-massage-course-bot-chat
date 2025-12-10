import os
import time
import hmac
import hashlib
import aiohttp
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# --------------------------
# ENV CONFIG
# --------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN")

MERCHANT_ACCOUNT = os.getenv("MERCHANT_LOGIN")
MERCHANT_PASSWORD = os.getenv("MERCHANT_PASSWORD")  # 32 символи
MERCHANT_DOMAIN = "massagesobi.com"

PRODUCT_NAME = "Massage Course"
AMOUNT = 290
CURRENCY = "UAH"

CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
SERVICE_URL = os.getenv("SERVICE_URL")  # https://yourdomain.com/wayforpay/callback

# --------------------------
# TELEGRAM APP
# --------------------------
telegram_app = Application.builder().token(BOT_TOKEN).build()

app = FastAPI()


# --------------------------
# SIGNATURE FOR CREATE_INVOICE
# --------------------------
def generate_invoice_signature(data):
    parts = [
        data["merchantAccount"],
        data["merchantDomainName"],
        data["orderReference"],
        str(data["orderDate"]),
        str(data["amount"]),
        data["currency"],
    ]

    for p in data["productName"]:
        parts.append(p)

    for p in data["productCount"]:
        parts.append(str(p))

    for p in data["productPrice"]:
        parts.append(str(p))

    sign_string = ";".join(parts)

    return hmac.new(
        MERCHANT_PASSWORD.encode(),
        sign_string.encode(),
        hashlib.md5,
    ).hexdigest()


# --------------------------
# SIGNATURE CHECK CALLBACK
# --------------------------
def verify_callback_signature(body):
    try:
        parts = [
            body["merchantAccount"],
            body["orderReference"],
            str(body["amount"]),
            body["currency"],
            body["authCode"],
            body["cardPan"],
            body["transactionStatus"],
            str(body["reasonCode"]),
        ]

        sign_str = ";".join(parts)

        expected = hmac.new(
            MERCHANT_PASSWORD.encode(),
            sign_str.encode(),
            hashlib.md5
        ).hexdigest()

        return expected == body["merchantSignature"]

    except Exception:
        return False


# --------------------------
# /start
# --------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Оплатити курс", callback_data="pay")]
    ])

    await update.message.reply_text(
        "Вітаю! Щоб отримати доступ до курсу – оплатіть натиснувши кнопку нижче:",
        reply_markup=keyboard
    )


telegram_app.add_handler(CommandHandler("start", start))


# --------------------------
# PAY BUTTON PRESSED
# --------------------------
async def pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    order_reference = f"order_{user.id}_{int(time.time())}"

    payload = {
        "transactionType": "CREATE_INVOICE",
        "merchantAccount": MERCHANT_ACCOUNT,
        "merchantDomainName": MERCHANT_DOMAIN,
        "orderReference": order_reference,
        "orderDate": int(time.time()),
        "amount": AMOUNT,
        "currency": CURRENCY,
        "productName": [PRODUCT_NAME],
        "productCount": [1],
        "productPrice": [AMOUNT],
        "language": "UA",
        "apiVersion": 1,
        "serviceUrl": SERVICE_URL,
    }

    payload["merchantSignature"] = generate_invoice_signature(payload)

    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.wayforpay.com/api", json=payload) as resp:
            data = await resp.json()

    if data.get("reasonCode") != 1100:
        await query.message.reply_text("❌ Помилка створення інвойсу.")
        return

    invoice_url = data.get("invoiceUrl")

    await query.message.reply_text(
        f"Оплатіть, будь ласка, за посиланням:\n{invoice_url}\n\n"
        "Після успішної оплати Ви автоматично отримаєте доступ ❤️"
    )


telegram_app.add_handler(CallbackQueryHandler(pay_callback, pattern="^pay$"))


# --------------------------
# CALLBACK FROM WAYFORPAY
# --------------------------
@app.post("/wayforpay/callback")
async def wayforpay_callback(request: Request):
    body = await request.json()

    print("WAYFORPAY CALLBACK:", body)

    if not verify_callback_signature(body):
        print("❌ Invalid signature")
        return {"code": "error", "message": "invalid signature"}

    status = body.get("transactionStatus")
    order_ref = body.get("orderReference")

    # orderReference = order_userId_timestamp
    parts = order_ref.split("_")
    user_id = int(parts[1])

    if status == "Approved":
        # видаємо доступ у канал
        invite_link = await telegram_app.bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1
        )

        await telegram_app.bot.send_message(
            chat_id=user_id,
            text=(
                "🎉 Оплата отримана!\n\n"
                "Ваш доступ до курсу:\n"
                f"{invite_link.invite_link}"
            )
        )

    # WayForPay expects response
    return {
        "orderReference": order_ref,
        "status": "accept",
        "time": int(time.time())
    }


# --------------------------
# TELEGRAM WEBHOOK
# --------------------------
@app.post("/telegram/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        return {"error": "forbidden"}

    update = Update.de_json(await request.json(), telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}


# --------------------------
# Root
# --------------------------
@app.get("/")
async def root():
    return {"status": "running"}
