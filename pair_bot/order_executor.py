# =============================================================================
# pair_bot/order_executor.py
# 실주문(Binance Futures) / 페이퍼 트레이딩 주문 집행기
# IS_PAPER_TRADING 플래그로 모드 전환
# 다중 페어 지원: 심볼을 인자로 전달받아 어느 페어든 처리 가능
# + Maker-Chasing 익절 엔진 (지정가 추적 → 100% Maker 체결)
# =============================================================================

import asyncio
import logging
from config import (
    API_KEY, API_SECRET, IS_PAPER_TRADING, USE_TESTNET,
    PAIRS_TO_TRADE, LEVERAGE,
    ORDER_RETRY_COUNT, ORDER_RETRY_WAIT,
    PAPER_INITIAL_BALANCE, ALLOCATION_PER_PAIR,
    MAKER_ORDER_TIMEOUT,
    CHASE_INTERVAL_SEC, CHASE_MAX_ITERATIONS, MIN_CHASE_PROFIT_PCT,
    TAKER_FEE_RATE, MAKER_FEE_RATE,
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
                "options"        : {"defaultType": "future", "adjustForTimeDifference": True},
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
            if "-1021" in str(e) or "Timestamp" in str(e):
                logger.warning(f"[가용잔고조회] 시간 오차 감지. 타임스탬프 리동기화 시도...")
                try:
                    await asyncio.get_running_loop().run_in_executor(None, self._exchange.load_time_difference)
                except Exception:
                    pass
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
            if "-1021" in str(e) or "Timestamp" in str(e):
                logger.warning(f"[총잔고조회] 시간 오차 감지. 타임스탬프 리동기화 시도...")
                try:
                    await asyncio.get_running_loop().run_in_executor(None, self._exchange.load_time_difference)
                except Exception:
                    pass
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
        """두 레그를 동시에 시장가 청산합니다. (손절/킬스위치/수동용)"""
        logger.info(f"[{pair_prefix}] [시장가 청산] reason={reason} | A~{price_a:.4f}, B~{price_b:.4f}")

        if IS_PAPER_TRADING:
            pos_a = self._paper.positions.get(sym_a, {})
            qty_a = pos_a.get("qty", 0.0)
            pos_b = self._paper.positions.get(sym_b, {})
            qty_b = pos_b.get("qty", 0.0)
            
            pnl_a = self._paper.close_order(sym_a, price_a, pair_prefix)
            pnl_b = self._paper.close_order(sym_b, price_b, pair_prefix)
            total  = pnl_a + pnl_b
            logger.info(
                f"[{pair_prefix}] [PAPER] 청산완료 | reason={reason} | "
                f"총 PnL={total:+.4f} USDT | 잔고={self._paper.balance:.2f} USDT"
            )
            return (
                {"average": price_a, "qty": qty_a, "maker": False},
                {"average": price_b, "qty": qty_b, "maker": False}
            )

        results = await asyncio.gather(
            self._close_real_position(sym_a, pair_prefix),
            self._close_real_position(sym_b, pair_prefix),
            return_exceptions=True
        )
        return results

    async def close_pair_limit(
        self,
        sym_a: str, price_a: float,
        sym_b: str, price_b: float,
        pos_side: str,
        pair_prefix: str = "",
    ):
        """
        두 레그를 지정가(Maker)로 익절 청산합니다.
        pos_side: "LONG_A_SHORT_B" | "SHORT_A_LONG_B"
        
        MAKER_ORDER_TIMEOUT(60초 -> 이제 5초) 이내 체결되지 않으면
        자동으로 주문 취소 → 시장가(Taker) Fallback 청산.
        
        페이퍼 모드에서는 기존 시장가 시뮬레이션으로 처리.
        """
        logger.info(
            f"[{pair_prefix}] [지정가 익절] pos_side={pos_side} | "
            f"A~{price_a:.4f}, B~{price_b:.4f}"
        )

        if IS_PAPER_TRADING:
            pos_a = self._paper.positions.get(sym_a, {})
            qty_a = pos_a.get("qty", 0.0)
            pos_b = self._paper.positions.get(sym_b, {})
            qty_b = pos_b.get("qty", 0.0)
            
            pnl_a = self._paper.close_order(sym_a, price_a, pair_prefix)
            pnl_b = self._paper.close_order(sym_b, price_b, pair_prefix)
            total  = pnl_a + pnl_b
            logger.info(
                f"[{pair_prefix}] [PAPER] 지정가 익절 완료 | "
                f"총 PnL={total:+.4f} USDT | 잔고={self._paper.balance:.2f} USDT"
            )
            return (
                {"average": price_a, "qty": qty_a, "maker": True},
                {"average": price_b, "qty": qty_b, "maker": True}
            )

        # 실거래: 두 레그를 병렬로 지정가 청산 시도
        results = await asyncio.gather(
            self._close_limit_with_fallback(sym_a, pos_side, "A", pair_prefix),
            self._close_limit_with_fallback(sym_b, pos_side, "B", pair_prefix),
            return_exceptions=True
        )
        return results

    # ── 지정가 청산 + Fallback ────────────────────────────────────────────────

    async def _close_limit_with_fallback(
        self, symbol: str, pos_side: str, leg: str, pair_prefix: str
    ):
        """
        1) 현재 포지션 수량 조회
        2) 최우선 호가에 지정가(Post-Only) 주문
        3) MAKER_ORDER_TIMEOUT 초 대기
        4) 미체결 시 주문 취소 → 시장가 Fallback
        """
        try:
            # 포지션 조회
            positions = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._exchange.fetch_positions([symbol])
            )
            pos = positions[0] if positions else {}
            contracts = abs(float(pos.get("contracts", 0)))
            if contracts == 0:
                logger.info(f"[{pair_prefix}] [지정가] {symbol} 보유 없음, 스킵")
                return {"symbol": symbol, "average": 0.0, "fee": 0.0, "qty": 0.0, "side": "", "maker": True}

            # 방향 결정
            pos_side_raw = pos.get("side")
            if pos_side_raw == "short":
                close_side = "buy"
            elif pos_side_raw == "long":
                close_side = "sell"
            else:
                amt = float(pos.get("info", {}).get("positionAmt", 0))
                close_side = "sell" if amt > 0 else "buy"

            # 오더북 조회 → 최우선 호가 결정
            ob = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._exchange.fetch_order_book(symbol, 5)
            )
            if close_side == "sell":
                # Long 청산 → Best Ask에 Sell Limit (더 높은 가격에 팔기)
                limit_price = ob["asks"][0][0] if ob.get("asks") else None
            else:
                # Short 청산 → Best Bid에 Buy Limit (더 낮은 가격에 사기)
                limit_price = ob["bids"][0][0] if ob.get("bids") else None

            if limit_price is None:
                logger.warning(
                    f"[{pair_prefix}] [지정가] {symbol} 호가 없음 → 시장가 Fallback"
                )
                return await self._close_real_position(symbol, pair_prefix)

            # 지정가 주문 (Post-Only로 Maker 보장)
            order = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._exchange.create_order(
                    symbol, "limit", close_side, contracts, limit_price,
                    {"reduceOnly": True, "timeInForce": "GTX"}  # GTX = Post-Only
                )
            )
            order_id = order["id"]
            logger.info(
                f"[{pair_prefix}] [지정가] {symbol} {close_side} {contracts:.4f} "
                f"@ {limit_price:.4f} 주문 (id={order_id})"
            )

            # 체결 대기 (MAKER_ORDER_TIMEOUT초)
            elapsed = 0
            check_interval = 1  # 5초에서 1초로 단축! (Fallback 5초 대응)
            while elapsed < MAKER_ORDER_TIMEOUT:
                await asyncio.sleep(check_interval)
                elapsed += check_interval
                try:
                    fetched = await asyncio.get_running_loop().run_in_executor(
                        None, lambda: self._exchange.fetch_order(order_id, symbol)
                    )
                    status = fetched.get("status", "")
                    if status == "closed":
                        logger.info(
                            f"[{pair_prefix}] [지정가 체결] {symbol} 완료! "
                            f"(Maker 수수료 적용, {elapsed}초 소요)"
                        )
                        return await self._aggregate_trades_for_order(
                            symbol, order_id, contracts, close_side, is_maker=True, pair_prefix=pair_prefix
                        )
                    elif status == "canceled":
                        logger.warning(
                            f"[{pair_prefix}] [지정가] {symbol} 외부 취소됨 → 시장가 Fallback"
                        )
                        break
                except Exception as e:
                    logger.debug(f"[{pair_prefix}] 주문 상태 조회 오류: {e}")

            # Fallback: 미체결 → 주문 취소 → 시장가 Fallback
            logger.warning(
                f"[{pair_prefix}] [지정가 미체결] {symbol} {MAKER_ORDER_TIMEOUT}초 초과 "
                f"→ 주문 취소 후 시장가 Fallback"
            )
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self._exchange.cancel_order(order_id, symbol)
                )
            except Exception as e:
                logger.debug(f"[{pair_prefix}] 주문 취소 중 오류 (이미 체결?): {e}")

            # 시장가로 잔여 수량 청산
            return await self._close_real_position(symbol, pair_prefix)

        except Exception as e:
            logger.error(
                f"[{pair_prefix}] [지정가 실패] {symbol}: {e} → 시장가 Fallback"
            )
            try:
                return await self._close_real_position(symbol, pair_prefix)
            except Exception as e2:
                logger.critical(
                    f"[{pair_prefix}] [Fallback 시장가도 실패!] {symbol}: {e2} — 수동 확인 필요!"
                )
                raise e2

    # ── Maker-Chasing 엔진 (지정가 추적 → 100% Maker 체결) ────────────────────

    async def chase_maker_order(
        self, symbol: str, pair_prefix: str,
        pos_info: dict = None,
        shared: dict = None,
    ) -> dict:
        """
        지정가 추적(Limit Order Chasing) 엔진 — 페어 동행 청산 보장판.

        핵심 안전 규칙 (Legging Risk 방지):
          1. 반대 레그가 먼저 체결되면 이 레그는 즉시 시장가로 동행 청산
             (헷지가 깨진 단방향 노출을 유지하지 않음)
          2. 반대 레그가 추적을 포기(ABORT)하면 이 레그도 함께 포기 (협조 Hold)
          3. ABORT는 양 레그 모두 완전 미체결일 때만 허용
          4. 부분 체결이 발생한 순간부터 ABORT 금지 → 잔량 끝까지 청산
          5. 추적 중 발생한 모든 주문의 realizedPnl/fee를 누락 없이 합산

        shared: close_pair_chase가 주입하는 레그 간 공유 상태
          {"filled": {"A": bool, "B": bool},
           "aborted": {"A": bool, "B": bool},
           "px": {"A": float|None, "B": float|None}}

        반환값:
          - 체결 성공: {"average":..., "qty":..., "maker":..., ...}
          - 추적 포기: {"status": "ABORTED", "reason": "..."}  (양 레그 미체결일 때만)
        """
        leg = (pos_info or {}).get("leg", "")
        other_leg = "B" if leg == "A" else "A"

        def _other_filled() -> bool:
            return bool(shared and leg and shared["filled"].get(other_leg))

        def _other_aborted() -> bool:
            return bool(shared and leg and shared["aborted"].get(other_leg))

        def _mark_filled():
            if shared and leg:
                shared["filled"][leg] = True

        def _mark_aborted():
            if shared and leg:
                shared["aborted"][leg] = True

        current_order_id = None
        placed_order_ids: list = []
        partial_filled = False

        async def _force_market_close() -> dict:
            """시장가 동행 청산 + 추적 중 부분 체결분 합산."""
            result = await self._close_real_position(symbol, pair_prefix)
            _mark_filled()
            return await self._finalize_with_partials(
                symbol, placed_order_ids, result, pair_prefix
            )

        try:
            # 포지션 조회
            positions = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._exchange.fetch_positions([symbol])
            )
            pos = positions[0] if positions else {}
            contracts = abs(float(pos.get("contracts", 0)))
            if contracts == 0:
                logger.info(f"[{pair_prefix}] [Chasing] {symbol} 보유 없음, 스킵")
                _mark_filled()  # 이 레그는 이미 닫힘 → 반대 레그도 동행 청산하도록 신호
                return {"symbol": symbol, "average": 0.0, "fee": 0.0,
                        "qty": 0.0, "side": "", "maker": True}

            # 방향 결정
            pos_side_raw = pos.get("side")
            if pos_side_raw == "short":
                close_side = "buy"
            elif pos_side_raw == "long":
                close_side = "sell"
            else:
                amt = float(pos.get("info", {}).get("positionAmt", 0))
                close_side = "sell" if amt > 0 else "buy"

            remaining = contracts

            for iteration in range(1, CHASE_MAX_ITERATIONS + 1):
                # ── 동행 규칙 1: 반대 레그 체결됨 → 즉시 시장가 동행 청산 ──
                if _other_filled():
                    filled_amt = await self._cancel_and_get_filled(current_order_id, symbol)
                    if filled_amt > 0:
                        partial_filled = True
                    logger.warning(
                        f"[{pair_prefix}] [Chasing 동행] {symbol} "
                        f"반대 레그 체결 감지 → 즉시 시장가 동행 청산 (iter={iteration})"
                    )
                    return await _force_market_close()

                # ── 동행 규칙 2: 반대 레그 포기 → 함께 포기 (헷지 유지 Hold) ──
                if _other_aborted() and not partial_filled:
                    filled_amt = await self._cancel_and_get_filled(current_order_id, symbol)
                    if filled_amt > 0:
                        # 취소 직전 체결 레이스 → 헷지 일부 붕괴, 시장가로 마저 청산
                        partial_filled = True
                        return await _force_market_close()
                    logger.info(
                        f"[{pair_prefix}] [Chasing 협조포기] {symbol} "
                        f"반대 레그 ABORT → 함께 Hold (iter={iteration})"
                    )
                    _mark_aborted()
                    return {
                        "status": "ABORTED",
                        "reason": "반대 레그 ABORT에 동조 (헷지 유지)",
                        "symbol": symbol,
                    }

                # 오더북 조회 → 최우선 호가 결정
                ob = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self._exchange.fetch_order_book(symbol, 5)
                )
                # 자기 레그 현재가(중간가)를 공유 상태에 게시 → 반대 레그 PnL 추정 정확도 확보
                if shared and leg and ob.get("bids") and ob.get("asks"):
                    shared["px"][leg] = (ob["bids"][0][0] + ob["asks"][0][0]) / 2.0

                if close_side == "sell":
                    # Long 청산 → Best Ask에 Sell Limit
                    chase_price = ob["asks"][0][0] if ob.get("asks") else None
                else:
                    # Short 청산 → Best Bid에 Buy Limit
                    chase_price = ob["bids"][0][0] if ob.get("bids") else None

                if chase_price is None:
                    logger.warning(
                        f"[{pair_prefix}] [Chasing] {symbol} 호가 없음 (iter={iteration})"
                    )
                    await asyncio.sleep(CHASE_INTERVAL_SEC)
                    continue

                # ── 수익 마지노선 체크 (매 반복마다, 반대 레그 최신가 반영) ──
                if pos_info:
                    if shared and shared["px"].get(other_leg):
                        # 트리거 시점 가격 고정 버그 수정: 반대 레그 실시간가로 갱신
                        pos_info["current_price_other"] = shared["px"][other_leg]
                    estimated_pnl_pct = self._estimate_chase_pnl(
                        pos_info, symbol, chase_price
                    )
                    if estimated_pnl_pct < MIN_CHASE_PROFIT_PCT:
                        if _other_filled() or partial_filled:
                            # 헷지가 이미 (일부) 깨짐 → 포기 불가, 시장가 동행
                            await self._cancel_and_get_filled(current_order_id, symbol)
                            return await _force_market_close()
                        # 양 레그 모두 미체결 → 안전한 Hold
                        filled_amt = await self._cancel_and_get_filled(current_order_id, symbol)
                        if filled_amt > 0:
                            partial_filled = True
                            return await _force_market_close()
                        logger.warning(
                            f"[{pair_prefix}] [Chasing ABORT] {symbol} "
                            f"iter={iteration} | 예상 PnL={estimated_pnl_pct:+.2f}% "
                            f"< 마지노선 {MIN_CHASE_PROFIT_PCT}% → 추적 포기 (Hold)"
                        )
                        _mark_aborted()
                        return {
                            "status": "ABORTED",
                            "reason": f"예상 PnL {estimated_pnl_pct:+.2f}% < {MIN_CHASE_PROFIT_PCT}%",
                            "symbol": symbol,
                        }

                # 기존 미체결 주문이 있으면 체결 확인 후 취소
                if current_order_id:
                    try:
                        fetched = await asyncio.get_running_loop().run_in_executor(
                            None, lambda: self._exchange.fetch_order(
                                current_order_id, symbol
                            )
                        )
                        if fetched.get("status") == "closed":
                            logger.info(
                                f"[{pair_prefix}] [Chasing 체결] {symbol} "
                                f"iter={iteration} Maker 체결! ({iteration * CHASE_INTERVAL_SEC:.0f}초 소요)"
                            )
                            _mark_filled()
                            return await self._aggregate_trades_for_orders(
                                symbol, placed_order_ids, contracts,
                                close_side, is_maker=True,
                                pair_prefix=pair_prefix
                            )
                        # 미체결 → 취소 (부분 체결량 반영)
                        await asyncio.get_running_loop().run_in_executor(
                            None, lambda: self._exchange.cancel_order(
                                current_order_id, symbol
                            )
                        )
                        filled_amt = float(fetched.get("filled") or 0)
                        if filled_amt > 0:
                            partial_filled = True
                            remaining = max(0.0, remaining - filled_amt)
                            if remaining <= contracts * 1e-6:
                                # 사실상 전량 체결 완료
                                _mark_filled()
                                return await self._aggregate_trades_for_orders(
                                    symbol, placed_order_ids, contracts,
                                    close_side, is_maker=True,
                                    pair_prefix=pair_prefix
                                )
                    except Exception as e:
                        logger.debug(
                            f"[{pair_prefix}] [Chasing] 주문 취소/조회 오류: {e}"
                        )
                    current_order_id = None

                # 새 호가에 지정가 주문 (Post-Only) — 잔량 기준
                try:
                    order = await asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda: self._exchange.create_order(
                            symbol, "limit", close_side, remaining, chase_price,
                            {"reduceOnly": True, "timeInForce": "GTX"}
                        )
                    )
                    current_order_id = order["id"]
                    placed_order_ids.append(current_order_id)
                    logger.debug(
                        f"[{pair_prefix}] [Chasing] {symbol} {close_side} "
                        f"{remaining:.4f} @ {chase_price:.4f} "
                        f"(iter={iteration}/{CHASE_MAX_ITERATIONS})"
                    )
                except Exception as e:
                    if "-2022" in str(e) or "ReduceOnly Order is rejected" in str(e):
                        # 포지션이 이미 닫힘 → 추적 중 부분 체결분을 실제 정산으로 합산
                        logger.warning(
                            f"[{pair_prefix}] [Chasing] {symbol} 이미 청산됨(-2022). "
                            f"체결 내역 합산으로 정산."
                        )
                        _mark_filled()
                        return await self._aggregate_trades_for_orders(
                            symbol, placed_order_ids, contracts,
                            close_side, is_maker=True, pair_prefix=pair_prefix
                        )
                    # Post-Only 거부(이미 Taker 가격) 등 → 재시도
                    logger.debug(
                        f"[{pair_prefix}] [Chasing] 주문 실패 iter={iteration}: {e}"
                    )
                    current_order_id = None

                # 체결 대기
                await asyncio.sleep(CHASE_INTERVAL_SEC)

                # 반복 끝 체결 확인
                if current_order_id:
                    try:
                        fetched = await asyncio.get_running_loop().run_in_executor(
                            None, lambda: self._exchange.fetch_order(
                                current_order_id, symbol
                            )
                        )
                        if fetched.get("status") == "closed":
                            logger.info(
                                f"[{pair_prefix}] [Chasing 체결] {symbol} "
                                f"Maker 체결 완료! (iter={iteration})"
                            )
                            _mark_filled()
                            return await self._aggregate_trades_for_orders(
                                symbol, placed_order_ids, contracts,
                                close_side, is_maker=True,
                                pair_prefix=pair_prefix
                            )
                    except Exception:
                        pass

            # ── 최대 추적 횟수 초과 → 최종 처리 ──
            filled_amt = await self._cancel_and_get_filled(current_order_id, symbol)
            if filled_amt > 0:
                partial_filled = True
            current_order_id = None

            # 헷지가 (일부) 깨졌으면 무조건 시장가 동행 청산
            if _other_filled() or partial_filled:
                logger.warning(
                    f"[{pair_prefix}] [Chasing Fallback] {symbol} "
                    f"{CHASE_MAX_ITERATIONS}회 초과 + 헷지 일부 붕괴 → 시장가 청산"
                )
                return await _force_market_close()

            # 양 레그 모두 미체결 → 수익 마지노선 최종 체크 후 시장가 or Hold
            if pos_info:
                ob = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self._exchange.fetch_order_book(symbol, 5)
                )
                if close_side == "sell":
                    fb_price = ob["bids"][0][0] if ob.get("bids") else 0
                else:
                    fb_price = ob["asks"][0][0] if ob.get("asks") else 0

                if fb_price > 0:
                    if shared and shared["px"].get(other_leg):
                        pos_info["current_price_other"] = shared["px"][other_leg]
                    fb_pnl_pct = self._estimate_chase_pnl(
                        pos_info, symbol, fb_price
                    )
                    if fb_pnl_pct < MIN_CHASE_PROFIT_PCT:
                        logger.warning(
                            f"[{pair_prefix}] [Chasing ABORT-Final] {symbol} "
                            f"{CHASE_MAX_ITERATIONS}회 초과 + 시장가 PnL "
                            f"{fb_pnl_pct:+.2f}% < {MIN_CHASE_PROFIT_PCT}% "
                            f"→ 시장가도 포기, Hold"
                        )
                        _mark_aborted()
                        return {
                            "status": "ABORTED",
                            "reason": f"최종 Fallback PnL {fb_pnl_pct:+.2f}% < {MIN_CHASE_PROFIT_PCT}%",
                            "symbol": symbol,
                        }

            # 수익 마지노선 통과 → 시장가 Fallback
            logger.warning(
                f"[{pair_prefix}] [Chasing Fallback] {symbol} "
                f"{CHASE_MAX_ITERATIONS}회 초과 → 수익 마지노선 통과, 시장가 청산"
            )
            return await _force_market_close()

        except Exception as e:
            logger.error(
                f"[{pair_prefix}] [Chasing 예외] {symbol}: {e}"
            )
            # 예외 시에도 미체결 주문 정리 시도
            filled_amt = await self._cancel_and_get_filled(current_order_id, symbol)
            if filled_amt > 0:
                partial_filled = True

            # 헷지가 (일부) 깨진 상태면 Hold 불가 → 시장가 동행 청산 시도
            if _other_filled() or partial_filled:
                try:
                    return await _force_market_close()
                except Exception as e2:
                    logger.critical(
                        f"[{pair_prefix}] [Chasing 비상] {symbol} 동행 청산도 실패: {e2} "
                        f"— 수동 확인 필요!"
                    )
                    raise

            # 양 레그 모두 미체결 → 안전한 Hold
            _mark_aborted()
            return {
                "status": "ABORTED",
                "reason": f"Chasing 예외: {e}",
                "symbol": symbol,
            }

    async def _cancel_and_get_filled(self, order_id, symbol: str) -> float:
        """
        주문을 취소하고 누적 체결 수량을 반환합니다.
        취소 직전에 체결되는 레이스를 취소 후 재조회로 보정합니다.
        order_id가 None이거나 조회 실패 시 0.0 반환.
        """
        if not order_id:
            return 0.0
        filled = 0.0
        try:
            fetched = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._exchange.fetch_order(order_id, symbol)
            )
            filled = float(fetched.get("filled") or 0)
            if fetched.get("status") == "closed":
                return filled
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._exchange.cancel_order(order_id, symbol)
            )
            refetched = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._exchange.fetch_order(order_id, symbol)
            )
            filled = float(refetched.get("filled") or filled)
        except Exception:
            pass
        return filled

    async def _finalize_with_partials(
        self, symbol: str, placed_order_ids: list,
        market_result: dict, pair_prefix: str = "",
    ) -> dict:
        """
        시장가 청산 결과에 추적 중 발생한 Maker 부분 체결분을 합산합니다.
        (부분 체결 PnL 누락 방지)
        """
        if not placed_order_ids:
            return market_result
        maker_part = await self._aggregate_trades_for_orders(
            symbol, placed_order_ids, 0.0, market_result.get("side", ""),
            is_maker=True, pair_prefix=pair_prefix,
        )
        if maker_part.get("qty", 0.0) <= 0:
            return market_result
        return self._merge_close_results(maker_part, market_result)

    @staticmethod
    def _merge_close_results(res_x: dict, res_y: dict) -> dict:
        """두 청산 결과(부분 체결 + 시장가 잔량)를 하나로 병합합니다."""
        qty_x = float(res_x.get("qty", 0.0) or 0.0)
        qty_y = float(res_y.get("qty", 0.0) or 0.0)
        if qty_x <= 0:
            return res_y
        if qty_y <= 0:
            return res_x

        qty = qty_x + qty_y
        avg_x = float(res_x.get("average", 0.0) or 0.0)
        avg_y = float(res_y.get("average", 0.0) or 0.0)
        if avg_x > 0 and avg_y > 0:
            average = (avg_x * qty_x + avg_y * qty_y) / qty
        else:
            average = avg_y or avg_x

        rp_x = res_x.get("total_realized_pnl")
        rp_y = res_y.get("total_realized_pnl")
        tf_x = res_x.get("total_fee")
        tf_y = res_y.get("total_fee")

        return {
            "symbol": res_y.get("symbol") or res_x.get("symbol"),
            "average": average,
            "qty": qty,
            "side": res_y.get("side") or res_x.get("side"),
            "maker": False,  # 혼합 체결
            "fee": float(res_x.get("fee", 0.0) or 0.0) + float(res_y.get("fee", 0.0) or 0.0),
            "total_realized_pnl": (
                float(rp_x) + float(rp_y)
                if (rp_x is not None and rp_y is not None) else None
            ),
            "total_fee": (
                float(tf_x) + float(tf_y)
                if (tf_x is not None and tf_y is not None) else None
            ),
        }

    def _estimate_chase_pnl(
        self, pos_info: dict, symbol: str, chase_price: float
    ) -> float:
        """
        현재 추적 호가(chase_price) 기준으로 해당 레그의
        페어 전체 예상 Net PnL(%)을 가계산합니다.

        pos_info 필드:
          entry_price_a, entry_price_b: 진입가
          margin_a, margin_b: 레그별 증거금
          qty_a, qty_b: 레그별 수량
          side: "LONG_A_SHORT_B" | "SHORT_A_LONG_B"
          leg: "A" | "B" - 이 함수를 호출하는 레그
          current_price_other: 반대쪽 레그의 현재 가격 (없으면 진입가 사용)
        """
        entry_a = pos_info["entry_price_a"]
        entry_b = pos_info["entry_price_b"]
        qty_a = pos_info["qty_a"]
        qty_b = pos_info["qty_b"]
        margin_a = pos_info["margin_a"]
        margin_b = pos_info["margin_b"]
        side = pos_info["side"]
        leg = pos_info["leg"]
        other_price = pos_info.get("current_price_other")

        # 이 레그가 chase_price로 체결될 때의 시뮬레이션 가격
        if leg == "A":
            sim_price_a = chase_price
            sim_price_b = other_price if other_price else entry_b
        else:
            sim_price_a = other_price if other_price else entry_a
            sim_price_b = chase_price

        # Gross PnL 계산
        if side == "LONG_A_SHORT_B":
            pnl_a = qty_a * (sim_price_a - entry_a)
            pnl_b = qty_b * (entry_b - sim_price_b)
        else:  # SHORT_A_LONG_B
            pnl_a = qty_a * (entry_a - sim_price_a)
            pnl_b = qty_b * (sim_price_b - entry_b)

        gross = pnl_a + pnl_b

        # 수수료: 진입 Taker + 청산 Maker (Chasing이니까)
        entry_notional = (qty_a * entry_a) + (qty_b * entry_b)
        exit_notional = (qty_a * sim_price_a) + (qty_b * sim_price_b)
        total_fee = (entry_notional * TAKER_FEE_RATE) + (exit_notional * MAKER_FEE_RATE)

        net_pnl = gross - total_fee
        total_margin = margin_a + margin_b

        return (net_pnl / total_margin * 100) if total_margin > 0 else 0.0

    async def close_pair_chase(
        self,
        sym_a: str, sym_b: str,
        pair_prefix: str = "",
        pos_info_a: dict = None,
        pos_info_b: dict = None,
    ) -> tuple:
        """
        두 레그를 병렬로 Maker-Chasing 청산합니다.

        pos_info_a, pos_info_b: 각 레그의 PnL 가계산에 필요한 포지션 정보.
          chase_maker_order에 전달됩니다.

        페이퍼 모드: 기존 시장가 시뮬레이션으로 처리.

        반환: (result_a, result_b)
          - 각 result가 {"status": "ABORTED"} 이면 해당 레그 추적 포기
            (협조 규칙상 ABORT는 양 레그 모두 미체결일 때만 발생)
        """
        logger.info(
            f"[{pair_prefix}] [Maker-Chasing 청산] A={sym_a}, B={sym_b}"
        )

        # 레그 간 공유 상태: 체결/포기 신호 + 실시간 중간가 (동행 청산 협조용)
        chase_shared = {
            "filled":  {"A": False, "B": False},
            "aborted": {"A": False, "B": False},
            "px":      {"A": None, "B": None},
        }

        if IS_PAPER_TRADING:
            pos_a = self._paper.positions.get(sym_a, {})
            qty_a = pos_a.get("qty", 0.0)
            price_a = pos_a.get("entry_price", 0.0)
            pos_b = self._paper.positions.get(sym_b, {})
            qty_b = pos_b.get("qty", 0.0)
            price_b = pos_b.get("entry_price", 0.0)

            pnl_a = self._paper.close_order(sym_a, price_a, pair_prefix)
            pnl_b = self._paper.close_order(sym_b, price_b, pair_prefix)
            total = pnl_a + pnl_b
            logger.info(
                f"[{pair_prefix}] [PAPER] Chasing 청산 완료 | "
                f"총 PnL={total:+.4f} USDT | 잔고={self._paper.balance:.2f} USDT"
            )
            return (
                {"average": price_a, "qty": qty_a, "maker": True},
                {"average": price_b, "qty": qty_b, "maker": True},
            )

        results = await asyncio.gather(
            self.chase_maker_order(sym_a, pair_prefix, pos_info=pos_info_a, shared=chase_shared),
            self.chase_maker_order(sym_b, pair_prefix, pos_info=pos_info_b, shared=chase_shared),
            return_exceptions=True,
        )
        return results

    async def close_leg_market(self, symbol: str, pair_prefix: str = "") -> dict:
        """
        단일 레그 강제 시장가 청산 (공개 래퍼).
        한쪽 레그만 체결되고 반대쪽이 포기한 헷지 붕괴 상태를 복구할 때 사용합니다.
        """
        return await self._close_real_position(symbol, pair_prefix)

    async def fetch_funding_total(
        self, symbols: list, since_ms: int, pair_prefix: str = ""
    ) -> float:
        """
        since_ms 이후 심볼들의 펀딩비 합계(USDT)를 반환합니다.
        음수 = 지불, 양수 = 수취. 조회 실패 시 해당 심볼은 0으로 처리.
        """
        if IS_PAPER_TRADING:
            return 0.0
        total = 0.0
        for symbol in symbols:
            try:
                rows = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda s=symbol: self._exchange.fetch_funding_history(s, since=since_ms),
                )
                for r in rows or []:
                    amt = r.get("amount")
                    if amt is not None:
                        total += float(amt)
            except Exception as e:
                logger.debug(f"[{pair_prefix}] 펀딩비 조회 실패 {symbol}: {e}")
        return total

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
                    symbol, "market", reverse_side, qty, None, {"reduceOnly": True}
                ),
            )
            logger.info(f"[{pair_prefix}] [롤백 성공] {symbol} {reverse_side} {qty:.4f}")
        except Exception as e:
            logger.critical(
                f"[{pair_prefix}] [롤백 실패!] {symbol}: {e} — 수동 확인 필요!"
            )

    async def _aggregate_trades_for_order(
        self, symbol: str, order_id: str, qty: float, side: str,
        is_maker: bool = False, pair_prefix: str = ""
    ) -> dict:
        """단일 order_id의 모든 부분 체결을 합산합니다 (다중 버전에 위임)."""
        return await self._aggregate_trades_for_orders(
            symbol, [order_id], qty, side,
            is_maker=is_maker, pair_prefix=pair_prefix,
        )

    async def _aggregate_trades_for_orders(
        self, symbol: str, order_ids: list, qty: float, side: str,
        is_maker: bool = False, pair_prefix: str = ""
    ) -> dict:
        """
        fetch_my_trades를 호출해 주어진 order_id들의 모든 부분 체결(Trade)을 합산합니다.
        Chasing 중 여러 주문에 걸쳐 부분 체결이 발생해도 realizedPnl과 fee를 누락 없이 누적합니다.
        """
        id_set = {oid for oid in (order_ids or []) if oid}
        try:
            if not id_set:
                return {"symbol": symbol, "average": 0.0, "fee": 0.0, "qty": qty, "side": side, "maker": is_maker, "total_realized_pnl": None, "total_fee": None}

            await asyncio.sleep(0.5)  # 거래소 정산 지연 대기
            trades = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._exchange.fetch_my_trades(symbol, limit=50)
            )
            matched = [t for t in trades if t.get("order") in id_set]

            if not matched:
                logger.warning(f"[{pair_prefix}] fetch_my_trades에서 order_ids={list(id_set)} 매칭 없음 — 기본값 사용")
                return {"symbol": symbol, "average": 0.0, "fee": 0.0, "qty": qty, "side": side, "maker": is_maker, "total_realized_pnl": None, "total_fee": None}

            total_realized_pnl = 0.0
            total_fee = 0.0
            total_cost = 0.0  # price * amount 합산 (가중평균가 계산용)
            total_amount = 0.0

            for t in matched:
                info = t.get("info", {})
                rpnl = info.get("realizedPnl")
                if rpnl is not None:
                    total_realized_pnl += float(rpnl)
                commission = info.get("commission")
                if commission is not None:
                    total_fee += float(commission)
                elif t.get("fee") and t["fee"].get("cost"):
                    total_fee += float(t["fee"]["cost"])
                amt = float(t.get("amount", 0))
                prc = float(t.get("price", 0))
                total_amount += amt
                total_cost += amt * prc

            avg_price = (total_cost / total_amount) if total_amount > 0 else 0.0

            logger.info(
                f"[{pair_prefix}] [체결합산] {symbol} | 체결건수={len(matched)} | "
                f"realizedPnl={total_realized_pnl:+.4f} | fee={total_fee:.4f} | avg={avg_price:.4f}"
            )
            return {
                "symbol": symbol, "average": avg_price, "fee": total_fee,
                "qty": total_amount if total_amount > 0 else qty,
                "side": side, "maker": is_maker,
                "total_realized_pnl": total_realized_pnl, "total_fee": total_fee,
            }
        except Exception as e:
            logger.warning(f"[{pair_prefix}] fetch_my_trades 실패: {e} — 기본값 사용")
            return {"symbol": symbol, "average": 0.0, "fee": 0.0, "qty": qty, "side": side, "maker": is_maker, "total_realized_pnl": None, "total_fee": None}

    async def _close_real_position(self, symbol: str, pair_prefix: str = ""):
        """실거래 포지션 전량 시장가 청산 (reduceOnly)."""
        for attempt in range(1, ORDER_RETRY_COUNT + 1):
            try:
                positions = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self._exchange.fetch_positions([symbol])
                )
                pos = positions[0] if positions else {}
                contracts = abs(float(pos.get("contracts", 0)))
                if contracts == 0:
                    logger.info(f"[{pair_prefix}] [청산] {symbol} 보유 포지션 없음, 스킵")
                    return {"symbol": symbol, "average": 0.0, "fee": 0.0, "qty": 0.0, "side": "", "maker": False}
                
                # CCXT의 contracts는 절대값이므로, side 필드로 방향 판단
                pos_side = pos.get("side")
                if pos_side == "short":
                    close_side = "buy"
                elif pos_side == "long":
                    close_side = "sell"
                else:
                    amt = float(pos.get("info", {}).get("positionAmt", 0))
                    close_side = "sell" if amt > 0 else "buy"
                    
                try:
                    order = await asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda: self._exchange.create_order(
                            symbol, "market", close_side, contracts, None, {"reduceOnly": True}
                        )
                    )
                except Exception as me:
                    if "-2022" in str(me) or "ReduceOnly Order is rejected" in str(me):
                        # 이미 청산된 포지션 → 이 주문으로 체결된 수량은 0
                        # (qty=contracts로 반환하면 PnL 계산에 유령 수량이 집계됨)
                        logger.warning(f"[{pair_prefix}] [ReduceOnly 거절] {symbol} 이미 거래소에서 청산됨(-2022). 체결량 0으로 처리.")
                        return {"symbol": symbol, "average": 0.0, "fee": 0.0, "qty": 0.0, "side": close_side, "maker": False}
                    elif "-4131" in str(me) or "PERCENT_PRICE" in str(me):
                        logger.warning(f"[{pair_prefix}] 시장가 불가능(-4131). MarkPrice ±1% IOC 지정가로 폴백: {symbol}")
                        mark_price = float(pos.get("markPrice", 0) or pos.get("info", {}).get("markPrice", 0))
                        if mark_price <= 0:
                            raise me
                        # PERCENT_PRICE 필터(보통 5%)를 피하기 위해 Mark Price의 1% 내외로 제한
                        limit_price = mark_price * 1.01 if close_side == "buy" else mark_price * 0.99
                        order = await asyncio.get_running_loop().run_in_executor(
                            None,
                            lambda: self._exchange.create_order(
                                symbol, "limit", close_side, contracts, limit_price, 
                                {"reduceOnly": True, "timeInForce": "IOC"}
                            )
                        )
                    else:
                        raise me

                order_id = order['id']
                logger.info(f"[{pair_prefix}] [실거래] 청산주문 id={order_id} | {symbol} {close_side} {contracts}")
                
                # ── 청산 후 포지션 잔량 검증 (부분 체결 방어) ──
                await asyncio.sleep(1)
                verify_pos_list = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self._exchange.fetch_positions([symbol])
                )
                v_pos = verify_pos_list[0] if verify_pos_list else {}
                remaining = abs(float(v_pos.get("contracts", 0)))
                if remaining > 0:
                    logger.warning(
                        f"[{pair_prefix}] [부분체결] {symbol} 잔여 {remaining}개 — "
                        f"재시도 {attempt}/{ORDER_RETRY_COUNT}"
                    )
                    continue  # 다음 retry에서 잔량 청산 시도

                logger.info(f"[{pair_prefix}] [청산검증] {symbol} 포지션 완전 청산 확인")
                
                # fetch_my_trades로 모든 부분 체결의 realizedPnl과 fee를 완벽 합산
                return await self._aggregate_trades_for_order(
                    symbol, order_id, contracts, close_side, is_maker=False, pair_prefix=pair_prefix
                )

            except Exception as e:
                logger.warning(f"[{pair_prefix}] [청산실패 {attempt}/{ORDER_RETRY_COUNT}] {symbol}: {e}")
                if attempt < ORDER_RETRY_COUNT:
                    await asyncio.sleep(ORDER_RETRY_WAIT)
                else:
                    logger.error(f"[{pair_prefix}] [청산포기] {symbol} — 재시도 횟수 초과")
                    raise

    async def get_active_positions(self) -> dict:
        """
        현재 실제로 거래소(또는 페이퍼 계정)에 열려 있는 모든 선물 포지션 정보를 반환합니다.
        반환 형식: { 'BTC/USDT:USDT': { 'side': 'long'|'short', 'qty': float } }
        """
        if IS_PAPER_TRADING:
            res = {}
            for sym, pos in self._paper.positions.items():
                qty = abs(float(pos.get("qty", 0.0)))
                if qty > 0:
                    # 'buy' -> 'long', 'sell' -> 'short'로 통일
                    side = "long" if pos.get("side") == "buy" else "short"
                    res[sym] = {"side": side, "qty": qty}
            return res

        try:
            positions = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._exchange.fetch_positions()
            )
            res = {}
            for pos in positions:
                contracts = abs(float(pos.get("contracts", 0.0)))
                if contracts > 0:
                    symbol = pos.get("symbol")
                    side = pos.get("side")
                    if side not in ("long", "short"):
                        amt = float(pos.get("info", {}).get("positionAmt", 0.0))
                        side = "long" if amt > 0 else "short"
                    res[symbol] = {"side": side, "qty": contracts}
            return res
        except Exception as e:
            logger.error(f"[포지션동기화] 실제 포지션 조회 실패: {e}")
            raise
