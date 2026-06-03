import ccxt, asyncio, os
from dotenv import load_dotenv

load_dotenv()

async def test():
    ex = ccxt.binance({
        'apiKey': os.getenv('BINANCE_API_KEY'),
        'secret': os.getenv('BINANCE_API_SECRET'),
        'options': {'defaultType': 'future'}
    })
    
    positions = await ex.fetch_positions(['RENDER/USDT:USDT', 'FET/USDT:USDT'])
    for p in positions:
        print("Symbol:", p.get('symbol'))
        print("Contracts:", p.get('contracts'))
        print("uPnL:", p.get('unrealizedPnl'))
    
    await ex.close()

asyncio.run(test())
