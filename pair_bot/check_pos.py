import asyncio, os
from dotenv import load_dotenv
import ccxt.async_support as ccxt_async

async def main():
    load_dotenv()
    ex = ccxt_async.binance({
        "apiKey": os.getenv("BINANCE_API_KEY"),
        "secret": os.getenv("BINANCE_API_SECRET"),
        "options": {"defaultType": "future", "adjustForTimeDifference": True},
        "enableRateLimit": True,
    })
    ex.enable_demo_trading(True)
    try:
        ps = await ex.fetch_positions()
        op = [p for p in ps if abs(float(p["info"]["positionAmt"])) > 0]
        if not op:
            print("ALL CLEAR - 열린 포지션 0개")
        else:
            print(f"{len(op)}개 포지션 잔존:")
            for p in op:
                sym = p["symbol"]
                amt = p["info"]["positionAmt"]
                print(f"  {sym}: {amt}")
    finally:
        await ex.close()

asyncio.run(main())
