# =============================================================================
# pair_bot/risk_manager.py
# 포지션 사이징, 델타 중립 수량 계산, 리스크 상태 관리
# =============================================================================

import logging
from config import LEVERAGE, STOP_LOSS_PCT, MIN_NOTIONAL_USDT

logger = logging.getLogger("pair_bot")


class RiskManager:
    """
    - 변동성 가중치 기반 비대칭 포지션 사이징 (Delta Neutral)
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

    def calc_qty(
        self,
        price_a: float,
        price_b: float,
        total_margin_usdt: float,
        vol_a: float = 1.0,
        vol_b: float = 1.0,
    ) -> dict | None:
        """
        변동성 역가중치(Volatility Parity) 기반으로 비대칭 수량을 계산합니다.

        total_margin_usdt: 두 레그 합산 총 증거금 (잔고 x ALLOCATION_PER_PAIR)
        vol_a, vol_b: 각 코인의 수익률 표준편차 (SpreadEngine.get_volatilities())
            - 워밍업 미완료 시 (1.0, 1.0) → 기존 50:50 균등 배분과 동일

        반환값:
            dict: qty_a, qty_b, notional_a, notional_b, margin_a, margin_b
            None: Min Notional 미달로 진입 불가 시
        """
        # 변동성 역가중치: 변동성이 큰 코인에 적은 금액 배분
        vol_sum  = vol_a + vol_b
        weight_a = vol_b / vol_sum   # A의 vol이 작을수록 A에 더 많이
        weight_b = vol_a / vol_sum

        margin_a = total_margin_usdt * weight_a
        margin_b = total_margin_usdt * weight_b

        notional_a = margin_a * LEVERAGE
        notional_b = margin_b * LEVERAGE

        # Min Notional 방어 — 바이낸스 최소 주문 금액 미달 시 진입 차단
        if notional_a < MIN_NOTIONAL_USDT or notional_b < MIN_NOTIONAL_USDT:
            logger.warning(
                f"Min Notional 미달: notional_A={notional_a:.2f}, "
                f"notional_B={notional_b:.2f} (최소={MIN_NOTIONAL_USDT})"
            )
            return None

        qty_a = notional_a / price_a
        qty_b = notional_b / price_b

        return {
            "qty_a"      : qty_a,
            "qty_b"      : qty_b,
            "notional_a" : notional_a,
            "notional_b" : notional_b,
            "margin_a"   : margin_a,
            "margin_b"   : margin_b,
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
