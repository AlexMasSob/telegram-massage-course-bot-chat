import os
import hashlib
import base64
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode

# --- ENV VARIABLES ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN")

CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

WAYFORPAY_MERCHANT = os.getenv("WAYFORPAY_MERCHANT")
WAYFORPAY_SECRET = os.getenv("WAYFORPAY_SECRET")
MERCHANT_DOMAIN = "www.massagesobi.com"  # <--- ВАЖЛИВО!

PRODUCT_NAME = "Massage Course"
PRODUCT_PRICE = "290"
CURRENCY = "UAH"

SERVICE_URL = os.getenv("SERVICE_URL")  # https://telegram-massage-course-bot-chat.onrender.com/wayforpay/callback

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()


# -------------------------------
#  SIGNATURE BUILDER (WAYFORPAY)
# -------------------------------
def build_signature(order_reference, order_date):
    elements = [
        WAYFORPAY_MERCHANT,
        MERCHANT_DOMAIN,
        order_reference,
        str(order_date),
        PRODUCT_PRICE,
        CURRENCY,
        PRODUCT_NAME,
        "1",
        PRODUCT_PRICE,
    ]
    string_to_sign = ";".join(elements)
    sha1_hash = hashlib.sha1((string_to_sign + WAYFORPAY_SECRET).encode("utf-8")).digest()
    signature = base64.b64encode(sha1_hash).decode("utf-8")
    return signature


# -------------------------------
#  CREATE INVOICE (BUTTON PRESS)
# -------------------------------
async def create_invoice(user_id: int):
    order_reference = f"order_{user_id}_{int(datetime.now().timestamp())}"
    order_date = int(datetime.now().timestamp())

    signature = build_signature(order_reference, order_date)

    payload = {
        "transactionType": "CREATE_INVOICE",
        "merchantAccount": WAYFORPAY_MERCHANT,
        "merchantDomainName": MERCHANT_DOMAIN,
        "orderReference": order_reference,
        "orderDate": order_date,
        "amount": PRODUCT_PRICE,
        "currency": CURRENCY,
        "productName": [PRODUCT_NAME],
        "productPrice": [PRODUCT_PRICE],
        "productCount": ["1"],
        "language": "UA",
        "serviceUrl": SERVICE_URL,
        "merchantSignature": signature,
    }

    print("Sending WayForPay payload:", payload)

    import requests
    r = requests.post("https://api.wayforpay.com/api", json=payload)
    print("WayForPay response:", r.text)

    try:
        data = r.json()
    except:
        return None, "Invalid response from WayForPay"

    if "invoiceUrl" in data:
        return data["invoiceUrl"], None
    else:
        return None, data.get("reason", "Error")


# -------------------------------
#   TELEGRAM BOT HANDLERS
# -------------------------------
@dp.message(CommandStart())
async def start(message: types.Message):
    kb = [
        [types.KeyboardButton(text="💳 Оплатити курс")],
        [types.KeyboardButton(text="🧪 Тестова оплата")]
    ]
    markup = types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

    await message.answer(
        "Вітаю! 👋\n\n"
        "Це бот доступу до курсу самомасажу.\n"
        "Натисніть кнопку нижче, щоб отримати доступ.\n\n"
        "Після оплати Ви автоматично отримаєте доступ у приватний канал.",
        reply_markup=markup
    )


@dp.message()
async def handle_payment(message: types.Message):
    if message.text in ["💳 Оплатити курс", "🧪 Тестова оплата"]:
        url, err = await create_invoice(message.from_user.id)

        if url:
            await message.answer(f"Ваш рахунок готовий! Перейдіть за посиланням👇\n\n{url}")
        else:
            await message.answer(f"❌ Помилка при створенні інвойсу.\nПричина: {err}")
    else:
        await message.answer("Натисніть кнопку для оплати 👇")


# -------------------------------
#   WAYFORPAY CALLBACK
# -------------------------------
@app.post("/wayforpay/callback")
async def wayforpay_callback(request: Request):
    data = await request.json()
    print("WayForPay callback:", data)

    if data.get("transactionStatus") == "Approved":
        user_id = int(data["orderReference"].split("_")[1])

        await bot.send_message(
            chat_id=user_id,
            text="🎉 Дякуємо за оплату!\nВи отримали доступ до приватного каналу ❤️"
        )

        await bot.send_message(
            chat_id=user_id,
            text=f"Ваш канал: https://t.me/+{CHANNEL_ID}"
        )

    return JSONResponse({"status": "ok"})


# -------------------------------
#   STARTUP
# -------------------------------
@app.on_event("startup")
async def on_startup():
    print("Bot started")


# -------------------------------
#   WEBHOOK ENDPOINT
# -------------------------------
@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    update = await req.json()
    await dp.feed_update(bot, types.Update(**update))
    return {"ok": True}
