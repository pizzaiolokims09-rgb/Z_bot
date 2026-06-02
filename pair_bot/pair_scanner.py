# =============================================================================
# pair_bot/pair_scanner.py
# 섹터 제한형 페어 스캐너 — 상관계수 + ADF 공적분 검정
# asyncio.to_thread 기반 논블로킹 설계 (메인 매매 루프 무중단)
# =============================================================================

import asyncio
import itertools
import logging
from typing import List, Dict, Optional

import numpy as np

from config import (
    SECTORS,
    SCAN_CORR_THRESHOLD,
    SCAN_ADF_PVALUE,
    SCAN_DATA_HOURS,
    SCAN_TIMEFRAME,
    SCAN_FETCH_DELAY,
)

logger = logging.getLogger("pair_bot")


class PairScanner:
    """
    특정 섹터 내의 모든 코인 조합에 대해
    상관계수 + ADF 공적분 검정을 수행하고 최적 페어를 추천합니다.

    사용법:
        scanner = PairScanner()
        results = await scanner.scan_sector("AI", exchange)
    """

    # ── 비동기 진입점 ─────────────────────────────────────────────────────────

    async def scan_sector(
        self,
        sector_name: str,
        exchange,
    ) -> List[dict]:
        """
        1) 섹터 내 코인들의 과거 가격을 async로 수집 (Rate Limit 방어 포함)
        2) 수학 연산(상관계수/ADF)은 asyncio.to_thread로 별도 스레드에서 실행
        3) 메인 이벤트 루프는 절대 블로킹되지 않음

        반환: [{pair, sym_a, sym_b, corr, adf_pvalue, score}, ...]
              score 내림차순 정렬, 빈 리스트면 유효 페어 없음
        """
        coins = SECTORS.get(sector_name)
        if not coins:
            logger.warning(f"[스캐너] 알 수 없는 섹터: {sector_name}")
            return []

        if len(coins) < 2:
            logger.warning(f"[스캐너] {sector_name} 섹터에 코인이 2개 미만")
            return []

        logger.info(
            f"[스캐너] {sector_name} 섹터 스캔 시작 | "
            f"코인={len(coins)}개 | 조합={len(coins)*(len(coins)-1)//2}개"
        )

        # 1단계: 가격 데이터 수집 (async, Rate Limit 방어)
        prices = await self._fetch_all_prices(coins, exchange)

        if len(prices) < 2:
            logger.warning(f"[스캐너] 가격 수집 실패 — 유효 코인 {len(prices)}개")
            return []

        # 2단계: CPU 연산을 별도 스레드에서 실행 (논블로킹)
        results = await asyncio.to_thread(self._blocking_scan, prices)

        logger.info(
            f"[스캐너] {sector_name} 스캔 완료 | "
            f"유효 페어={len(results)}개"
        )
        return results

    # ── 가격 데이터 수집 (async + Rate Limit 방어) ────────────────────────────

    async def _fetch_all_prices(
        self, coins: List[str], exchange
    ) -> Dict[str, np.ndarray]:
        """
        섹터 내 각 코인의 1h 종가 배열을 수집합니다.
        바이낸스 Rate Limit 방어: 코인 간 SCAN_FETCH_DELAY(0.3초) 간격
        """
        prices: Dict[str, np.ndarray] = {}

        for coin in coins:
            symbol = f"{coin}/USDT:USDT"
            try:
                ohlcv = await exchange.fetch_ohlcv(
                    symbol,
                    timeframe=SCAN_TIMEFRAME,
                    limit=SCAN_DATA_HOURS,
                )
                if ohlcv and len(ohlcv) >= 100:
                    closes = np.array([c[4] for c in ohlcv], dtype=np.float64)
                    prices[coin] = closes
                    logger.debug(
                        f"[스캐너] {coin} 가격 수집 완료 | {len(closes)}개"
                    )
                else:
                    logger.debug(
                        f"[스캐너] {coin} 데이터 부족 ({len(ohlcv) if ohlcv else 0}개) — 스킵"
                    )
            except Exception as e:
                logger.debug(f"[스캐너] {coin} 가격 수집 실패: {e}")

            # Rate Limit 방어: 코인 간 최소 0.3초 간격
            await asyncio.sleep(SCAN_FETCH_DELAY)

        return prices

    # ── CPU 바운드 스캔 연산 (동기 함수, to_thread로 호출) ─────────────────────

    @staticmethod
    def _blocking_scan(prices: Dict[str, np.ndarray]) -> List[dict]:
        """
        모든 코인 조합에 대해 상관계수 + ADF 검정을 실행합니다.
        이 함수는 동기 함수이며, asyncio.to_thread()로 별도 스레드에서 실행됩니다.

        반환: [{pair, sym_a, sym_b, corr, adf_pvalue, score}, ...]
        """
        from statsmodels.tsa.stattools import adfuller

        coins = list(prices.keys())
        results = []

        for coin_a, coin_b in itertools.combinations(coins, 2):
            arr_a = prices[coin_a]
            arr_b = prices[coin_b]

            # 길이 맞추기 (짧은 쪽에 맞춤)
            min_len = min(len(arr_a), len(arr_b))
            if min_len < 100:
                continue
            a = arr_a[-min_len:]
            b = arr_b[-min_len:]

            # 1) 상관계수 필터
            corr = float(np.corrcoef(a, b)[0, 1])
            if abs(corr) < SCAN_CORR_THRESHOLD:
                continue

            # 2) ADF 공적분 검정 — 가격 비율(spread)의 정상성 테스트
            try:
                spread = a / b
                adf_result = adfuller(spread, autolag="AIC")
                adf_pvalue = float(adf_result[1])
            except Exception:
                continue

            if adf_pvalue > SCAN_ADF_PVALUE:
                continue

            # 3) 종합 점수: 상관계수 높을수록 + p-value 낮을수록 좋음
            score = abs(corr) * (1.0 - adf_pvalue)

            results.append({
                "pair": f"{coin_a}-{coin_b}",
                "sym_a": f"{coin_a}/USDT",
                "sym_b": f"{coin_b}/USDT",
                "corr": round(corr, 4),
                "adf_pvalue": round(adf_pvalue, 4),
                "score": round(score, 4),
                "data_points": min_len,
            })

        # score 내림차순 정렬
        results.sort(key=lambda x: x["score"], reverse=True)
        return results
