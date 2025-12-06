import os
import hmac
import hashlib
import json
import time
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.session.aiohttp import AiohttpSession

WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

WAYFORPAY_MERCHANT = os.getenv("WAYFORPAY_MERCHANT")
WAYFORPAY_SECRET = os.getenv("WAYFORPAY_SECRET")
MERCHANT_DOMAIN = os.getenv("MERCHANT_DOMAIN", "massagesobi.com")

PRODUCT_NAME = os.getenv("PRODUCT_NAME", "Massage Course")
AMOUNT = float(os.getenv("AMOUNT", "290.00"))
CURRENCY = os.getenv("CURRENCY", "UAH")

SERVICE_URL = os.getenv("SERVICE_URL")  # https://telegram-massage-course-bot-chat.onrender.com/wayforpay/callback

bot = Bot(token=TELEGRAM_TOKEN, session=AiohttpSession())
dp = Dispatcher()
app = FastAPI()


# ============================================================
# 🔥 ФУНКЦІЯ ГЕНЕРАЦІЇ ПІДПИСУ — З ЛОГУВАННЯМ ДЛЯ ДІАГНОСТИКИ
# ============================================================
def create_signature(data: dict, secret: str) -> str:
    fields = [
        data.get("merchantAccount", ""),
        data.get("merchantDomainName", ""),
        data.get("orderReference", ""),
        str(data.get("orderDate", "")),
    ]

    product_names = data.get("productName", [])
    product_counts = data.get("productCount", [])
    product_prices = data.get("productPrice", [])

    fields.extend(product_names)
    fields.extend([str(x) for x in product_counts])
    fields.extend([str(x) for x in product_prices])

    signature_string = ";".join(fields)

    signature = hmac.new(
        secret.encode("utf-8"),
        signature_string.encode("utf-8"),
        hashlib.md5
    ).hexdigest()

    print("\n===== WAYFORPAY SIGNATURE DEBUG =====")
    print("Signature string:")
    print(signature_string)
    print("\nGenerated signature:")
    print(signature)
    print("====================================\n")

    return signature


# ============================================================
# 🔥 TELEGRAM — START
# ============================================================
@dp.message(CommandStart())
async def start(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Оплатити курс", callback_data="pay")
    kb.button(text="🧪 Тестова оплата", callback_data="pay_test")
    kb.adjust(1)

    await message.answer(
        "Вітаю! 👋\n\n"
        "Це бот доступу до курсу самомасажу.\n"
        "Натисніть кнопку нижче, щоб отримати доступ.\n\n"
        "Після оплати Ви автоматично отримаєте доступ у приватний канал з відеоуроками ❤️",
        reply_markup=kb.as_markup()
    )


# ============================================================
# 🔥 TELEGRAM — CALLBACK "PAY"
# ============================================================
@dp.callback_query(lambda c: c.data.startswith("pay"))
async def process_payment(call: types.CallbackQuery):
    is_test = call.data == "pay_test"

    order_ref = f"order_{int(time.time())}"
    timestamp = int(time.time())

    payload = {
        "transactionType": "CREATE_INVOICE",
        "merchantAccount": WAYFORPAY_MERCHANT,
        "merchantDomainName": MERCHANT_DOMAIN,
        "orderReference": order_ref,
        "orderDate": timestamp,
        "amount": AMOUNT,
        "currency": CURRENCY,
        "productName": [PRODUCT_NAME],
        "productCount": [1],
        "productPrice": [AMOUNT],
        "language": "UA",
        "apiVersion": 1,
        "serviceUrl": SERVICE_URL,
    }

    payload["merchantSignature"] = create_signature(payload, WAYFORPAY_SECRET)

    # 🔥 ЛОГУЄМО PAYLOAD ПЕРЕД ВІДПРАВКОЮ
    print("\n===== WAYFORPAY PAYLOAD TO SEND =====")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print("====================================\n")

    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.wayforpay.com/api",
            json=payload
        ) as response:
            resp_text = await response.text()
            print("===== WAYFORPAY RAW RESPONSE =====")
            print(resp_text)
            print("=================================\n")

            try:
                data = json.loads(resp_text)
            except:
                await call.message.answer("❌ Помилка WayForPay: неправильна відповідь")
                return

            if data.get("reasonCode") == 1100:
                invoice_url = data.get("invoiceUrl")
                await call.message.answer(f"Перейдіть для оплати:\n{invoice_url}")
            else:
                await call.message.answer(f"❌ Помилка при створенні інвойсу.\n"
                                          f"Код: {data.get('reasonCode')}\n"
                                          f"Причина: {data.get('reason')}")


# ============================================================
# 🔥 CALLBACK ДЛЯ WayForPay
# ============================================================
@app.post("/wayforpay/callback")
async def wayforpay_callback(request: Request):
    data = await request.json()
    print("\n===== WAYFORPAY CALLBACK RECEIVED =====")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    print("=======================================\n")

    if data.get("transactionStatus") == "Approved":
        user_id = data.get("clientAccount", None)
        if user_id:
            await bot.send_message(user_id, "🎉 Ваш платіж успішний! Доступ надано.")

    return JSONResponse({"status": "success"})


# ============================================================
# 🔥 WEBHOOK
# ============================================================
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update_data = await request.json()
    await dp.feed_webhook_update(bot, update_data)
    return JSONResponse({"ok": True})
