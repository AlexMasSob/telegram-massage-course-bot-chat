import os
import threading
import hashlib
import hmac
import time
import requests
from dotenv import load_dotenv

from fastapi import FastAPI, Request
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler

load_dotenv()

# === TELEGRAM ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
bot = Bot(token=BOT_TOKEN)

# === WAYFORPAY ===
WAYFORPAY_MERCHANT = os.getenv("WAYFORPAY_MERCHANT")
WAYFORPAY_SECRET = os.getenv("WAYFORPAY_SECRET")
MERCHANT_DOMAIN = "www.massagesobi.com"

PRODUCT_NAME = os.getenv("PRODUCT_NAME", "Курс самомасажу")
AMOUNT = float(os.getenv("AMOUNT", "290.00"))
CURRENCY = "UAH"

SERVICE_URL = os.getenv("SERVICE_URL")

# === FASTAPI APP ===
app = FastAPI()


# -------- SIGNATURE GENERATION --------

def generate_signature(data: dict) -> str:
    parts = [
        data["merchantAccount"],
        data["merchantDomainName"],
        data["orderReference"],
        str(data["orderDate"]),
        str(data["amount"]),
        data["currency"],
        data["productName"][0],
        str(data["productCount"][0]),
        str(data["productPrice"][0]),
    ]

    string_to_sign = ";".join(parts)

    signature = hmac.new(
        WAYFORPAY_SECRET.encode(),
        string_to_sign.encode(),
        hashlib.md5
    ).hexdigest()

    return signature


def create_invoice(user_id):
    order_ref = f"order_{user_id}_{int(time.time())}"

    payload = {
        "transactionType": "CREATE_INVOICE",
        "merchantAccount": WAYFORPAY_MERCHANT,
        "merchantDomainName": MERCHANT_DOMAIN,
        "orderReference": order_ref,
        "orderDate": int(time.time()),
        "amount": AMOUNT,
        "currency": CURRENCY,
        "productName": [PRODUCT_NAME],
        "productCount": [1],
        "productPrice": [AMOUNT],
        "serviceUrl": SERVICE_URL,
        "apiVersion": 1,
    }

    payload["merchantSignature"] = generate_signature(payload)

    res = requests.post("https://api.wayforpay.com/api", json=payload)
    return res.json()


# -------- TELEGRAM HANDLERS --------

async def start(update, context):
    keyboard = [
        [InlineKeyboardButton("💳 Оплатити курс", callback_data="pay")],
        [InlineKeyboardButton("🧪 Тестова оплата", callback_data="testpay")]
    ]
    reply = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Вітаю! 👋\n"
        "Це бот доступу до курсу самомасажу.\n"
        "Натисніть кнопку нижче, щоб отримати доступ.",
        reply_markup=reply
    )


async def handle_buttons(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if query.data == "pay":
        invoice = create_invoice(user_id)

        if "invoiceUrl" in invoice:
            await bot.send_message(chat_id=user_id, text=invoice["invoiceUrl"])
        else:
            await bot.send_message(chat_id=user_id, text="Помилка при створенні інвойсу.")

    elif query.data == "testpay":
        await bot.send_message(chat_id=user_id, text="ТЕСТ: доступ надано!")
        await bot.approve_chat_join_request(CHANNEL_ID, user_id)


# -------- WAYFORPAY CALLBACK --------

@app.post("/wayforpay/callback")
async def wayforpay_callback(request: Request):
    data = await request.json()

    if data.get("transactionStatus") == "Approved":
        user_id = extract_user(data)
        if user_id:
            await bot.approve_chat_join_request(CHANNEL_ID, user_id)
            await bot.send_message(chat_id=user_id, text="🎉 Оплата підтверджена! Доступ надано.")

        return {"status": "success"}

    return {"status": "ignored"}


def extract_user(data):
    try:
        ref = data["orderReference"]
        return int(ref.split("_")[1])
    except:
        return None


# -------- TELEGRAM BOT RUN IN SEPARATE THREAD --------

def run_bot():
    app_tg = ApplicationBuilder().token(BOT_TOKEN).build()
    app_tg.add_handler(CommandHandler("start", start))
    app_tg.add_handler(CallbackQueryHandler(handle_buttons))
    app_tg.run_polling()


# запускаємо Telegram у фоновому потоці
threading.Thread(target=run_bot, daemon=True).start()
