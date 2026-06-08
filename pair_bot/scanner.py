# =============================================================================
# pair_bot/scanner.py
# 다이내믹 포트폴리오 스캐너 — 4단계 파이프라인
# 바이낸스 선물 전체 시장에서 최적 페어를 자동 발굴합니다.
# asyncio.to_thread 기반 논블로킹 설계 (메인 매매 루프 무중단)
# =============================================================================

import asyncio
import itertools
import logging
from typing import List, Dict

import numpy as np

from config import (
    SCANNER_TOP_N_COINS,
    SCANNER_CORR_THRESHOLD,
    SCANNER_COINT_PVALUE,
    SCANNER_MAX_PAIRS,
    SCANNER_OHLCV_TIMEFRAME,
    SCANNER_OHLCV_LIMIT,
    SCANNER_FETCH_DELAY,
    SCANNER_CHUNK_SIZE,
    SCANNER_EXCLUDE_COINS,
    MAX_PAIRS_PER_COIN,
    SECTOR_MAP,
)

logger = logging.getLogger("pair_bot")


class DynamicScanner:
    """
    4단계 파이프라인으로 전체 바이낸스 선물 시장에서
    최적의 페어 트레이딩 후보를 자동 발굴합니다.

    Step 1: 거래대금 상위 60개 우량주 필터
    Step 2: 15분봉 200개 OHLCV 데이터 수집
    Step 3: 섹터 동조화 필터 + 피어슨 상관계수 >= 0.7 고속 필터
    Step 4: 공적분 검정 p-value <= 0.05 오디션 + 코인 중복 제한 → Top 15 선발
    """

    # ── 메인 진입점 ───────────────────────────────────────────────────────────

    async def run_full_scan(self, exchange) -> List[dict]:
        """
        4단계 파이프라인을 순차 실행하여 최종 Top N 페어를 반환합니다.

        반환: [{pair, sym_a, sym_b, corr, coint_pvalue, score, data_points}, ...]
              score 내림차순 정렬, 빈 리스트면 유효 페어 없음
        """
        # Step 1: 거래대금 상위 코인 필터
        top_coins = await self._fetch_top_coins(exchange, SCANNER_TOP_N_COINS)
        if len(top_coins) < 2:
            logger.warning("[DynScanner] Step 1 실패: 우량주 2개 미만")
            return []
        logger.info(
            f"[DynScanner] Step 1 완료 | 상위 {len(top_coins)}개 코인 선별"
        )

        # Step 2: OHLCV 데이터 수집
        prices = await self._fetch_ohlcv_bulk(
            exchange, top_coins,
            SCANNER_OHLCV_TIMEFRAME, SCANNER_OHLCV_LIMIT,
        )
        if len(prices) < 2:
            logger.warning(
                f"[DynScanner] Step 2 실패: 가격 수집 성공 코인 {len(prices)}개"
            )
            return []
        logger.info(
            f"[DynScanner] Step 2 완료 | {len(prices)}개 코인 가격 수집 성공"
        )

        # Step 3 + 4: CPU 연산을 별도 스레드에서 실행 (논블로킹)
        results = await asyncio.to_thread(self._blocking_scan, prices)
        logger.info(
            f"[DynScanner] Step 3+4 완료 | "
            f"최종 선발 {len(results)}개 페어"
        )
        return results

    # ── Step 1: 거래대금 상위 코인 필터 ───────────────────────────────────────

    async def _fetch_top_coins(
        self, exchange, count: int = 60
    ) -> List[str]:
        """
        바이낸스 선물의 전체 티커를 조회하여
        24시간 거래대금(quoteVolume) 상위 N개 코인의 base 심볼을 반환합니다.
        스테이블코인, 레버리지 토큰 등은 제외합니다.
        """
        try:
            tickers = await exchange.fetch_tickers()
        except Exception as e:
            logger.error(f"[DynScanner] 전체 티커 조회 실패: {e}")
            return []

        # USDT 페어만 추출 + 거래대금 기준 정렬
        candidates = []
        for symbol, ticker in tickers.items():
            # 선물 심볼 형식: BTC/USDT:USDT
            if not symbol.endswith("/USDT:USDT"):
                continue

            base = symbol.split("/")[0]

            # 제외 필터: 스테이블코인, 레버리지 토큰(UP, DOWN, BULL, BEAR)
            if base in SCANNER_EXCLUDE_COINS:
                continue
            if any(base.endswith(suffix) for suffix in
                   ("UP", "DOWN", "BULL", "BEAR")):
                continue

            quote_vol = float(ticker.get("quoteVolume", 0) or 0)
            if quote_vol <= 0:
                continue

            candidates.append((base, quote_vol))

        # 거래대금 내림차순 정렬 → 상위 N개
        candidates.sort(key=lambda x: x[1], reverse=True)
        top = [c[0] for c in candidates[:count]]

        if top:
            logger.debug(
                f"[DynScanner] Top {len(top)} 코인: "
                f"{', '.join(top[:10])}{'...' if len(top) > 10 else ''}"
            )
        return top

    # ── Step 2: OHLCV 데이터 수집 ────────────────────────────────────────────

    async def _fetch_ohlcv_bulk(
        self,
        exchange,
        coins: List[str],
        timeframe: str,
        limit: int,
    ) -> Dict[str, np.ndarray]:
        """
        코인 리스트의 종가 배열을 비동기로 수집합니다.
        Rate Limit 방어: SCANNER_CHUNK_SIZE개씩 청크, 청크 간 딜레이
        """
        prices: Dict[str, np.ndarray] = {}

        for i in range(0, len(coins), SCANNER_CHUNK_SIZE):
            chunk = coins[i:i + SCANNER_CHUNK_SIZE]

            for coin in chunk:
                symbol = f"{coin}/USDT:USDT"
                try:
                    ohlcv = await exchange.fetch_ohlcv(
                        symbol, timeframe=timeframe, limit=limit,
                    )
                    if ohlcv and len(ohlcv) >= 100:
                        closes = np.array(
                            [c[4] for c in ohlcv], dtype=np.float64
                        )
                        prices[coin] = closes
                    else:
                        logger.debug(
                            f"[DynScanner] {coin} 데이터 부족 "
                            f"({len(ohlcv) if ohlcv else 0}개) — 스킵"
                        )
                except Exception as e:
                    logger.debug(f"[DynScanner] {coin} OHLCV 수집 실패: {e}")

                await asyncio.sleep(SCANNER_FETCH_DELAY)

            # 청크 간 추가 딜레이 (Rate Limit 보강)
            if i + SCANNER_CHUNK_SIZE < len(coins):
                await asyncio.sleep(0.5)

            # 진행률 로깅
            done = min(i + SCANNER_CHUNK_SIZE, len(coins))
            logger.debug(
                f"[DynScanner] OHLCV 수집 진행: {done}/{len(coins)} "
                f"({done / len(coins) * 100:.0f}%)"
            )

        return prices

    # ── Step 3 + 4: CPU 연산 (동기 함수, to_thread로 호출) ────────────────────

    @staticmethod
    def _blocking_scan(prices: Dict[str, np.ndarray]) -> List[dict]:
        """
        Step 3: 섹터 동조화 필터 + 피어슨 상관계수 >= 0.7 필터 (고속)
        Step 4: 공적분 검정 p-value <= 0.05 오디션
                + 코인 중복 출현 제한 (MAX_PAIRS_PER_COIN) → Top N 선발

        이 함수는 동기 함수이며, asyncio.to_thread()로 별도 스레드에서 실행됩니다.
        """
        from statsmodels.tsa.stattools import coint
        from collections import defaultdict

        coins = list(prices.keys())
        total_combos = len(coins) * (len(coins) - 1) // 2
        logger.info(
            f"[DynScanner] Step 3 시작 | "
            f"{len(coins)}개 코인 × {total_combos}개 조합"
        )

        # ── Step 3: 섹터 동조화 필터 + 상관계수 고속 필터 ──
        # 허용 조건: (1) 같은 섹터끼리 OR (2) 한쪽이 Macro(BTC/ETH)
        corr_passed = []
        sector_skipped = 0
        for coin_a, coin_b in itertools.combinations(coins, 2):
            # 섹터 동조화 필터 (이종 교배 차단)
            sec_a = SECTOR_MAP.get(coin_a, "ETC")
            sec_b = SECTOR_MAP.get(coin_b, "ETC")
            same_sector = (sec_a == sec_b)
            has_macro = (sec_a == "Macro" or sec_b == "Macro")
            if not same_sector and not has_macro:
                sector_skipped += 1
                continue

            arr_a = prices[coin_a]
            arr_b = prices[coin_b]

            min_len = min(len(arr_a), len(arr_b))
            if min_len < 100:
                continue
            a = arr_a[-min_len:]
            b = arr_b[-min_len:]

            corr = float(np.corrcoef(a, b)[0, 1])
            if abs(corr) >= SCANNER_CORR_THRESHOLD:
                corr_passed.append((coin_a, coin_b, a, b, corr))

        logger.info(
            f"[DynScanner] Step 3 완료 | "
            f"섹터 필터로 {sector_skipped}개 이종 조합 차단 | "
            f"상관계수 >= {SCANNER_CORR_THRESHOLD} 통과: "
            f"{len(corr_passed)}개 페어"
        )

        if not corr_passed:
            return []

        # ── Step 4: 공적분 오디션 ──
        coint_candidates = []
        for coin_a, coin_b, a, b, corr in corr_passed:
            try:
                _, pvalue, _ = coint(a, b)
                pvalue = float(pvalue)
            except Exception:
                continue

            if pvalue > SCANNER_COINT_PVALUE:
                continue

            # 종합 점수: 상관계수 높을수록 + p-value 낮을수록 좋음
            score = abs(corr) * (1.0 - pvalue)

            coint_candidates.append({
                "pair": f"{coin_a}-{coin_b}",
                "sym_a": f"{coin_a}/USDT",
                "sym_b": f"{coin_b}/USDT",
                "coin_a": coin_a,
                "coin_b": coin_b,
                "corr": round(corr, 4),
                "coint_pvalue": round(pvalue, 4),
                "score": round(score, 4),
                "data_points": min(len(a), len(b)),
            })

        # score 내림차순 정렬
        coint_candidates.sort(key=lambda x: x["score"], reverse=True)

        logger.info(
            f"[DynScanner] Step 4-A | "
            f"공적분 p<={SCANNER_COINT_PVALUE} 통과: "
            f"{len(coint_candidates)}개 후보"
        )

        # ── 코인 중복 출현 제한 (포트폴리오 분산) ──
        # score 순서대로 뽑되, 한 코인이 MAX_PAIRS_PER_COIN 초과하면 Skip
        coin_count: Dict[str, int] = defaultdict(int)
        results = []
        skipped_by_limit = 0

        for cand in coint_candidates:
            ca = cand["coin_a"]
            cb = cand["coin_b"]

            # 어느 한쪽이라도 한도 초과 시 Skip
            if (coin_count[ca] >= MAX_PAIRS_PER_COIN or
                    coin_count[cb] >= MAX_PAIRS_PER_COIN):
                skipped_by_limit += 1
                continue

            # 선발 확정 → 카운트 증가
            coin_count[ca] += 1
            coin_count[cb] += 1

            # 최종 결과에서 내부용 키 제거
            result = {k: v for k, v in cand.items()
                      if k not in ("coin_a", "coin_b")}
            results.append(result)

            if len(results) >= SCANNER_MAX_PAIRS:
                break

        logger.info(
            f"[DynScanner] Step 4-B 완료 | "
            f"코인 분산 필터(MAX={MAX_PAIRS_PER_COIN})로 "
            f"{skipped_by_limit}개 Skip | "
            f"최종 {len(results)}개 선발"
        )
        return results
