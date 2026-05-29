# =============================================================================
# pair_bot/main.py
# 페어 트레이딩 봇 — 메인 진입점 (5개 페어 병렬 감시)
# 실행: python main.py
# =============================================================================

import asyncio
import logging
import math
import sys
import time
from logging.handlers import RotatingFileHandler

import ccxt.async_support as ccxt_async

from config import (
    IS_PAPER_TRADING, PAIRS_TO_TRADE,
    ALLOCATION_PER_PAIR,
    POLL_INTERVAL_SEC, LOG_FILE,
)
from spread_engine  import SpreadEngine
from risk_manager   import RiskManager
from order_executor import OrderExecutor


# ─────────────────────────────────────────────────────────────────────────────
# 로거 초기화
# ─────────────────────────────────────────────────────────────────────────────
def setup_logger() -> logging.Logger:
    logger = logging.getLogger("pair_bot")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 콘솔 출력 (Windows CP949 환경에서도 한글/유니코드 깨짐 방지)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    # 파일 출력 (최대 10MB, 3개 롤링)
    fh = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger


# ─────────────────────────────────────────────────────────────────────────────
# 심볼 변환 유틸
# ─────────────────────────────────────────────────────────────────────────────
def to_futures_symbol(sym: str) -> str:
    """BTC/USDT → BTC/USDT:USDT"""
    return sym + ":USDT" if ":" not in sym else sym


def make_prefix(sym_a: str, sym_b: str) -> str:
    """BTC/USDT, ETH/USDT → BTC-ETH (로그 접두어)"""
    return f"{sym_a.split('/')[0]}-{sym_b.split('/')[0]}"


# ─────────────────────────────────────────────────────────────────────────────
# 가격 조회 (ccxt async — 공개 REST, 키 불필요)
# ─────────────────────────────────────────────────────────────────────────────
async def fetch_mid_price(exchange: ccxt_async.Exchange, symbol: str) -> float:
    """
    오더북 최우선 매도/매수 호가의 중간값(Mid Price)을 반환합니다.
    실패 시 NaN 반환.
    """
    try:
        ob = await exchange.fetch_order_book(symbol, limit=5)
        best_bid = ob["bids"][0][0] if ob["bids"] else None
        best_ask = ob["asks"][0][0] if ob["asks"] else None
        if best_bid and best_ask:
            return (best_bid + best_ask) / 2.0
    except Exception as e:
        logging.getLogger("pair_bot").warning(f"[가격조회 실패] {symbol}: {e}")
    return float("nan")


# ─────────────────────────────────────────────────────────────────────────────
# 단일 페어 감시 루프 (Z-Score/진입/청산 핵심 로직 유지)
# ─────────────────────────────────────────────────────────────────────────────
async def pair_loop(
    raw_sym_a: str,
    raw_sym_b: str,
    exchange: ccxt_async.Exchange,
    order_executor: OrderExecutor,
):
    """
    한 페어에 대한 독립적인 감시/매매 루프.
    SpreadEngine, RiskManager 인스턴스가 페어별로 완전히 분리됩니다.
    """
    logger = logging.getLogger("pair_bot")
    sym_a  = to_futures_symbol(raw_sym_a)
    sym_b  = to_futures_symbol(raw_sym_b)
    prefix = make_prefix(raw_sym_a, raw_sym_b)

    spread_engine = SpreadEngine()   # 이 페어 전용 Z-Score 계산기
    risk_manager  = RiskManager()    # 이 페어 전용 포지션 상태

    reconnect_wait = 5

    while True:
        try:
            # ── 1. 가격 조회 ────────────────────────────────────────────────
            price_a, price_b = await asyncio.gather(
                fetch_mid_price(exchange, sym_a),
                fetch_mid_price(exchange, sym_b),
            )

            if math.isnan(price_a) or math.isnan(price_b) or price_b == 0:
                logger.warning(f"[{prefix}] 가격 조회 실패 — 다음 사이클 대기")
                await asyncio.sleep(POLL_INTERVAL_SEC)
                continue

            # ── 2. 스프레드 계산 (이동평균 / Z-Score — 기존 로직 유지) ────────
            state  = spread_engine.update(price_a, price_b)
            signal = state["signal"]
            dev    = state["dev_pct"]
            ratio  = state["ratio"]
            window = spread_engine.window_size

            # 주기적 상태 로그 (10초마다 DEBUG 레벨로 bot.log에 기록)
            if int(time.time()) % 10 == 0:
                pnl_est = risk_manager.estimate_pnl_pct(ratio) if risk_manager.has_position else 0.0
                logger.debug(
                    f"[{prefix}] ratio={ratio:.6f} | dev={dev:+.3f}% | "
                    f"z={state['z_score']:+.3f} | window={window} | "
                    f"pos={'O' if risk_manager.has_position else '-'} | "
                    f"추정PnL={pnl_est:+.3f}%"
                )

            # ── 3. 동적 포지션 사이징 — 가용 잔고의 ALLOCATION_PER_PAIR% ──────
            # 진입 신호가 있을 때만 잔고 조회 (불필요한 API 호출 최소화)
            trade_usdt = 0.0
            if not risk_manager.has_position and signal.startswith("ENTRY"):
                free_bal   = await order_executor.get_free_balance()
                # 페어 총 배분 = 잔고 x 10%, 각 레그(롱/숏) = 그 절반
                trade_usdt = (free_bal * ALLOCATION_PER_PAIR) / 2.0
                logger.info(
                    f"[{prefix}] 잔고={free_bal:.2f} USDT | "
                    f"레그당={trade_usdt:.2f} USDT (배분={ALLOCATION_PER_PAIR * 100:.0f}%)"
                )

            # ── 4. 신호 처리 (기존 진입/청산 조건 로직 유지) ─────────────────

            if not risk_manager.has_position:
                if signal == "ENTRY_SHORT_A_LONG_B" and trade_usdt > 0:
                    sizing = risk_manager.calc_qty(price_a, price_b, trade_usdt)
                    logger.info(f"[{prefix}] 진입 시그널 발생 | A Short / B Long | dev={dev:+.3f}%")
                    await order_executor.open_pair(
                        sym_a=sym_a, side_a="sell", qty_a=sizing["qty_a"], price_a=price_a,
                        sym_b=sym_b, side_b="buy",  qty_b=sizing["qty_b"], price_b=price_b,
                        pair_prefix=prefix,
                    )
                    risk_manager.open_position("SHORT_A_LONG_B", ratio, trade_usdt, prefix)

                elif signal == "ENTRY_LONG_A_SHORT_B" and trade_usdt > 0:
                    sizing = risk_manager.calc_qty(price_a, price_b, trade_usdt)
                    logger.info(f"[{prefix}] 진입 시그널 발생 | A Long / B Short | dev={dev:+.3f}%")
                    await order_executor.open_pair(
                        sym_a=sym_a, side_a="buy",  qty_a=sizing["qty_a"], price_a=price_a,
                        sym_b=sym_b, side_b="sell", qty_b=sizing["qty_b"], price_b=price_b,
                        pair_prefix=prefix,
                    )
                    risk_manager.open_position("LONG_A_SHORT_B", ratio, trade_usdt, prefix)

            else:
                if signal == "STOP" or risk_manager.should_stop_loss(dev):
                    logger.warning(f"[{prefix}] 손절 발동 | dev={dev:+.3f}% >= 임계값 — 전량 청산")
                    await order_executor.close_pair(
                        sym_a=sym_a, price_a=price_a,
                        sym_b=sym_b, price_b=price_b,
                        reason="STOP_LOSS", pair_prefix=prefix,
                    )
                    risk_manager.close_position(prefix)

                elif signal == "EXIT":
                    pnl_est = risk_manager.estimate_pnl_pct(ratio)
                    logger.info(f"[{prefix}] 익절 발동 | 괴리 회귀 | dev={dev:+.3f}% | 추정PnL={pnl_est:+.3f}%")
                    await order_executor.close_pair(
                        sym_a=sym_a, price_a=price_a,
                        sym_b=sym_b, price_b=price_b,
                        reason="TAKE_PROFIT", pair_prefix=prefix,
                    )
                    risk_manager.close_position(prefix)

            # ── 5. 다음 사이클 대기 ──────────────────────────────────────────
            await asyncio.sleep(POLL_INTERVAL_SEC)

        except ccxt_async.NetworkError as e:
            logger.error(f"[{prefix}] 네트워크 단절: {e} — {reconnect_wait}초 후 재연결")
            await asyncio.sleep(reconnect_wait)

        except ccxt_async.RateLimitExceeded as e:
            logger.warning(f"[{prefix}] 레이트 리밋: {e} — 10초 대기")
            await asyncio.sleep(10)

        except Exception as e:
            logger.error(f"[{prefix}] 예상치 못한 오류: {e}", exc_info=True)
            await asyncio.sleep(reconnect_wait)


# ─────────────────────────────────────────────────────────────────────────────
# 메인 루프 — 5개 페어 병렬 실행
# ─────────────────────────────────────────────────────────────────────────────
async def main_loop():
    logger = logging.getLogger("pair_bot")

    mode_str = "PAPER TRADING (가상 시뮬레이션)" if IS_PAPER_TRADING else "LIVE TRADING (실계좌)"
    logger.info("=" * 60)
    logger.info(f"  페어 트레이딩 봇 시작 — 모드: {mode_str}")
    logger.info(f"  감시 페어 수: {len(PAIRS_TO_TRADE)}개")
    for sym_a, sym_b in PAIRS_TO_TRADE:
        logger.info(f"    [{make_prefix(sym_a, sym_b):10s}]  {sym_a}  /  {sym_b}")
    logger.info(f"  페어당 배분 비율: {ALLOCATION_PER_PAIR * 100:.0f}%")
    logger.info("=" * 60)

    # ccxt 비동기 교환소 (가격 조회용 — 공개 API, 키 불필요)
    exchange       = ccxt_async.binanceusdm({"options": {"defaultType": "future"}})
    order_executor = OrderExecutor()

    # 5개 페어 루프를 asyncio.gather로 동시 병렬 실행
    tasks = [
        pair_loop(sym_a, sym_b, exchange, order_executor)
        for sym_a, sym_b in PAIRS_TO_TRADE
    ]

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        pass
    finally:
        await exchange.close()
        logger.info("교환소 연결 종료. 봇을 정상적으로 종료합니다.")


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    setup_logger()
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass
