# close_ordi.py — ORDI 잔여 포지션 청산 전용 1회용 스크립트
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
        # 먼저 ORDI 관련 미체결 주문 취소
        try:
            orders = await ex.fetch_open_orders("ORDI/USDT:USDT")
            for o in orders:
                await ex.cancel_order(o["id"], "ORDI/USDT:USDT")
                print(f"미체결 취소: {o['id']}")
        except Exception as e:
            print(f"미체결 조회/취소: {e}")

        await asyncio.sleep(1)

        positions = await ex.fetch_positions(["ORDI/USDT:USDT"])
        for p in positions:
            amt = float(p["info"]["positionAmt"])
            if abs(amt) == 0:
                print("ORDI 포지션 없음!")
                return

            mark = float(p.get("markPrice", 0) or p["info"].get("markPrice", 0))
            close_side = "buy" if amt < 0 else "sell"
            close_qty = abs(amt)

            print(f"ORDI amt={amt} mark={mark} side={close_side}")

            # 가격 제한 내에서 지정가 시도 (markPrice 근처)
            prices_to_try = [
                round(mark, 4),
                round(mark * 1.001, 4),
                round(mark * 0.999, 4),
                3.56,
                3.50,
                3.40,
                3.30,
            ]

            for price in prices_to_try:
                try:
                    print(f"  시도: {close_side} {close_qty} @ {price}")
                    order = await ex.create_order(
                        "ORDI/USDT:USDT", "limit", close_side, close_qty, price,
                        {"reduceOnly": True, "timeInForce": "GTC"}
                    )
                    oid = order["id"]
                    print(f"  주문 접수! id={oid}")

                    # 10초 체결 대기
                    for i in range(10):
                        await asyncio.sleep(1)
                        fetched = await ex.fetch_order(oid, "ORDI/USDT:USDT")
                        if fetched["status"] == "closed":
                            print(f"  체결 완료! ({i+1}초)")
                            return
                    # 미체결 시 취소 후 다음 가격 시도
                    await ex.cancel_order(oid, "ORDI/USDT:USDT")
                    print(f"  미체결 → 취소, 다음 가격 시도")
                except Exception as e:
                    print(f"  실패: {e}")

            print("모든 가격 시도 실패 — 바이낸스 웹에서 수동 청산 필요")
    finally:
        await ex.close()

asyncio.run(main())
