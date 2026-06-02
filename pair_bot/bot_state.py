# =============================================================================
# pair_bot/bot_state.py
# 20개 페어 루프와 텔레그램 봇이 공유하는 중앙 상태 저장소
# asyncio 환경에서 안전하게 읽고 쓰는 단순 dataclass 기반 설계
# =============================================================================

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Set, Tuple

from config import TAKER_FEE_RATE, MAKER_FEE_RATE


@dataclass
class PairPosition:
    """진입 중인 페어의 스냅샷 (PnL 계산 + CSV 로깅에 필요한 정보를 저장)."""
    prefix    : str
    side      : str       # "LONG_A_SHORT_B" | "SHORT_A_LONG_B"
    entry_ratio: float
    sym_a     : str
    sym_b     : str
    price_a   : float     # 진입 시 A 가격
    price_b   : float     # 진입 시 B 가격
    margin_a  : float     # A 레그 증거금 (USDT) - 변동성 가중치 기반
    margin_b  : float     # B 레그 증거금 (USDT) - 변동성 가중치 기반
    entry_time: datetime = field(default_factory=datetime.now)  # 진입 시각 (KST)
    entry_z_score: float = 0.0                                  # 진입 시 Z-Score

    @property
    def total_margin(self) -> float:
        """두 레그 합산 총 증거금."""
        return self.margin_a + self.margin_b

    @property
    def trade_usdt(self) -> float:
        """하위 호환용: 기존 trade_usdt 접근 시 총증거금의 절반 반환."""
        return self.total_margin / 2.0


class BotState:
    """
    전 모듈에서 공유하는 싱글턴 상태 객체.
    asyncio 단일 스레드 모델이므로 별도 Lock 없이 사용 가능.
    """

    def __init__(self):
        # 신규 진입 허용 여부 (텔레그램 '봇 정지' 버튼으로 제어)
        self.is_accepting_entries: bool = True

        # BTC 시장 폭주 감지 플래그 (True이면 신규 진입 전면 차단)
        self.market_turbulent: bool = False

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

        # 글로벌 킬 스위치
        self.initial_balance: float = 0.0        # 봇 시작 시 잔고 (기준값)
        self.kill_switch_triggered: bool = False  # 킬 스위치 발동 여부

        # 손절 쿨다운 및 일일 손절 횟수 영구 저장용
        self.cooldowns: Dict[str, float] = {}              # {prefix: timestamp}
        self.daily_stop_counts: Dict[str, int] = {}        # {prefix: count}
        self.daily_reset_date: str = ""                    # 일일 초기화 기준일 (YYYY-MM-DD)

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
        is_maker_exit: bool = False,
    ) -> Tuple[float, float]:
        """
        수수료가 완전히 차감된 순수익(Net PnL)을 반환합니다.
        변동성 가중치 비대칭 증거금(margin_a, margin_b) 기반 계산.
        
        반환: (net_pnl_usdt, net_pnl_pct)
        """
        notional_a = pos.margin_a * leverage
        notional_b = pos.margin_b * leverage
        qty_a = notional_a / pos.price_a
        qty_b = notional_b / pos.price_b

        # Gross PnL (수수료 전)
        if pos.side == "LONG_A_SHORT_B":
            gross = qty_a * (price_a - pos.price_a) + qty_b * (pos.price_b - price_b)
        else:  # SHORT_A_LONG_B
            gross = qty_a * (pos.price_a - price_a) + qty_b * (price_b - pos.price_b)

        # 수수료 계산
        total_notional = notional_a + notional_b
        entry_fee = total_notional * TAKER_FEE_RATE       # 진입 2회 (시장가)
        if is_maker_exit:
            exit_fee = total_notional * MAKER_FEE_RATE     # 청산 2회 (지정가 Maker)
        else:
            exit_fee = total_notional * TAKER_FEE_RATE     # 청산 2회 (시장가 Taker)
        total_fee = entry_fee + exit_fee

        net_pnl      = gross - total_fee
        total_margin = pos.total_margin
        net_pnl_pct  = (net_pnl / total_margin * 100) if total_margin > 0 else 0.0
        return net_pnl, net_pnl_pct

    def calc_gross_pnl(
        self,
        pos: PairPosition,
        price_a: float,
        price_b: float,
        leverage: int,
    ) -> Tuple[float, float]:
        """수수료 미차감 Gross PnL (참고용, 손절 등 내부 판단에 사용)."""
        notional_a = pos.margin_a * leverage
        notional_b = pos.margin_b * leverage
        qty_a = notional_a / pos.price_a
        qty_b = notional_b / pos.price_b

        if pos.side == "LONG_A_SHORT_B":
            gross = qty_a * (price_a - pos.price_a) + qty_b * (pos.price_b - price_b)
        else:
            gross = qty_a * (pos.price_a - price_a) + qty_b * (price_b - pos.price_b)

        total_margin = pos.total_margin
        gross_pct    = (gross / total_margin * 100) if total_margin > 0 else 0.0
        return gross, gross_pct
