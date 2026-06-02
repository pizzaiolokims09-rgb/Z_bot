# =============================================================================
# pair_bot/order_executor.py
# 실주문(Binance Futures) / 페이퍼 트레이딩 주문 집행기
# IS_PAPER_TRADING 플래그로 모드 전환
# 다중 페어 지원: 심볼을 인자로 전달받아 어느 페어든 처리 가능
# + 지정가(Maker) 익절 청산 & 60초 Fallback
# =============================================================================

import asyncio
import logging
from config import (
    API_KEY, API_SECRET, IS_PAPER_TRADING, USE_TESTNET,
    PAIRS_TO_TRADE, LEVERAGE,
    ORDER_RETRY_COUNT, ORDER_RETRY_WAIT,
    PAPER_INITIAL_BALANCE, ALLOCATION_PER_PAIR,
    MAKER_ORDER_TIMEOUT,
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

    async def _aggregate_trades_for_order(
        self, symbol: str, order_id: str, qty: float, side: str,
        is_maker: bool = False, pair_prefix: str = ""
    ) -> dict:
        """
        fetch_my_trades를 호출해 해당 order_id의 모든 부분 체결(Trade)을 합산합니다.
        부분 체결이 여러 건이어도 realizedPnl과 fee를 누락 없이 누적합니다.
        """
        try:
            await asyncio.sleep(0.5)  # 거래소 정산 지연 대기
            trades = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._exchange.fetch_my_trades(symbol, limit=50)
            )
            matched = [t for t in trades if t.get("order") == order_id]

            if not matched:
                logger.warning(f"[{pair_prefix}] fetch_my_trades에서 order_id={order_id} 매칭 없음 — 기본값 사용")
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
                    
                order = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: self._exchange.create_order(
                        symbol, "market", close_side, contracts, {"reduceOnly": True}
                    )
                )
                order_id = order['id']
                logger.info(f"[{pair_prefix}] [실거래] 청산완료 id={order_id} | {symbol}")
                
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
