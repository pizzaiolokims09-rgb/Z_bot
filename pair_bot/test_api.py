import os
from dotenv import load_dotenv
import ccxt
import logging

logging.basicConfig(level=logging.INFO)
load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "options": {"defaultType": "spot"},
    "enableRateLimit": True,
})

exchange.set_sandbox_mode(True)
try:
    balance = exchange.fetch_balance()
    print("Spot Testnet Balance fetched successfully!")
except Exception as e:
    print("Spot Testnet Fetch balance failed:", e)
