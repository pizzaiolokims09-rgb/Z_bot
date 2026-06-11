import ccxt.async_support as ccxt, asyncio
async def test():
  e = ccxt.binanceusdm({'options':{'defaultType':'future'}})
  await e.load_markets()
  print('BTC', e.markets['BTC/USDT:USDT']['info'].get('underlyingType'))
  print('XAU', e.markets.get('XAU/USDT:USDT',{}).get('info',{}).get('underlyingType'))
  print('MRVL', e.markets.get('MRVL/USDT:USDT',{}).get('info',{}).get('underlyingType'))
  await e.close()
asyncio.run(test())
