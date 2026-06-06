# =============================================================================
# pair_bot/reset_stats.py
# 활성 포지션을 보호하면서 누적 거래 통계만 초기화하는 스크립트
# 사용법: python reset_stats.py
# =============================================================================

import json
import os
import sys
from datetime import datetime

STATE_FILE = "bot_state.json"


def main():
    if not os.path.exists(STATE_FILE):
        print(f"[ERROR] {STATE_FILE}이 존재하지 않습니다.")
        sys.exit(1)

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 리셋 전 현재 값 출력
    print("=" * 50)
    print("  리셋 전 통계")
    print("=" * 50)
    print(f"  total_trades   : {data.get('total_trades', 0)}")
    print(f"  wins           : {data.get('wins', 0)}")
    print(f"  cumulative_pnl : {data.get('cumulative_pnl', 0.0):+.4f} USDT")
    print(f"  활성 포지션    : {len(data.get('positions', {}))}개")
    print(f"  쿨다운 항목    : {len(data.get('cooldowns', {}))}개")
    print()

    # 활성 포지션 목록 표시
    positions = data.get("positions", {})
    if positions:
        print("  [보호 대상] 활성 포지션:")
        for prefix in positions:
            pos = positions[prefix]
            print(f"    - {prefix} | {pos.get('side', '?')} | 진입가 A={pos.get('price_a', 0):.4f} B={pos.get('price_b', 0):.4f}")
        print()

    # 통계만 리셋
    data["total_trades"]   = 0
    data["wins"]           = 0
    data["cumulative_pnl"] = 0.0
    data["pair_stats"]     = {}  # 페어별 승/패 통계도 함께 초기화

    # 저장 시각 갱신
    data["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print("=" * 50)
    print("  리셋 완료!")
    print("=" * 50)
    print(f"  total_trades   : 0")
    print(f"  wins           : 0")
    print(f"  cumulative_pnl : 0.0000 USDT")
    print(f"  활성 포지션    : {len(positions)}개 (변경 없음)")
    print()
    print("  봇을 재시작하지 않아도 다음 save_state 호출 시 반영됩니다.")


if __name__ == "__main__":
    main()
