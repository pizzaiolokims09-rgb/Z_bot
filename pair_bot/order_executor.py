# =============================================================================
# pair_bot/order_executor.py
# 실주문(Binance Futures) / 페이퍼 트레이딩 주문 집행기
# IS_PAPER_TRADING 플래그로 모드 전환
# 다중 페어 지원: 심볼을 인자로 전달받아 어느 페어든 처리 가능
# =============================================================================

import asyncio
import logging
from config import (
    API_KEY, API_SECRET, IS_PAPER_TRADING, USE_TESTNET,
    PAIRS_TO_TRADE, LEVERAGE,
    ORDER_RETRY_COUNT, ORDER_RETRY_WAIT,
    PAPER_INITIAL_BALANCE, ALLOCATION_PER_PAIR,
)

logger = logging.getLogger("pair_bot")


# ─────────────────────────────────────────────────────────────────────────────
# 페이퍼 트레이딩 가상 계좌
# ─────────────────────────────────────────────────────────────────────────────
class PaperAccount:
    """가상 잔고 및 포지션을 추적하는 간단한 시뮬레이터."""

    def __init__(self, initial_balance: float = PAPER_INITIAL_BALANCE):
        self.balance   = initial_balance
        self.positions = {}   # symbol → {"side": str, "qty": float, "entry_price": float}

    @property
    def free_balance(self) -> float:
        """현재 가용 잔고 반환 (진입 중인 포지션 증거금 차감 없이 단순화)."""
        return self.balance

    def open_order(self, symbol: str, side: str, qty: float, price: float, pair_prefix: str = ""):
        cost = qty * price
        self.positions[symbol] = {"side": side, "qty": qty, "entry_price": price}
        logger.info(f"[{pair_prefix}] [PAPER] {side} {qty:.4f} {symbol} @ {price:.4f} | 명목={cost:.2f} USDT")

    def close_order(self, symbol: str, price: float, pair_prefix: str = "") -> float:
        """포지션 청산 후 손익(USDT)을 반환합니다."""
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return 0.0
        entry = pos["entry_price"]
        qty   = pos["qty"]
        pnl   = (price - entry) * qty if pos["side"] == "buy" else (entry - price) * qty
        self.balance += pnl
        logger.info(
            f"[{pair_prefix}] [PAPER] CLOSE {symbol} @ {price:.4f} | "
            f"진입={entry:.4f}, 수량={qty:.4f}, PnL={pnl:+.4f} USDT"
        )
        return pnl


# ─────────────────────────────────────────────────────────────────────────────
# 주문 집행기 (실거래 + 페이퍼) — 다중 페어 지원
# ─────────────────────────────────────────────────────────────────────────────
class OrderExecutor:

    def __init__(self):
        self._paper    = PaperAccount()
        self._exchange = None   # ccxt 교환소 인스턴스 (실거래 시 초기화)

        if not IS_PAPER_TRADING:
            self._init_exchange()

    # ── 교환소 초기화 ─────────────────────────────────────────────────────────

    def _init_exchange(self):
        """ccxt binanceusdm 교환소를 초기화하고 모든 페어의 레버리지를 설정합니다."""
        try:
            import ccxt
            self._exchange = ccxt.binanceusdm({
                "apiKey"         : API_KEY,
                "secret"         : API_SECRET,
                "options"        : {"defaultType": "future"},
                "enableRateLimit": True,
            })
            
            if USE_TESTNET:
                self._exchange.set_sandbox_mode(True)
                logger.info("[실거래] 바이낸스 모의투자(Testnet) 모드 활성화")
            else:
                logger.info("[실거래] 바이낸스 실거래(Mainnet) 모드 활성화")

            for sym_a, sym_b in PAIRS_TO_TRADE:
                for sym in (sym_a + ":USDT", sym_b + ":USDT"):
                    try:
                        self._exchange.set_leverage(LEVERAGE, sym)
                    except Exception as e:
                        logger.warning(f"[레버리지 설정 실패] {sym}: {e}")
            logger.info(f"[실거래] 바이낸스 선물 연결 완료. 레버리지={LEVERAGE}x")
        except Exception as e:
            logger.error(f"[실거래] 교환소 초기화 실패: {e}")
            raise

    # ── 잔고 조회 ─────────────────────────────────────────────────────────────

    async def get_free_balance(self) -> float:
        """
        가용 USDT 잔고를 조회합니다.
        페이퍼 모드: 가상 계좌 잔고 반환
        실거래 모드: 바이낸스 선물 Free USDT 조회
        """
        if IS_PAPER_TRADING:
            return self._paper.free_balance

        try:
            bal = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._exchange.fetch_balance()
            )
            return float(bal.get("free", {}).get("USDT", 0.0))
        except Exception as e:
            logger.warning(f"[잔고조회 실패] {e} — 0 반환")
            return 0.0

    # ── 공개 API ──────────────────────────────────────────────────────────────

    async def open_pair(
        self,
        sym_a: str, side_a: str, qty_a: float, price_a: float,
        sym_b: str, side_b: str, qty_b: float, price_b: float,
        pair_prefix: str = "",
    ):
        """두 레그를 동시에(asyncio.gather) 진입 주문합니다."""
        logger.info(
            f"[{pair_prefix}] [주문진입] A={sym_a} {side_a} {qty_a:.4f} @ ~{price_a:.4f} | "
            f"B={sym_b} {side_b} {qty_b:.4f} @ ~{price_b:.4f}"
        )
        await asyncio.gather(
            self._place_order(sym_a, side_a, qty_a, price_a, pair_prefix),
            self._place_order(sym_b, side_b, qty_b, price_b, pair_prefix),
        )

    async def close_pair(
        self,
        sym_a: str, price_a: float,
        sym_b: str, price_b: float,
        reason: str = "EXIT",
        pair_prefix: str = "",
    ):
        """두 레그를 동시에 청산합니다."""
        logger.info(f"[{pair_prefix}] [주문청산] reason={reason} | A~{price_a:.4f}, B~{price_b:.4f}")

        if IS_PAPER_TRADING:
            pnl_a = self._paper.close_order(sym_a, price_a, pair_prefix)
            pnl_b = self._paper.close_order(sym_b, price_b, pair_prefix)
            total  = pnl_a + pnl_b
            logger.info(
                f"[{pair_prefix}] [PAPER] 청산완료 | reason={reason} | "
                f"총 PnL={total:+.4f} USDT | 잔고={self._paper.balance:.2f} USDT"
            )
            return

        await asyncio.gather(
            self._close_real_position(sym_a, pair_prefix),
            self._close_real_position(sym_b, pair_prefix),
        )

    # ── 내부 주문 처리 ────────────────────────────────────────────────────────

    async def _place_order(self, symbol: str, side: str, qty: float, price: float, pair_prefix: str = ""):
        """재시도 로직이 포함된 단일 주문 집행."""
        for attempt in range(1, ORDER_RETRY_COUNT + 1):
            try:
                if IS_PAPER_TRADING:
                    self._paper.open_order(symbol, side, qty, price, pair_prefix)
                    return
                order = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._exchange.create_order(symbol, "market", side, qty)
                )
                logger.info(f"[{pair_prefix}] [실거래] 주문완료 id={order['id']} | {symbol} {side} {qty:.4f}")
                return
            except Exception as e:
                logger.warning(f"[{pair_prefix}] [주문실패 {attempt}/{ORDER_RETRY_COUNT}] {symbol}: {e}")
                if attempt < ORDER_RETRY_COUNT:
                    await asyncio.sleep(ORDER_RETRY_WAIT)
                else:
                    logger.error(f"[{pair_prefix}] [주문포기] {symbol} — 재시도 횟수 초과")
                    raise

    async def _close_real_position(self, symbol: str, pair_prefix: str = ""):
        """실거래 포지션 전량 청산 (reduceOnly)."""
        for attempt in range(1, ORDER_RETRY_COUNT + 1):
            try:
                pos = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self._exchange.fetch_position(symbol)
                )
                contracts = abs(float(pos.get("contracts", 0)))
                if contracts == 0:
                    logger.info(f"[{pair_prefix}] [청산] {symbol} 보유 포지션 없음, 스킵")
                    return
                close_side = "sell" if float(pos.get("contracts", 0)) > 0 else "buy"
                order = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._exchange.create_order(
                        symbol, "market", close_side, contracts, {"reduceOnly": True}
                    )
                )
                logger.info(f"[{pair_prefix}] [실거래] 청산완료 id={order['id']} | {symbol}")
                return
            except Exception as e:
                logger.warning(f"[{pair_prefix}] [청산실패 {attempt}/{ORDER_RETRY_COUNT}] {symbol}: {e}")
                if attempt < ORDER_RETRY_COUNT:
                    await asyncio.sleep(ORDER_RETRY_WAIT)
                else:
                    logger.error(f"[{pair_prefix}] [청산포기] {symbol} — 재시도 횟수 초과")
                    raise
