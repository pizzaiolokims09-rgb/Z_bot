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


class LeggingError(Exception):
    """짝짝이 체결(한쪽만 성공) 시 롤백 후 발생시키는 예외."""
    pass


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
        """
        ccxt.binance 통합 클래스로 선물 교환소를 초기화합니다.
        USE_TESTNET=True 이면 enable_demo_trading(True)으로
        demo.binance.com API 서버에 접속합니다.
        """
        try:
            import ccxt

            self._exchange = ccxt.binance({
                "apiKey"         : API_KEY,
                "secret"         : API_SECRET,
                "options"        : {"defaultType": "future"},
                "enableRateLimit": True,
            })

            if USE_TESTNET:
                # CCXT 4.x 공식 메서드 — demo.binance.com 전용 URL 자동 적용
                self._exchange.enable_demo_trading(True)
                logger.info("[거래소] 데모 트레이딩 모드 활성화 (demo.binance.com)")
            else:
                logger.info("[거래소] Mainnet 실거래 모드 활성화")

            logger.info(
                f"[거래소] 바이낸스 선물({'Demo' if USE_TESTNET else 'Mainnet'}) "
                f"연결 초기화 완료"
            )
        except Exception as e:
            logger.error(f"[거래소] 초기화 실패: {e}")
            raise



    async def setup_leverage(self) -> None:
        """
        봇 시작 시 1회 호출. PAIRS_TO_TRADE 전 심볼의 레버리지를 LEVERAGE 값으로 세팅합니다.
        IS_PAPER_TRADING 모드에서는 건너뜁니다.
        """
        if IS_PAPER_TRADING:
            logger.info(f"[PAPER] 레버리지 자동 세팅 스킵 (가상 계좌)")
            return

        success, fail = 0, 0
        for sym_a, sym_b in PAIRS_TO_TRADE:
            for raw_sym in (sym_a, sym_b):
                # BTC/USDT → BTC/USDT:USDT (바이낸스 선물 표기)
                sym = raw_sym if ":" in raw_sym else raw_sym.replace("/USDT", "/USDT:USDT")
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda s=sym: self._exchange.set_leverage(LEVERAGE, s)
                    )
                    logger.info(f"[레버리지] {sym} → {LEVERAGE}x 세팅 완료")
                    success += 1
                except Exception as e:
                    logger.warning(f"[레버리지 설정 실패] {sym}: {e}")
                    fail += 1
        logger.info(
            f"[레버리지] 전체 세팅 완료 | 성공={success}개 / 실패={fail}개 | {LEVERAGE}x 적용"
        )

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
            bal = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._exchange.fetch_balance()
            )
            return float(bal.get("free", {}).get("USDT", 0.0))
        except Exception as e:
            logger.warning(f"[잔고조회 실패] {e} — 0 반환")
            return 0.0

    async def get_total_balance(self) -> float:
        """
        초기 자본 대비 손실률(킬 스위치) 계산용 총 USDT 잔고 (사용 중인 증거금 포함)
        """
        if IS_PAPER_TRADING:
            return self._paper.free_balance

        try:
            bal = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._exchange.fetch_balance()
            )
            return float(bal.get("total", {}).get("USDT", 0.0))
        except Exception as e:
            logger.warning(f"[총잔고조회 실패] {e} — 일시적 통신 오류이므로 킬스위치 판정을 보류합니다.")
            return None

    # ── 공개 API ──────────────────────────────────────────────────────────────

    async def open_pair(
        self,
        sym_a: str, side_a: str, qty_a: float, price_a: float,
        sym_b: str, side_b: str, qty_b: float, price_b: float,
        pair_prefix: str = "",
    ):
        """
        두 레그를 동시에 진입 주문합니다.
        한쪽만 성공(짝짝이 체결) 시 성공한 레그를 즉시 롤백하고 LeggingError를 raise합니다.
        """
        logger.info(
            f"[{pair_prefix}] [주문진입] A={sym_a} {side_a} {qty_a:.4f} @ ~{price_a:.4f} | "
            f"B={sym_b} {side_b} {qty_b:.4f} @ ~{price_b:.4f}"
        )

        results = await asyncio.gather(
            self._place_order(sym_a, side_a, qty_a, price_a, pair_prefix),
            self._place_order(sym_b, side_b, qty_b, price_b, pair_prefix),
            return_exceptions=True,
        )

        a_ok = not isinstance(results[0], Exception)
        b_ok = not isinstance(results[1], Exception)

        if a_ok and b_ok:
            return  # 양쪽 모두 성공

        if a_ok and not b_ok:
            # A만 성공, B 실패 → A 롤백
            logger.error(
                f"[{pair_prefix}] [Legging Risk] B 주문 실패! A 포지션 즉시 롤백 | "
                f"B 에러: {results[1]}"
            )
            await self._rollback_leg(sym_a, side_a, qty_a, price_a, pair_prefix)
            raise LeggingError(f"[{pair_prefix}] B 주문 실패 → A 롤백 완료")

        if not a_ok and b_ok:
            # B만 성공, A 실패 → B 롤백
            logger.error(
                f"[{pair_prefix}] [Legging Risk] A 주문 실패! B 포지션 즉시 롤백 | "
                f"A 에러: {results[0]}"
            )
            await self._rollback_leg(sym_b, side_b, qty_b, price_b, pair_prefix)
            raise LeggingError(f"[{pair_prefix}] A 주문 실패 → B 롤백 완료")

        # 양쪽 모두 실패
        logger.error(f"[{pair_prefix}] [주문실패] 양쪽 모두 실패 | A: {results[0]} | B: {results[1]}")
        raise LeggingError(f"[{pair_prefix}] 양쪽 주문 모두 실패")

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
                order = await asyncio.get_running_loop().run_in_executor(
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

    async def _rollback_leg(
        self, symbol: str, side: str, qty: float, price: float, pair_prefix: str = ""
    ):
        """짝짝이 체결 시 성공했던 레그를 반대 방향 시장가로 즉시 되돌립니다."""
        reverse_side = "sell" if side == "buy" else "buy"
        logger.warning(
            f"[{pair_prefix}] [롤백] {symbol} {reverse_side} {qty:.4f} 시장가 되돌리기"
        )
        if IS_PAPER_TRADING:
            self._paper.close_order(symbol, price, pair_prefix)
            return
        try:
            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._exchange.create_order(
                    symbol, "market", reverse_side, qty, {"reduceOnly": True}
                ),
            )
            logger.info(f"[{pair_prefix}] [롤백 성공] {symbol} {reverse_side} {qty:.4f}")
        except Exception as e:
            logger.critical(
                f"[{pair_prefix}] [롤백 실패!] {symbol}: {e} — 수동 확인 필요!"
            )

    async def _close_real_position(self, symbol: str, pair_prefix: str = ""):
        """실거래 포지션 전량 청산 (reduceOnly)."""
        for attempt in range(1, ORDER_RETRY_COUNT + 1):
            try:
                positions = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self._exchange.fetch_positions([symbol])
                )
                pos = positions[0] if positions else {}
                contracts = abs(float(pos.get("contracts", 0)))
                if contracts == 0:
                    logger.info(f"[{pair_prefix}] [청산] {symbol} 보유 포지션 없음, 스킵")
                    return
                
                # CCXT의 contracts는 절대값이므로, side 필드로 방향 판단
                pos_side = pos.get("side")
                if pos_side == "short":
                    close_side = "buy"
                elif pos_side == "long":
                    close_side = "sell"
                else:
                    amt = float(pos.get("info", {}).get("positionAmt", 0))
                    close_side = "sell" if amt > 0 else "buy"
                    
                order = await asyncio.get_running_loop().run_in_executor(
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
