import asyncio
import os
from dotenv import load_dotenv
import ccxt.async_support as ccxt_async

async def main():
    load_dotenv()
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    
    # 봇과 동일한 설정으로 바이낸스 선물 실거래 접속
    exchange = ccxt_async.binance({
        "apiKey": api_key,
        "secret": api_secret,
        "options": {"defaultType": "future", "adjustForTimeDifference": True},
        "enableRateLimit": True,
    })
    
    symbols_to_close = ['BCH/USDT', 'LTC/USDT', 'HBAR/USDT', 'XRP/USDT']
    print("--- 낡은 포지션 조회 중 ---")
    
    try:
        positions = await exchange.fetch_positions(symbols_to_close)
        
        for pos in positions:
            sym = pos['symbol']
            amt = float(pos['info']['positionAmt'])
            
            if amt == 0:
                continue
                
            close_qty = 0
            close_side = ""
            
            # 100% 청산 대상
            if sym in ['BCH/USDT', 'LTC/USDT', 'HBAR/USDT']:
                close_qty = abs(amt)
                close_side = "buy" if amt < 0 else "sell"
                
            # 부분 청산 대상 (XRP)
            elif sym == 'XRP/USDT':
                # 전체 수량(1877.6) 중 예전 진입분(1087)만 시장가 매도
                if amt > 1087:
                    close_qty = 1087
                    close_side = "sell"
                else:
                    print(f"[스킵] {sym} 보유량이 1087보다 적습니다 (현재: {amt})")
                    continue
                    
            if close_qty > 0:
                print(f"청산 주문 전송: {sym} | 수량: {close_qty} | 방향: {close_side.upper()}")
                try:
                    order = await exchange.create_order(
                        sym, "market", close_side, close_qty, {"reduceOnly": True}
                    )
                    print(f"✅ {sym} 청산 완료! (주문 ID: {order['id']})")
                except Exception as e:
                    print(f"❌ {sym} 청산 실패: {e}")
                    
    except Exception as e:
        print(f"오류 발생: {e}")
    finally:
        await exchange.close()

if __name__ == "__main__":
    asyncio.run(main())
