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

        # 전체 티커 캐시 (Dual Loop 아키텍처용) {symbol: ticker_dict}
        self.global_tickers: Dict[str, dict] = {}

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

        # ── 페어 교체(Pending Swap) 시스템 ────────────────────────────────────
        # 예약 교체: {'old_prefix': ('new_sym_a', 'new_sym_b')}
        # 활성 포지션이 종료될 때 자동으로 바통 터치
        self.pending_swaps: Dict[str, Tuple[str, str]] = {}

        # 스캔 진행 중 플래그 (중복 스캔 방지)
        self.scan_in_progress: bool = False

        # 런타임 페어 리스트 오버라이드 (bot_state.json에 저장/복구)
        # 서버 재시작 시 config.py 하드코딩 리스트 대신 이 리스트를 사용
        # None이면 config.py 기본값 사용, 리스트가 있으면 덮어쓰기
        self.active_pairs_override: list | None = None

        # 페어별 누적 승/패 통계 {prefix: {"win": N, "lose": N}}
        self.pair_stats: Dict[str, Dict[str, int]] = {}

        # 최신 단기 상관계수 캐시 {prefix: corr_value}
        self.latest_corr: Dict[str, float] = {}

        # 감시 일시 정지 페어 집합 (paused → pair_loop에서 스킵)
        self.paused_pairs: Set[str] = set()

    # ── 통계 헬퍼 ─────────────────────────────────────────────────────────────

    def record_trade(self, pnl_usdt: float, prefix: str = "") -> None:
        """청산 완료 시 누적 통계와 페어별 통계를 동시에 업데이트합니다."""
        self.total_trades   += 1
        self.cumulative_pnl += pnl_usdt
        if pnl_usdt >= 0:
            self.wins += 1

        # 페어별 통계 갱신
        if prefix:
            if prefix not in self.pair_stats:
                self.pair_stats[prefix] = {"win": 0, "lose": 0}
            if pnl_usdt >= 0:
                self.pair_stats[prefix]["win"] += 1
            else:
                self.pair_stats[prefix]["lose"] += 1

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.wins / self.total_trades * 100.0

    # ── PnL 계산 ──────────────────────────────────────────────────────────────

    def calc_pnl(
        self,
        pos: PairPosition,
        result_a: dict,
        result_b: dict,
    ) -> Tuple[float, float]:
        """
        수수료가 완전히 차감된 순수익(Net PnL)을 반환합니다.

        result_a, result_b 딕셔너리 필드:
          - total_realized_pnl: fetch_my_trades에서 합산한 거래소 정산 실현손익
          - total_fee: fetch_my_trades에서 합산한 거래소 실제 수수료
          - average, qty, maker: 가중평균가, 수량, Maker 여부
          위 필드가 None이면 실시간 감시(가상 청산) 모드 → 수학 수식 Fallback

        반환: (net_pnl_usdt, net_pnl_pct)
        """
        from config import LEVERAGE

        # 거래소 정산 값 추출 (fetch_my_trades 합산 결과)
        rpnl_a = result_a.get("total_realized_pnl")
        rpnl_b = result_b.get("total_realized_pnl")
        tfee_a = result_a.get("total_fee")
        tfee_b = result_b.get("total_fee")

        avg_a = result_a.get("average", 0.0)
        avg_b = result_b.get("average", 0.0)

        # 방어 로직: 체결가가 0이면 진입가로 대체 (오류 방지)
        if avg_a == 0.0: avg_a = pos.price_a
        if avg_b == 0.0: avg_b = pos.price_b

        qty_a = result_a.get("qty", 0.0)
        qty_b = result_b.get("qty", 0.0)

        # 방어 로직: 수량이 0이면 레버리지 기반으로 역산
        if qty_a == 0.0: qty_a = (pos.margin_a * LEVERAGE) / pos.price_a
        if qty_b == 0.0: qty_b = (pos.margin_b * LEVERAGE) / pos.price_b

        # ── 1) Gross PnL 산출 ──
        has_exchange_pnl = (rpnl_a is not None and rpnl_b is not None)

        if has_exchange_pnl:
            # 거래소 정산 값 직접 사용 (부분체결 완벽 합산)
            gross = float(rpnl_a) + float(rpnl_b)
        else:
            # 수학 수식 Fallback — Long/Short 방향 엄격 적용
            # LONG_A_SHORT_B: A는 롱(현재가-진입가), B는 숏(진입가-현재가)
            # SHORT_A_LONG_B: A는 숏(진입가-현재가), B는 롱(현재가-진입가)
            if pos.side == "LONG_A_SHORT_B":
                pnl_a = qty_a * (avg_a - pos.price_a)   # A 롱 손익
                pnl_b = qty_b * (pos.price_b - avg_b)   # B 숏 손익
            else:  # SHORT_A_LONG_B
                pnl_a = qty_a * (pos.price_a - avg_a)   # A 숏 손익
                pnl_b = qty_b * (avg_b - pos.price_b)   # B 롱 손익
            gross = pnl_a + pnl_b

        # ── 2) 수수료 계산 ──
        # 진입: 양 레그 모두 시장가(Taker)로 진입
        entry_notional = (qty_a * pos.price_a) + (qty_b * pos.price_b)
        entry_fee = entry_notional * TAKER_FEE_RATE

        if has_exchange_pnl and tfee_a is not None and tfee_b is not None:
            # 거래소 실제 수수료 (fetch_my_trades 합산)
            exit_fee = float(tfee_a) + float(tfee_b)
        else:
            # Fallback: 청산 호가 기반으로 수수료율 추산
            rate_a = MAKER_FEE_RATE if result_a.get("maker") else TAKER_FEE_RATE
            rate_b = MAKER_FEE_RATE if result_b.get("maker") else TAKER_FEE_RATE
            exit_fee = (qty_a * avg_a) * rate_a + (qty_b * avg_b) * rate_b

        total_fee = entry_fee + exit_fee

        # ── 3) 최종 Net PnL 산출 ──
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
