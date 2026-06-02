# =============================================================================
# close_all_positions.py
# 현재 바이낸스 선물 계좌의 모든 열린 포지션을 시장가로 청산하는 1회용 스크립트
# 실행 후 bot_state.json의 포지션도 비워줍니다.
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
    # 데모(Testnet) 모드 활성화
    exchange.set_sandbox_mode(True)

    try:
        # 모든 포지션 조회
        positions = await exchange.fetch_positions()
        open_positions = [p for p in positions if abs(float(p["info"]["positionAmt"])) > 0]

        if not open_positions:
            print("열린 포지션이 없습니다.")
        else:
            print(f"--- {len(open_positions)}개 포지션 청산 시작 ---")
            for pos in open_positions:
                sym = pos["symbol"]
                amt = float(pos["info"]["positionAmt"])
                close_side = "buy" if amt < 0 else "sell"
                close_qty = abs(amt)

                print(f"  청산: {sym} | 수량={close_qty} | 방향={close_side.upper()}")
                try:
                    await exchange.create_order(
                        sym, "market", close_side, close_qty, {"reduceOnly": True}
                    )
                    print(f"  -> {sym} 청산 완료")
                except Exception as e:
                    print(f"  -> {sym} 청산 실패: {e}")

        # bot_state.json 포지션 비우기
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
