# =============================================================================
# pair_bot/risk_manager.py
# 포지션 사이징, 델타 중립 수량 계산, 리스크 상태 관리
# =============================================================================

import logging
from config import LEVERAGE, STOP_LOSS_PCT

logger = logging.getLogger("pair_bot")


class RiskManager:
    """
    - 달러 기준 1:1 포지션 사이징 (델타 중립)
    - 현재 포지션 상태 추적
    - 손절 조건 판단
    """

    def __init__(self):
        # 현재 보유 포지션 정보
        # side: "SHORT_A_LONG_B" | "LONG_A_SHORT_B" | None
        self.position_side: str | None = None
        self.entry_ratio: float | None = None   # 진입 시점 ratio
        self.entry_notional: float = 0.0        # 진입 시 투입 USDT (레버리지 반영 전 증거금)

    @property
    def has_position(self) -> bool:
        return self.position_side is not None

    # ── 수량 계산 ─────────────────────────────────────────────────────────────

    def calc_qty(self, price_a: float, price_b: float, trade_usdt: float) -> dict:
        """
        trade_usdt 기준으로 달러 중립 수량을 계산합니다.

        trade_usdt: 한 레그에 투입할 증거금(USDT) — 계좌 잔고 x ALLOCATION_PER_PAIR / 2
        반환값:
            {
                "qty_a"        : float,  A 코인 수량
                "qty_b"        : float,  B 코인 수량
                "notional_a"   : float,  A 명목가치(USDT)
                "notional_b"   : float,  B 명목가치(USDT)
                "margin_each"  : float,  레그당 증거금(USDT)
            }
        """
        notional = trade_usdt * LEVERAGE   # 레버리지 적용 명목가치
        qty_a = notional / price_a
        qty_b = notional / price_b

        return {
            "qty_a"      : qty_a,
            "qty_b"      : qty_b,
            "notional_a" : qty_a * price_a,
            "notional_b" : qty_b * price_b,
            "margin_each": trade_usdt,
        }

    # ── 포지션 상태 기록/해제 ─────────────────────────────────────────────────

    def open_position(self, side: str, entry_ratio: float, trade_usdt: float, pair_prefix: str = ""):
        """진입 시 포지션 상태를 기록합니다."""
        self.position_side  = side
        self.entry_ratio    = entry_ratio
        self.entry_notional = trade_usdt
        logger.info(f"[{pair_prefix}] [포지션 열림] side={side}, entry_ratio={entry_ratio:.6f}, margin={trade_usdt:.2f}USDT")

    def close_position(self, pair_prefix: str = ""):
        """청산/손절 후 포지션 상태를 초기화합니다."""
        ratio_str = f"{self.entry_ratio:.6f}" if self.entry_ratio is not None else "N/A"
        logger.info(f"[{pair_prefix}] [포지션 닫힘] side={self.position_side}, entry_ratio={ratio_str}")
        self.position_side  = None
        self.entry_ratio    = None
        self.entry_notional = 0.0

    # ── 손절 조건 체크 ────────────────────────────────────────────────────────

    def should_stop_loss(self, dev_pct: float) -> bool:
        """
        포지션 보유 중 괴리가 더 벌어진 경우(손절 임계치 도달) True를 반환합니다.
        신호 분류기에서 이미 STOP 신호를 처리하지만,
        방향이 반전된 경우도 커버하기 위해 별도로 체크합니다.
        """
        if not self.has_position:
            return False
        return abs(dev_pct) >= STOP_LOSS_PCT

    # ── 현재 손익 추정 ────────────────────────────────────────────────────────

    def estimate_pnl_pct(self, current_ratio: float) -> float:
        """
        진입 ratio와 현재 ratio의 차이로 간략한 손익(%)을 추정합니다.
        (슬리피지, 수수료 미반영 — 참고용)
        """
        if self.entry_ratio is None or self.entry_ratio == 0:
            return 0.0
        change = (current_ratio - self.entry_ratio) / self.entry_ratio * 100.0
        # SHORT_A_LONG_B: ratio 하락 시 이익
        if self.position_side == "SHORT_A_LONG_B":
            return -change
        # LONG_A_SHORT_B: ratio 상승 시 이익
        return change
