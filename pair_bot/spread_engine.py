# =============================================================================
# pair_bot/spread_engine.py
# 가격 비율(Spread Ratio) 실시간 계산 엔진
# Z-Score 및 괴리율(%) 산출 담당
# =============================================================================

import collections
import math
import logging
from config import RATIO_WINDOW, ENTRY_THRESHOLD_PCT, EXIT_THRESHOLD_PCT, STOP_LOSS_PCT

logger = logging.getLogger("pair_bot")


class SpreadEngine:
    """
    두 자산의 가격 비율(ratio = price_A / price_B)을 수집하고
    이동평균/표준편차를 기반으로 현재 괴리 상태를 반환합니다.
    """

    def __init__(self):
        # 최근 N개 비율값을 저장하는 원형 큐
        self._window: collections.deque = collections.deque(maxlen=RATIO_WINDOW)

    # ── 공개 인터페이스 ────────────────────────────────────────────────────────

    def update(self, price_a: float, price_b: float) -> dict:
        """
        새 가격을 받아 윈도우에 추가한 뒤 현재 상태를 반환합니다.

        반환값:
            {
                "ratio"      : float,   현재 ratio
                "mean"       : float,   이동평균
                "std"        : float,   표준편차
                "z_score"    : float,   Z-Score
                "dev_pct"    : float,   괴리율 (%)
                "signal"     : str,     "ENTRY_LONG_A" | "ENTRY_LONG_B" | "EXIT" | "STOP" | "NONE"
                "ready"      : bool,    윈도우가 충분히 채워졌는지 여부
            }
        """
        ratio = price_a / price_b
        self._window.append(ratio)

        ready = len(self._window) >= max(30, RATIO_WINDOW // 10)  # 최소 10% 채워야 신호 활성화
        mean, std = self._calc_stats()
        z_score   = (ratio - mean) / std if std > 1e-12 else 0.0
        dev_pct   = ((ratio - mean) / mean) * 100.0 if mean > 1e-12 else 0.0

        signal = self._classify_signal(dev_pct, ready)

        return {
            "ratio"   : ratio,
            "mean"    : mean,
            "std"     : std,
            "z_score" : z_score,
            "dev_pct" : dev_pct,
            "signal"  : signal,
            "ready"   : ready,
        }

    @property
    def window_size(self) -> int:
        return len(self._window)

    # ── 내부 계산 ─────────────────────────────────────────────────────────────

    def _calc_stats(self) -> tuple[float, float]:
        """이동평균과 표준편차를 계산합니다."""
        if not self._window:
            return 1.0, 0.0

        data = list(self._window)
        n    = len(data)
        mean = sum(data) / n

        if n < 2:
            return mean, 0.0

        variance = sum((x - mean) ** 2 for x in data) / (n - 1)
        return mean, math.sqrt(variance)

    def _classify_signal(self, dev_pct: float, ready: bool) -> str:
        """
        괴리율을 기반으로 매매 신호를 분류합니다.

        dev_pct > 0  : ratio가 평균보다 높음 → A 고평가, B 저평가
        dev_pct < 0  : ratio가 평균보다 낮음 → A 저평가, B 고평가
        """
        if not ready:
            return "NONE"

        abs_dev = abs(dev_pct)

        # 1순위: 손절 (가장 먼저 체크)
        if abs_dev >= STOP_LOSS_PCT:
            return "STOP"

        # 2순위: 청산 회귀
        if abs_dev <= EXIT_THRESHOLD_PCT:
            return "EXIT"

        # 3순위: 진입
        if abs_dev >= ENTRY_THRESHOLD_PCT:
            if dev_pct > 0:
                # A가 고평가: A Short, B Long
                return "ENTRY_SHORT_A_LONG_B"
            else:
                # B가 고평가: B Short, A Long
                return "ENTRY_LONG_A_SHORT_B"

        return "NONE"
