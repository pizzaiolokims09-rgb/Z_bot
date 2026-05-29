# =============================================================================
# pair_bot/main.py
# 페어 트레이딩 봇 — 메인 진입점
# 실행: python main.py
# =============================================================================

import asyncio
import logging
import sys
import time
from logging.handlers import RotatingFileHandler

import ccxt.async_support as ccxt_async

from config import (
    API_KEY, API_SECRET, IS_PAPER_TRADING,
    SYMBOL_A, SYMBOL_B,
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
# 가격 조회 (ccxt async — 공개 REST)
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
# 메인 루프
# ─────────────────────────────────────────────────────────────────────────────
async def main_loop():
    logger = logging.getLogger("pair_bot")

    mode_str = "PAPER TRADING (가상 시뮬레이션)" if IS_PAPER_TRADING else "LIVE TRADING (실계좌)"
    logger.info("=" * 60)
    logger.info(f"  페어 트레이딩 봇 시작 — 모드: {mode_str}")
    logger.info(f"  대상 페어: {SYMBOL_A}  /  {SYMBOL_B}")
    logger.info("=" * 60)

    # ccxt 비동기 교환소 (가격 조회용 — 공개 API, 키 불필요)
    exchange = ccxt_async.binanceusdm({"options": {"defaultType": "future"}})

    spread_engine  = SpreadEngine()
    risk_manager   = RiskManager()
    order_executor = OrderExecutor()

    reconnect_wait = 5  # 재연결 대기 초

    while True:
        try:
            # ── 1. 가격 조회 ────────────────────────────────────────────────
            price_a, price_b = await asyncio.gather(
                fetch_mid_price(exchange, SYMBOL_A),
                fetch_mid_price(exchange, SYMBOL_B),
            )

            import math
            if math.isnan(price_a) or math.isnan(price_b) or price_b == 0:
                logger.warning("[스킵] 가격 조회 실패 — 다음 사이클 대기")
                await asyncio.sleep(POLL_INTERVAL_SEC)
                continue

            # ── 2. 스프레드 계산 ─────────────────────────────────────────────
            state  = spread_engine.update(price_a, price_b)
            signal = state["signal"]
            dev    = state["dev_pct"]
            ratio  = state["ratio"]
            window = spread_engine.window_size

            # 주기적 상태 로그 (10초마다)
            if int(time.time()) % 10 == 0:
                pnl_est = risk_manager.estimate_pnl_pct(ratio) if risk_manager.has_position else 0.0
                logger.debug(
                    f"[상태] ratio={ratio:.6f} | dev={dev:+.3f}% | "
                    f"z={state['z_score']:+.3f} | window={window} | "
                    f"pos={'O' if risk_manager.has_position else '-'} | "
                    f"추정PnL={pnl_est:+.3f}%"
                )

            # ── 3. 신호 처리 ─────────────────────────────────────────────────
            sizing = risk_manager.calc_qty(price_a, price_b)

            # 포지션 없을 때만 진입
            if not risk_manager.has_position:
                if signal == "ENTRY_SHORT_A_LONG_B":
                    logger.info(f"[진입신호] A({SYMBOL_A}) Short, B({SYMBOL_B}) Long | dev={dev:+.3f}%")
                    await order_executor.open_pair(
                        side_a="sell", qty_a=sizing["qty_a"], price_a=price_a,
                        side_b="buy",  qty_b=sizing["qty_b"], price_b=price_b,
                    )
                    risk_manager.open_position("SHORT_A_LONG_B", ratio)

                elif signal == "ENTRY_LONG_A_SHORT_B":
                    logger.info(f"[진입신호] A({SYMBOL_A}) Long, B({SYMBOL_B}) Short | dev={dev:+.3f}%")
                    await order_executor.open_pair(
                        side_a="buy",  qty_a=sizing["qty_a"], price_a=price_a,
                        side_b="sell", qty_b=sizing["qty_b"], price_b=price_b,
                    )
                    risk_manager.open_position("LONG_A_SHORT_B", ratio)

            # 포지션 있을 때 청산/손절 판단
            else:
                if signal == "STOP" or risk_manager.should_stop_loss(dev):
                    logger.warning(f"[손절발동] dev={dev:+.3f}% ≥ 임계값 — 즉시 전량 청산")
                    await order_executor.close_pair(price_a, price_b, reason="STOP_LOSS")
                    risk_manager.close_position()

                elif signal == "EXIT":
                    pnl_est = risk_manager.estimate_pnl_pct(ratio)
                    logger.info(f"[익절발동] 괴리 회귀 | dev={dev:+.3f}% | 추정PnL={pnl_est:+.3f}%")
                    await order_executor.close_pair(price_a, price_b, reason="TAKE_PROFIT")
                    risk_manager.close_position()

            # ── 4. 다음 사이클 대기 ──────────────────────────────────────────
            await asyncio.sleep(POLL_INTERVAL_SEC)

        except ccxt_async.NetworkError as e:
            logger.error(f"[네트워크 단절] {e} — {reconnect_wait}초 후 재연결")
            await asyncio.sleep(reconnect_wait)

        except ccxt_async.RateLimitExceeded as e:
            logger.warning(f"[레이트 리밋] {e} — 10초 대기")
            await asyncio.sleep(10)

        except KeyboardInterrupt:
            logger.info("[종료] 사용자 인터럽트 — 봇 종료")
            break

        except Exception as e:
            logger.error(f"[예상치 못한 오류] {e}", exc_info=True)
            await asyncio.sleep(reconnect_wait)

    await exchange.close()
    logger.info("교환소 연결 종료. 봇을 정상적으로 종료합니다.")


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    setup_logger()
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass
