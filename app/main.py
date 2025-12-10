import hashlib
import json
import aiohttp
from fastapi import FastAPI

# -----------------------------
# CONFIG
# -----------------------------
MERCHANT_ACCOUNT = "freelance_user_68fcc913e7b6e"
MERCHANT_PASSWORD = "e73cdf0eab18148a76c5f02eb96454c3"
MERCHANT_DOMAIN = "www.massagesobi.com"

# -----------------------------
# CREATE FASTAPI APP
# -----------------------------
app = FastAPI()

@app.get("/")
def root():
    return {"status": "running"}

# -----------------------------
# WAYFORPAY TEST INVOICE
# -----------------------------

async def create_invoice():
    url = "https://api.wayforpay.com/api"

    data = {
        "transactionType": "CREATE_INVOICE",
        "merchantAccount": MERCHANT_ACCOUNT,
        "merchantDomainName": MERCHANT_DOMAIN,
        "apiVersion": 1,
        "orderReference": "test_123456",
        "orderDate": 1702200000,
        "amount": 290,
        "currency": "UAH",
        "productName": ["Massage Course"],
        "productPrice": [290],
        "productCount": [1]
    }

    signature_string = ";".join([
        data["merchantAccount"],
        data["merchantDomainName"],
        str(data["orderReference"]),
        str(data["orderDate"]),
        str(data["amount"]),
        data["currency"],
        data["productName"][0],
        str(data["productCount"][0]),
        str(data["productPrice"][0]),
    ])

    data["merchantSignature"] = hashlib.md5(
        (signature_string + MERCHANT_PASSWORD).encode()
    ).hexdigest()

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=data) as resp:
            raw = await resp.text()
            print("WAYFORPAY RAW RESPONSE:", raw)   # ⬅ ВАЖЛИВО
            try:
                return json.loads(raw)
            except:
                return {"error": "NOT_JSON", "raw": raw}

# -----------------------------
# API ENDPOINT FOR TEST RUN
# -----------------------------
@app.get("/test-wfp")
async def test_wayforpay():
    response = await create_invoice()
    return response
