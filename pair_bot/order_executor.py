# =============================================================================
# pair_bot/order_executor.py
# 실주문(Binance Futures) / 페이퍼 트레이딩 주문 집행기
# IS_PAPER_TRADING 플래그로 모드 전환
# =============================================================================

import asyncio
import logging
import time
from config import (
    API_KEY, API_SECRET, IS_PAPER_TRADING,
    SYMBOL_A, SYMBOL_B, LEVERAGE,
    ORDER_RETRY_COUNT, ORDER_RETRY_WAIT,
)

logger = logging.getLogger("pair_bot")


# ─────────────────────────────────────────────────────────────────────────────
# 페이퍼 트레이딩 가상 계좌
# ─────────────────────────────────────────────────────────────────────────────
class PaperAccount:
    """가상 잔고 및 포지션을 추적하는 간단한 시뮬레이터."""

    def __init__(self, initial_balance: float = 1000.0):
        self.balance    = initial_balance
        self.positions  = {}   # symbol → {"side": str, "qty": float, "entry_price": float}
        self.trade_log  = []

    def open_order(self, symbol: str, side: str, qty: float, price: float):
        cost = qty * price
        self.positions[symbol] = {"side": side, "qty": qty, "entry_price": price}
        logger.info(f"[PAPER] {side} {qty:.4f} {symbol} @ {price:.4f} USDT | 명목가치={cost:.2f}")

    def close_order(self, symbol: str, price: float) -> float:
        """포지션 청산 후 손익(USDT)을 반환합니다."""
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return 0.0
        entry = pos["entry_price"]
        qty   = pos["qty"]
        if pos["side"] == "buy":
            pnl = (price - entry) * qty
        else:
            pnl = (entry - price) * qty
        self.balance += pnl
        logger.info(
            f"[PAPER] CLOSE {symbol} @ {price:.4f} | "
            f"진입={entry:.4f}, 수량={qty:.4f}, PnL={pnl:+.4f} USDT"
        )
        return pnl


# ─────────────────────────────────────────────────────────────────────────────
# 주문 집행기 (실거래 + 페이퍼)
# ─────────────────────────────────────────────────────────────────────────────
class OrderExecutor:

    def __init__(self):
        self._paper = PaperAccount()
        self._exchange = None   # ccxt 교환소 인스턴스 (실거래 시 초기화)

        if not IS_PAPER_TRADING:
            self._init_exchange()

    def _init_exchange(self):
        """ccxt binanceusdm 교환소를 초기화하고 레버리지를 설정합니다."""
        try:
            import ccxt
            self._exchange = ccxt.binanceusdm({
                "apiKey"   : API_KEY,
                "secret"   : API_SECRET,
                "options"  : {"defaultType": "future"},
                "enableRateLimit": True,
            })
            # 레버리지 사전 설정
            for sym in (SYMBOL_A, SYMBOL_B):
                self._exchange.set_leverage(LEVERAGE, sym)
            logger.info(f"[실거래] 바이낸스 선물 연결 완료. 레버리지={LEVERAGE}x")
        except Exception as e:
            logger.error(f"[실거래] 교환소 초기화 실패: {e}")
            raise

    # ── 공개 API ──────────────────────────────────────────────────────────────

    async def open_pair(
        self,
        side_a: str, qty_a: float, price_a: float,
        side_b: str, qty_b: float, price_b: float,
    ):
        """
        두 레그를 동시에(asyncio.gather) 진입 주문합니다.
        side: "buy" | "sell"
        """
        logger.info(
            f"[주문진입] A={SYMBOL_A} {side_a} {qty_a:.4f} @ ~{price_a:.4f} | "
            f"B={SYMBOL_B} {side_b} {qty_b:.4f} @ ~{price_b:.4f}"
        )
        await asyncio.gather(
            self._place_order(SYMBOL_A, side_a, qty_a, price_a),
            self._place_order(SYMBOL_B, side_b, qty_b, price_b),
        )

    async def close_pair(self, price_a: float, price_b: float, reason: str = "EXIT"):
        """두 레그를 동시에 청산합니다."""
        logger.info(f"[주문청산] reason={reason} | A~{price_a:.4f}, B~{price_b:.4f}")

        if IS_PAPER_TRADING:
            pnl_a = self._paper.close_order(SYMBOL_A, price_a)
            pnl_b = self._paper.close_order(SYMBOL_B, price_b)
            total_pnl = pnl_a + pnl_b
            logger.info(
                f"[PAPER] 청산완료 | reason={reason} | "
                f"총 PnL={total_pnl:+.4f} USDT | 잔고={self._paper.balance:.2f} USDT"
            )
            return

        # 실거래: 현재 포지션을 반전시켜 청산
        await asyncio.gather(
            self._close_real_position(SYMBOL_A),
            self._close_real_position(SYMBOL_B),
        )

    # ── 내부 주문 처리 ────────────────────────────────────────────────────────

    async def _place_order(self, symbol: str, side: str, qty: float, price: float):
        """재시도 로직이 포함된 단일 주문 집행."""
        for attempt in range(1, ORDER_RETRY_COUNT + 1):
            try:
                if IS_PAPER_TRADING:
                    self._paper.open_order(symbol, side, qty, price)
                    return
                # 실거래: 시장가 주문
                order = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._exchange.create_order(
                        symbol, "market", side, qty
                    )
                )
                logger.info(f"[실거래] 주문완료 id={order['id']} | {symbol} {side} {qty:.4f}")
                return

            except Exception as e:
                logger.warning(f"[주문실패 {attempt}/{ORDER_RETRY_COUNT}] {symbol}: {e}")
                if attempt < ORDER_RETRY_COUNT:
                    await asyncio.sleep(ORDER_RETRY_WAIT)
                else:
                    logger.error(f"[주문포기] {symbol} — 재시도 횟수 초과")
                    raise

    async def _close_real_position(self, symbol: str):
        """실거래 포지션 전량 청산 (reduceOnly)."""
        for attempt in range(1, ORDER_RETRY_COUNT + 1):
            try:
                pos = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._exchange.fetch_position(symbol)
                )
                contracts = abs(float(pos.get("contracts", 0)))
                if contracts == 0:
                    logger.info(f"[청산] {symbol} 보유 포지션 없음, 스킵")
                    return
                close_side = "sell" if float(pos.get("contracts", 0)) > 0 else "buy"
                order = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._exchange.create_order(
                        symbol, "market", close_side, contracts,
                        {"reduceOnly": True}
                    )
                )
                logger.info(f"[실거래] 청산완료 id={order['id']} | {symbol}")
                return
            except Exception as e:
                logger.warning(f"[청산실패 {attempt}/{ORDER_RETRY_COUNT}] {symbol}: {e}")
                if attempt < ORDER_RETRY_COUNT:
                    await asyncio.sleep(ORDER_RETRY_WAIT)
                else:
                    logger.error(f"[청산포기] {symbol} — 재시도 횟수 초과")
                    raise
