import os
import uuid

import requests
from dotenv import load_dotenv


load_dotenv()

FLW_SECRET_KEY = os.getenv("FLUTTERWAVE_SECRET_KEY", "").strip()
FLW_CURRENCY = os.getenv("FLUTTERWAVE_CURRENCY", "NGN").strip() or "NGN"

try:
    FLW_PREMIUM_AMOUNT = int(float(os.getenv("FLUTTERWAVE_PREMIUM_AMOUNT", "3000")))
except Exception:
    FLW_PREMIUM_AMOUNT = 10000

if not FLW_SECRET_KEY:
    print("ERROR: No FLUTTERWAVE_SECRET_KEY found in .env")
    raise SystemExit

headers = {
    "Authorization": f"Bearer {FLW_SECRET_KEY}",
    "Content-Type": "application/json",
}

print("Checking Flutterwave account...")

account_response = requests.get(
    "https://api.flutterwave.com/v3/account",
    headers=headers,
    timeout=30,
)

print("Account status code:", account_response.status_code)

try:
    print(account_response.json())
except Exception:
    print(account_response.text)

payload = {
    "tx_ref": f"test_{uuid.uuid4().hex}",
    "amount": FLW_PREMIUM_AMOUNT,
    "currency": FLW_CURRENCY,
    "redirect_url": "http://127.0.0.1:8000/app",
    "customer": {
        "email": "test@example.com",
    },
    "customizations": {
        "title": "Test Payment",
        "description": "Flutterwave connection test",
    },
}

print("\nInitializing test payment...")

payment_response = requests.post(
    "https://api.flutterwave.com/v3/payments",
    json=payload,
    headers=headers,
    timeout=30,
)

print("Payment status code:", payment_response.status_code)

try:
    print(payment_response.json())
except Exception:
    print(payment_response.text)