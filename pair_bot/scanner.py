# =============================================================================
# pair_bot/scanner.py
# 다이내믹 포트폴리오 스캐너 — 4단계 파이프라인
# 바이낸스 선물 전체 시장에서 최적 페어를 자동 발굴합니다.
# asyncio.to_thread 기반 논블로킹 설계 (메인 매매 루프 무중단)
# =============================================================================

import asyncio
import itertools
import logging
import re
from typing import List, Dict

import numpy as np

from config import (
    SCANNER_TOP_N_COINS,
    SCANNER_CORR_THRESHOLD,
    SCANNER_COINT_PVALUE,
    SCANNER_MAX_PAIRS,
    SCANNER_OHLCV_TIMEFRAME,
    SCANNER_OHLCV_LIMIT,
    SCANNER_MIN_BARS,
    SCANNER_FETCH_DELAY,
    SCANNER_CHUNK_SIZE,
    SCANNER_EXCLUDE_COINS,
    SCANNER_MIN_QUOTE_VOLUME,
    SCANNER_MAX_SPREAD_BP,
    SCANNER_MIN_SURVIVAL_SCANS,
    MAX_PAIRS_PER_COIN,
    SECTOR_MAP,
)

logger = logging.getLogger("pair_bot")

# 유효한 코인 심볼: 영문 대문자 + 숫자만 허용 (1000PEPE 등 가능)
_VALID_BASE_RE = re.compile(r"^[A-Z0-9]+$")


class DynamicScanner:
    """
    4단계 파이프라인으로 전체 바이낸스 선물 시장에서
    최적의 페어 트레이딩 후보를 자동 발굴합니다.

    Step 1: 거래대금 상위 우량주 필터 + 티커 위생/유동성 검사
    Step 2: 1시간봉 500개(~21일) OHLCV 데이터 수집
    Step 3: 섹터 동조화 필터 + 수익률 상관계수 >= 0.7 (양의 상관만)
    Step 4: 로그가격 공적분 검정 p <= 0.01 오디션 + 코인 중복 제한
    Step 5: 생존 필터 — N회 연속 스캔에서 발견된 페어만 최종 채택
    """

    def __init__(self):
        # 생존(지속성) 필터: {pair_key: 연속 발견 횟수}
        # 진짜 공적분 관계는 스캔을 거듭해도 유지된다는 전제
        self._survival_streaks: Dict[str, int] = {}

    # ── 메인 진입점 ───────────────────────────────────────────────────────────

    async def run_full_scan(self, exchange) -> List[dict]:
        """
        파이프라인을 순차 실행하여 최종 Top N 페어를 반환합니다.

        반환: [{pair, sym_a, sym_b, corr, coint_pvalue, score, data_points,
                survival_count}, ...]
              score 내림차순 정렬, 빈 리스트면 유효 페어 없음
              (생존 필터로 SCANNER_MIN_SURVIVAL_SCANS회 연속 발견 페어만 포함)
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
        candidates = await asyncio.to_thread(self._blocking_scan, prices)
        logger.info(
            f"[DynScanner] Step 3+4 완료 | "
            f"통계 필터 통과 {len(candidates)}개 페어"
        )

        # ── Step 5: 생존(지속성) 필터 ──
        # 이번 스캔 후보의 streak 갱신, 미발견 페어 streak 초기화
        current_keys = {c["pair"] for c in candidates}
        new_streaks: Dict[str, int] = {}
        for key in current_keys:
            new_streaks[key] = self._survival_streaks.get(key, 0) + 1
        self._survival_streaks = new_streaks

        survivors = []
        for c in candidates:
            streak = self._survival_streaks[c["pair"]]
            c["survival_count"] = streak
            if streak >= SCANNER_MIN_SURVIVAL_SCANS:
                survivors.append(c)

        rookie_count = len(candidates) - len(survivors)
        logger.info(
            f"[DynScanner] Step 5 완료 | 생존 필터"
            f"(>= {SCANNER_MIN_SURVIVAL_SCANS}회 연속) 통과: {len(survivors)}개 | "
            f"검증 대기(첫 발견): {rookie_count}개"
        )

        # ── Step 6: 코인 분산 선발 (생존자 대상) ──
        results = self._apply_diversity_filter(survivors)
        return results

    # ── Step 1: 거래대금 상위 코인 필터 ───────────────────────────────────────

    async def _fetch_top_coins(
        self, exchange, count: int = 60
    ) -> List[str]:
        """
        바이낸스 선물의 전체 티커를 조회하여
        24시간 거래대금(quoteVolume) 상위 N개 코인의 base 심볼을 반환합니다.
        스테이블코인, 레버리지 토큰, 주식/ETF, 특수문자 티커 등을 제외합니다.
        """
        tickers = None
        for attempt in range(3):
            try:
                await exchange.load_markets()
                tickers = await exchange.fetch_tickers()
                break
            except Exception as e:
                logger.warning(f"[DynScanner] 전체 티커 조회 실패 (시도 {attempt+1}/3): {e}")
                await asyncio.sleep(2)
                
        if not tickers:
            logger.error("[DynScanner] 전체 티커 조회 최종 실패")
            return []

        # USDT 페어만 추출 + 거래대금 기준 정렬
        candidates = []
        hygiene_rejected = 0
        for symbol, ticker in tickers.items():
            # 선물 심볼 형식: BTC/USDT:USDT
            if not symbol.endswith("/USDT:USDT"):
                continue

            base = symbol.split("/")[0]

            # 위생 필터 1: 기초 자산(Underlying Type)이 COIN(암호화폐)인지 확인 (주식/원자재 원천 차단)
            market_info = exchange.markets.get(symbol, {})
            underlying_type = market_info.get("info", {}).get("underlyingType", "COIN")
            if underlying_type != "COIN":
                logger.debug(f"[DynScanner] 비코인 자산(주식/원자재 등) 차단: {symbol} ({underlying_type})")
                continue

            # 위생 필터 2: 영문 대문자 + 숫자만 허용 (하이픈, 밑줄, 한자, 공백 등 차단)
            if not _VALID_BASE_RE.match(base):
                hygiene_rejected += 1
                continue

            # 위생 필터 3: 블랙리스트 (스테이블, 테스트넷 토큰 등)
            if base in SCANNER_EXCLUDE_COINS:
                continue

            # 위생 필터 4: 레버리지 토큰 접미사
            if any(base.endswith(suffix) for suffix in
                   ("UP", "DOWN", "BULL", "BEAR")):
                continue

            # 유동성 필터 1: 24h 거래대금 하한 (저유동성 = 슬리피지로 기대수익 잠식)
            quote_vol = float(ticker.get("quoteVolume", 0) or 0)
            if quote_vol < SCANNER_MIN_QUOTE_VOLUME:
                continue

            # 유동성 필터 2: 호가 스프레드 상한 (bp)
            # bid/ask가 티커에 없으면 통과시키되, 있으면 엄격 검사
            bid = float(ticker.get("bid", 0) or 0)
            ask = float(ticker.get("ask", 0) or 0)
            if bid > 0 and ask > bid:
                spread_bp = (ask - bid) / ((ask + bid) / 2) * 10_000
                if spread_bp > SCANNER_MAX_SPREAD_BP:
                    logger.debug(
                        f"[DynScanner] 스프레드 과대 차단: {base} "
                        f"({spread_bp:.1f}bp > {SCANNER_MAX_SPREAD_BP}bp)"
                    )
                    continue

            candidates.append((base, quote_vol))

        if hygiene_rejected > 0:
            logger.debug(
                f"[DynScanner] 위생 필터로 {hygiene_rejected}개 비정상 티커 제거"
            )

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
                    if ohlcv and len(ohlcv) >= SCANNER_MIN_BARS:
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

        coins = list(prices.keys())
        total_combos = len(coins) * (len(coins) - 1) // 2
        logger.info(
            f"[DynScanner] Step 3 시작 | "
            f"{len(coins)}개 코인 × {total_combos}개 조합"
        )

        # ── Step 3: 섹터 동조화 필터 + 수익률 상관계수 고속 필터 ──
        # 허용 조건: (1) 같은 "분류된" 섹터끼리 OR (2) 한쪽이 Macro(BTC/ETH)
        # ※ 미분류 코인(ETC)끼리는 차단 — ETC==ETC로 통과하던 구멍 봉쇄
        # ※ 상관계수는 가격 레벨이 아닌 "로그 수익률"로 계산 (레벨 상관은 추세 오염)
        # ※ 양의 상관만 허용 — 역상관 페어는 스마트 손절(corr 손절)과 모순되어 자동 손실
        corr_passed = []
        sector_skipped = 0
        for coin_a, coin_b in itertools.combinations(coins, 2):
            # 섹터 동조화 필터 (이종 교배 차단)
            sec_a = SECTOR_MAP.get(coin_a, "ETC")
            sec_b = SECTOR_MAP.get(coin_b, "ETC")
            same_sector = (sec_a == sec_b) and (sec_a != "ETC")
            has_macro = (sec_a == "Macro" or sec_b == "Macro")
            if not same_sector and not has_macro:
                sector_skipped += 1
                continue

            arr_a = prices[coin_a]
            arr_b = prices[coin_b]

            min_len = min(len(arr_a), len(arr_b))
            if min_len < SCANNER_MIN_BARS:
                continue
            a = arr_a[-min_len:]
            b = arr_b[-min_len:]
            if np.any(a <= 0) or np.any(b <= 0):
                continue

            # 로그 수익률 상관계수 (양의 상관만)
            ret_a = np.diff(np.log(a))
            ret_b = np.diff(np.log(b))
            corr = float(np.corrcoef(ret_a, ret_b)[0, 1])
            if corr >= SCANNER_CORR_THRESHOLD:
                corr_passed.append((coin_a, coin_b, a, b, corr))

        logger.info(
            f"[DynScanner] Step 3 완료 | "
            f"섹터 필터로 {sector_skipped}개 이종 조합 차단 | "
            f"수익률 상관계수 >= {SCANNER_CORR_THRESHOLD} 통과: "
            f"{len(corr_passed)}개 페어"
        )

        if not corr_passed:
            return []

        # ── Step 4: 공적분 오디션 (로그 가격 기준) ──
        coint_candidates = []
        for coin_a, coin_b, a, b, corr in corr_passed:
            try:
                _, pvalue, _ = coint(np.log(a), np.log(b))
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
            f"[DynScanner] Step 4 완료 | "
            f"공적분 p<={SCANNER_COINT_PVALUE} 통과: "
            f"{len(coint_candidates)}개 후보"
        )
        # 코인 분산 선발은 생존 필터 이후(run_full_scan)에서 수행
        return coint_candidates

    # ── 코인 중복 출현 제한 (포트폴리오 분산) ────────────────────────────────

    @staticmethod
    def _apply_diversity_filter(candidates: List[dict]) -> List[dict]:
        """
        score 순서대로 뽑되, 한 코인이 MAX_PAIRS_PER_COIN 초과하면 Skip.
        내부용 키(coin_a, coin_b)를 제거한 최종 리스트를 반환합니다.
        """
        from collections import defaultdict

        coin_count: Dict[str, int] = defaultdict(int)
        results = []
        skipped_by_limit = 0

        for cand in candidates:
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
            f"[DynScanner] 분산 필터(MAX={MAX_PAIRS_PER_COIN}) | "
            f"{skipped_by_limit}개 Skip | 최종 {len(results)}개 선발"
        )
        return results
