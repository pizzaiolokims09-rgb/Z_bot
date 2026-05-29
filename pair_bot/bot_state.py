# =============================================================================
# pair_bot/bot_state.py
# 5개 페어 루프와 텔레그램 봇이 공유하는 중앙 상태 저장소
# asyncio 환경에서 안전하게 읽고 쓰는 단순 dataclass 기반 설계
# =============================================================================

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Set, Tuple


@dataclass
class PairPosition:
    """진입 중인 페어의 스냅샷 (PnL 계산에 필요한 정보를 저장)."""
    prefix    : str
    side      : str     # "LONG_A_SHORT_B" | "SHORT_A_LONG_B"
    entry_ratio: float
    sym_a     : str
    sym_b     : str
    price_a   : float   # 진입 시 A 가격
    price_b   : float   # 진입 시 B 가격
    trade_usdt: float   # 레그당 증거금 (14% / 2)


class BotState:
    """
    전 모듈에서 공유하는 싱글턴 상태 객체.
    asyncio 단일 스레드 모델이므로 별도 Lock 없이 사용 가능.
    """

    def __init__(self):
        # 신규 진입 허용 여부 (텔레그램 '봇 정지' 버튼으로 제어)
        self.is_accepting_entries: bool = True

        # 현재 활성 포지션  {prefix: PairPosition}
        self.positions: Dict[str, PairPosition] = {}

        # 최신 괴리율      {prefix: dev_pct}
        self.latest_dev: Dict[str, float] = {}

        # 최신 가격        {prefix: (price_a, price_b)}
        self.latest_price: Dict[str, Tuple[float, float]] = {}

        # 텔레그램 '수동 청산' 요청 집합 — pair_loop가 다음 틱에 처리
        self.manual_close_requests: Set[str] = set()

        # 누적 거래 통계
        self.total_trades  : int   = 0
        self.wins          : int   = 0
        self.cumulative_pnl: float = 0.0

    # ── 통계 헬퍼 ─────────────────────────────────────────────────────────────

    def record_trade(self, pnl_usdt: float) -> None:
        """청산 완료 시 누적 통계를 업데이트합니다."""
        self.total_trades   += 1
        self.cumulative_pnl += pnl_usdt
        if pnl_usdt >= 0:
            self.wins += 1

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.wins / self.total_trades * 100.0

    # ── PnL 계산 ──────────────────────────────────────────────────────────────

    def calc_pnl(
        self,
        pos: PairPosition,
        price_a: float,
        price_b: float,
        leverage: int,
    ) -> Tuple[float, float]:
        """
        진입 정보와 현재 가격으로 실현 PnL(USDT)과 수익률(%)을 계산합니다.
        (슬리피지·수수료 미반영 — 참고용 추정값)
        """
        notional_a = pos.trade_usdt * leverage
        notional_b = pos.trade_usdt * leverage
        qty_a = notional_a / pos.price_a
        qty_b = notional_b / pos.price_b

        if pos.side == "LONG_A_SHORT_B":
            pnl = qty_a * (price_a - pos.price_a) + qty_b * (pos.price_b - price_b)
        else:  # SHORT_A_LONG_B
            pnl = qty_a * (pos.price_a - price_a) + qty_b * (price_b - pos.price_b)

        total_margin = pos.trade_usdt * 2            # 양 레그 합산 증거금
        pnl_pct      = (pnl / total_margin * 100) if total_margin > 0 else 0.0
        return pnl, pnl_pct
