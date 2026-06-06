# =============================================================================
# pair_bot/spread_engine.py
# 가격 비율(Spread Ratio) 실시간 계산 엔진
# 듀얼 윈도우(스윙 24h / 단기 12h) Z-Score 산출
# + 추세 감지(선형 회귀 기울기) 필터
# =============================================================================

import collections
import math
import logging
from config import (
    RATIO_WINDOW_SWING, RATIO_WINDOW_SHORT,
    ENTRY_THRESHOLD_PCT, EXIT_THRESHOLD_PCT, STOP_LOSS_PCT,
    ENTRY_Z_SCORE_SWING, ENTRY_Z_SCORE_SHORT,
    EXIT_Z_SCORE, STOP_LOSS_Z_SCORE,
    MIN_SPREAD_THRESHOLD,
    MIN_WARMUP_RATIO, TREND_SLOPE_THRESHOLD_PCT, TREND_SLOPE_WINDOW,
)

logger = logging.getLogger("pair_bot")


class SpreadEngine:
    """
    두 자산의 가격 비율(ratio = price_A / price_B)을 수집하고
    듀얼 윈도우(스윙 24h / 단기 12h)의 이동평균/표준편차를 기반으로
    현재 괴리 상태를 반환합니다.
    """

    def __init__(self):
        # 스윙 윈도우 (24시간 = 8640 포인트 @ 10초 폴링)
        self._window_swing: collections.deque = collections.deque(maxlen=RATIO_WINDOW_SWING)
        # 단기 윈도우 (12시간 = 4320 포인트 @ 10초 폴링)
        self._window_short: collections.deque = collections.deque(maxlen=RATIO_WINDOW_SHORT)
        # 변동성 계산용 개별 코인 가격 히스토리 (스윙 윈도우와 동일 크기)
        self._prices_a: collections.deque = collections.deque(maxlen=RATIO_WINDOW_SWING)
        self._prices_b: collections.deque = collections.deque(maxlen=RATIO_WINDOW_SWING)

    # ── 공개 인터페이스 ────────────────────────────────────────────────────────

    def update(self, price_a: float, price_b: float) -> dict:
        """
        새 가격을 받아 두 윈도우에 추가한 뒤 현재 상태를 반환합니다.

        반환값:
            {
                "ratio"          : float,   현재 ratio
                "mean"           : float,   스윙 윈도우 이동평균
                "std"            : float,   스윙 윈도우 표준편차
                "z_score"        : float,   스윙 Z-Score (로그/청산 기준)
                "z_score_swing"  : float,   스윙 윈도우 Z-Score (24h)
                "z_score_short"  : float,   단기 윈도우 Z-Score (12h)
                "dev_pct"        : float,   스윙 기준 괴리율 (%)
                "signal"         : str,     진입/청산/손절 시그널
                "ready"          : bool,    윈도우가 충분히 채워졌는지
                "slope"          : float,   ratio 선형 회귀 기울기
                "is_trending"    : bool,    추세 감지 여부
                "entry_window"   : str,     진입 트리거 윈도우 ("swing" / "short" / "")
            }
        """
        ratio = price_a / price_b
        self._window_swing.append(ratio)
        self._window_short.append(ratio)
        self._prices_a.append(price_a)
        self._prices_b.append(price_b)

        # 워밍업: 스윙 윈도우의 MIN_WARMUP_RATIO(50%) 이상 채워야 신호 활성화
        min_ready = max(30, int(RATIO_WINDOW_SWING * MIN_WARMUP_RATIO))
        ready = len(self._window_swing) >= min_ready

        # 스윙 윈도우 통계 (24h — 청산/손절/로그 기준)
        mean_sw, std_sw = self._calc_stats(self._window_swing)
        z_swing = (ratio - mean_sw) / std_sw if std_sw > 1e-12 else 0.0
        dev_pct = ((ratio - mean_sw) / mean_sw) * 100.0 if mean_sw > 1e-12 else 0.0

        # 단기 윈도우 통계 (12h — 단기 극단 진입용)
        mean_sh, std_sh = self._calc_stats(self._window_short)
        z_short = (ratio - mean_sh) / std_sh if std_sh > 1e-12 else 0.0

        # 추세 감지 (정규화 % — 페어 비율 스케일에 무관)
        slope = self._calc_slope(self._window_swing)
        normalized_slope_pct = (abs(slope) / mean_sw * 100) if mean_sw > 1e-12 else 0.0
        is_trending = normalized_slope_pct > TREND_SLOPE_THRESHOLD_PCT

        signal, entry_window = self._classify_signal(
            z_swing, z_short, dev_pct, ready, is_trending
        )

        return {
            "ratio"         : ratio,
            "mean"          : mean_sw,
            "std"           : std_sw,
            "z_score"       : z_swing,     # 기본 Z-Score = 스윙 기준
            "z_score_swing" : z_swing,
            "z_score_short" : z_short,
            "dev_pct"       : dev_pct,
            "signal"        : signal,
            "ready"         : ready,
            "slope"         : slope,
            "normalized_slope_pct": normalized_slope_pct,
            "is_trending"   : is_trending,
            "entry_window"  : entry_window,
        }

    @property
    def window_size(self) -> int:
        """스윙 윈도우 크기 반환 (워밍업 기준)."""
        return len(self._window_swing)

    def get_volatilities(self) -> tuple[float, float]:
        """
        A, B 코인의 수익률 표준편차(변동성)를 계산합니다.
        메모리에 이미 축적된 가격 데이터를 사용하므로 추가 API 호출 없음.

        반환: (vol_a, vol_b)
          - 데이터가 30개 미만이면 (1.0, 1.0) → 50:50 균등 배분으로 Fallback
        """
        min_data = 30
        if len(self._prices_a) < min_data or len(self._prices_b) < min_data:
            return 1.0, 1.0

        vol_a = self._calc_return_std(self._prices_a)
        vol_b = self._calc_return_std(self._prices_b)

        # 변동성이 0이면 균등 배분 Fallback
        if vol_a < 1e-12 or vol_b < 1e-12:
            return 1.0, 1.0

        return vol_a, vol_b

    @staticmethod
    def _calc_return_std(prices: collections.deque) -> float:
        """가격 배열의 수익률 표준편차를 계산합니다."""
        data = list(prices)
        n = len(data)
        if n < 2:
            return 0.0
        returns = [(data[i] - data[i - 1]) / data[i - 1] for i in range(1, n) if data[i - 1] != 0]
        if len(returns) < 2:
            return 0.0
        mean_r = sum(returns) / len(returns)
        variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        return math.sqrt(variance)

    # ── 내부 계산 ─────────────────────────────────────────────────────────────

    def _calc_stats(self, window: collections.deque) -> tuple[float, float]:
        """이동평균과 표준편차를 계산합니다."""
        if not window:
            return 1.0, 0.0

        data = list(window)
        n    = len(data)
        mean = sum(data) / n

        if n < 2:
            return mean, 0.0

        variance = sum((x - mean) ** 2 for x in data) / (n - 1)
        return mean, math.sqrt(variance)

    def _calc_slope(self, window: collections.deque) -> float:
        """
        최근 ratio 데이터의 선형 회귀 기울기 (추세 감지).
        3초 폴링 기준 100개 = 최근 5분간 기울기를 계산합니다.
        기울기가 양수면 ratio 상승 추세, 음수면 하락 추세.
        """
        n = len(window)
        if n < TREND_SLOPE_WINDOW:
            return 0.0

        sample_size = min(TREND_SLOPE_WINDOW, n)
        recent = list(window)[-sample_size:]
        n_s = len(recent)
        x_mean = (n_s - 1) / 2.0
        y_mean = sum(recent) / n_s

        numerator = sum(
            (i - x_mean) * (y - y_mean)
            for i, y in enumerate(recent)
        )
        denominator = sum((i - x_mean) ** 2 for i in range(n_s))
        return numerator / denominator if denominator > 0 else 0.0

    def _classify_signal(
        self,
        z_swing: float,
        z_short: float,
        dev_pct: float,
        ready: bool,
        is_trending: bool = False,
    ) -> tuple[str, str]:
        """
        듀얼 윈도우 하이브리드 진입 조건:

        [스윙 타점 — Window A (24h)]
          |z_swing| >= 2.5 AND |dev_pct| >= 1.5% → 진입

        [단기 타점 — Window B (12h)]
          |z_short| >= 3.0 AND |dev_pct| >= 1.5% → 진입

        두 조건 중 하나라도 만족 시 진입 (OR)
        + 추세 감지 시 진입 차단

        손절/익절은 스윙 윈도우(24h) 기준으로 통일

        반환: (signal, entry_window)
        """
        if not ready:
            return "NONE", ""

        abs_z_sw = abs(z_swing)
        abs_z_sh = abs(z_short)
        abs_dev = abs(dev_pct)

        # 1순위: 진입 — 듀얼 윈도우 OR 조건 + 추세 필터
        if is_trending:
            return "NONE", ""  # 추세 중이면 진입하지 않음

        # 공통 괴리율 조건
        if abs_dev < MIN_SPREAD_THRESHOLD:
            return "NONE", ""

        # Window A: 스윙 (24h) 진입 확인
        swing_triggered = abs_z_sw >= ENTRY_Z_SCORE_SWING
        # Window B: 단기 (12h) 진입 확인
        short_triggered = abs_z_sh >= ENTRY_Z_SCORE_SHORT

        if swing_triggered or short_triggered:
            # 진입 방향 결정 (트리거된 윈도우의 Z-Score 부호 기준)
            if swing_triggered:
                entry_win = "swing"
                z_for_dir = z_swing
            else:
                entry_win = "short"
                z_for_dir = z_short

            if z_for_dir > 0:
                return "ENTRY_SHORT_A_LONG_B", entry_win
            else:
                return "ENTRY_LONG_A_SHORT_B", entry_win

        return "NONE", ""
