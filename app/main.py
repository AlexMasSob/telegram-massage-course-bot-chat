import hashlib
import json
import aiohttp

MERCHANT_ACCOUNT = "freelance_user_68fcc913e7b6e"
MERCHANT_PASSWORD = "e73cdf0eab18148a76c5f02eb9640000"
MERCHANT_DOMAIN = "www.massagesobi.com"

async def create_invoice():
    url = "https://api.wayforpay.com/api"

    # УВАГА: тільки такі назви і порядок елементів!
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

    # Формування підпису (str і саме в такому порядку)
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
            text = await resp.text()
            print("RAW RESPONSE:", text)
            try:
                return json.loads(text)
            except:
                return {"error": "NOT_JSON", "raw": text}
