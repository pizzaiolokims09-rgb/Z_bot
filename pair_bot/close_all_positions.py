# =============================================================================
# close_all_positions.py
# 바이낸스 데모(테스트넷) 선물 계좌의 모든 열린 포지션을 지정가로 청산
# 시장가 주문이 안 되는 테스트넷 환경 대응
# =============================================================================

import asyncio
import json
import os
from dotenv import load_dotenv
import ccxt.async_support as ccxt_async


async def main():
    load_dotenv()
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")

    exchange = ccxt_async.binance({
        "apiKey": api_key,
        "secret": api_secret,
        "options": {"defaultType": "future", "adjustForTimeDifference": True},
        "enableRateLimit": True,
    })
    exchange.enable_demo_trading(True)

    try:
        # 1) 기존 미체결 주문 전부 취소
        print("--- 기존 미체결 주문 취소 중 ---")
        try:
            open_orders = await exchange.fetch_open_orders()
            for o in open_orders:
                try:
                    await exchange.cancel_order(o["id"], o["symbol"])
                    print(f"  취소: {o['symbol']} id={o['id']}")
                except Exception as e:
                    print(f"  취소 실패: {o['symbol']} {e}")
        except Exception as e:
            print(f"  미체결 주문 조회 실패: {e}")

        await asyncio.sleep(1)

        # 2) 모든 포지션 조회
        positions = await exchange.fetch_positions()
        open_positions = [p for p in positions if abs(float(p["info"]["positionAmt"])) > 0]

        if not open_positions:
            print("열린 포지션이 없습니다.")
        else:
            print(f"\n--- {len(open_positions)}개 포지션 청산 시작 ---")
            for pos in open_positions:
                sym = pos["symbol"]
                amt = float(pos["info"]["positionAmt"])
                close_side = "buy" if amt < 0 else "sell"
                close_qty = abs(amt)
                mark_price = float(pos.get("markPrice", 0) or pos.get("info", {}).get("markPrice", 0))

                print(f"\n  [{sym}] 수량={amt} | 청산방향={close_side.upper()} | markPrice={mark_price}")

                # 오더북에서 최우선 호가 조회
                try:
                    ob = await exchange.fetch_order_book(sym, 5)
                    if close_side == "sell":
                        # Long 청산 → Best Bid에 팔기
                        limit_price = ob["bids"][0][0] if ob.get("bids") and ob["bids"] else mark_price
                    else:
                        # Short 청산 → Best Ask에 사기
                        limit_price = ob["asks"][0][0] if ob.get("asks") and ob["asks"] else mark_price
                except Exception:
                    limit_price = mark_price

                if limit_price <= 0:
                    print(f"  -> {sym} 유효한 가격 없음, 스킵")
                    continue

                print(f"  -> 지정가 {close_side.upper()} {close_qty} @ {limit_price}")
                try:
                    order = await exchange.create_order(
                        sym, "limit", close_side, close_qty, limit_price,
                        {"reduceOnly": True, "timeInForce": "GTC"}
                    )
                    print(f"  -> 주문 접수 id={order['id']}")
                except Exception as e:
                    print(f"  -> 지정가 실패: {e}")
                    # Fallback: reduceOnly 없이 시도
                    try:
                        print(f"  -> reduceOnly 없이 재시도...")
                        order = await exchange.create_order(
                            sym, "limit", close_side, close_qty, limit_price,
                            {"timeInForce": "GTC"}
                        )
                        print(f"  -> 주문 접수 id={order['id']}")
                    except Exception as e2:
                        print(f"  -> 최종 실패: {e2}")

            # 3) 체결 대기 (최대 30초)
            print("\n--- 체결 대기 중 (30초) ---")
            for sec in range(30):
                await asyncio.sleep(1)
                remaining = await exchange.fetch_positions()
                still_open = [p for p in remaining if abs(float(p["info"]["positionAmt"])) > 0]
                if not still_open:
                    print(f"  {sec+1}초 후 전체 청산 확인!")
                    break
                if (sec + 1) % 5 == 0:
                    syms = [p["symbol"] for p in still_open]
                    print(f"  {sec+1}초... 잔여: {syms}")

        # 4) bot_state.json 포지션 비우기
        state_file = "bot_state.json"
        if os.path.exists(state_file):
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["positions"] = {}
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print("\nbot_state.json 포지션 초기화 완료")

        print("\n--- 전체 청산 완료! 봇을 재구동하세요. ---")

    except Exception as e:
        print(f"오류: {e}")
    finally:
        await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
