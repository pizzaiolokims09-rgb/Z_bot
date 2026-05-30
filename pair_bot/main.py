# =============================================================================
# pair_bot/main.py
# 페어 트레이딩 봇 — 메인 진입점 (5개 페어 병렬 감시 + 텔레그램 연동)
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
    ALLOCATION_PER_PAIR, LEVERAGE,
    POLL_INTERVAL_SEC, LOG_FILE,
    TELEGRAM_BOT_TOKEN, MAX_DRAWDOWN_LIMIT,
    API_KEY, API_SECRET, USE_TESTNET,
    MAX_BTC_VOLATILITY, BTC_VOLATILITY_CHECK_INTERVAL,
)
from spread_engine      import SpreadEngine
from risk_manager       import RiskManager
from order_executor     import OrderExecutor, LeggingError
from bot_state          import BotState, PairPosition
from telegram_bot       import TelegramNotifier
from trade_logger       import init_csv, log_trade, _now_kst
from state_persistence  import save_state, load_state


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
    fh = RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
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


async def fetch_btc_volatility(exchange: ccxt_async.Exchange) -> float:
    """
    BTC/USDT 15분봉 최근 1개를 조회해 변동성(%) = (High-Low)/Low*100 반환.
    조회 실패 시 0.0 반환 (필터 비활성 처리).
    """
    try:
        ohlcv = await exchange.fetch_ohlcv("BTC/USDT", timeframe="15m", limit=1)
        if ohlcv and len(ohlcv) > 0:
            _, _, high, low, _, _ = ohlcv[0]
            if low and low > 0:
                return (high - low) / low * 100.0
    except Exception as e:
        logging.getLogger("pair_bot").debug(f"[BTC변동성 조회 실패] {e}")
    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 청산 공통 처리 (익절 / 손절 / 수동 — 코드 중복 방지)
# ─────────────────────────────────────────────────────────────────────────────
async def _execute_close(
    prefix: str,
    reason: str,
    price_a: float,
    price_b: float,
    sym_a: str,
    sym_b: str,
    risk_manager: RiskManager,
    order_executor: OrderExecutor,
    bot_state: BotState,
    notifier: TelegramNotifier,
    logger: logging.Logger,
    dev: float,
    z_score: float = 0.0,      # 청산 시점 Z-Score (CSV 로깅용)
):
    """청산 주문 → Net PnL 계산(수수료 차감) → 상태 정리 → 텔레그램 알림 → CSV 기록 → 상태 저장."""
    pos = bot_state.positions.get(prefix)

    # Net PnL 계산 — Taker 수수료(0.2%) 완전 차감
    pnl_usdt, pnl_pct = 0.0, 0.0
    if pos:
        pnl_usdt, pnl_pct = bot_state.calc_pnl(pos, price_a, price_b, LEVERAGE)
        bot_state.record_trade(pnl_usdt)
        bot_state.positions.pop(prefix, None)

    if reason == "STOP_LOSS":
        logger.warning(
            f"[{prefix}] 손절 발동 | dev={dev:+.3f}% | "
            f"Net PnL={pnl_usdt:+.4f} USDT ({pnl_pct:+.3f}%) [수수료 차감]"
        )
    elif reason == "MANUAL":
        logger.info(
            f"[{prefix}] 수동 청산 | Net PnL={pnl_usdt:+.4f} USDT ({pnl_pct:+.3f}%) [수수료 차감]"
        )
    elif reason == "KILL_SWITCH":
        logger.critical(
            f"[{prefix}] 킬스위치 청산 | Net PnL={pnl_usdt:+.4f} USDT [수수료 차감]"
        )
    else:
        logger.info(
            f"[{prefix}] 익절 | 괴리 회귀 | dev={dev:+.3f}% | "
            f"Net PnL={pnl_usdt:+.4f} USDT ({pnl_pct:+.3f}%) [수수료 차감]"
        )

    await order_executor.close_pair(
        sym_a=sym_a, price_a=price_a,
        sym_b=sym_b, price_b=price_b,
        reason=reason, pair_prefix=prefix,
    )
    risk_manager.close_position(prefix)
    await notifier.send_exit(prefix, pnl_usdt, pnl_pct, reason)

    # CSV 로깅 & 상태 저장
    if pos:
        exit_time = _now_kst()
        await log_trade(
            pair=prefix,
            entry_time=pos.entry_time,
            exit_time=exit_time,
            entry_z=pos.entry_z_score,
            exit_z=z_score,
            pnl_usdt=pnl_usdt,
            pnl_pct=pnl_pct,
            reason=reason,
        )
    await save_state(bot_state)


# ─────────────────────────────────────────────────────────────────────────────
# BTC 시장 폭주 감지 모니터 (독립 코루틴 — 30초마다 실행)
# ─────────────────────────────────────────────────────────────────────────────
async def btc_turbulence_monitor(
    exchange: ccxt_async.Exchange,
    bot_state: BotState,
    notifier: TelegramNotifier,
) -> None:
    """
    BTC/USDT 15분봉 변동성을 주기적으로 체크.
    MAX_BTC_VOLATILITY 초과 시 market_turbulent=True → 전 페어 신규 진입 차단.
    변동성 해소 시 market_turbulent=False → 진입 재개.
    """
    logger = logging.getLogger("pair_bot")
    while True:
        try:
            vol_pct = await fetch_btc_volatility(exchange)
            is_turbulent = vol_pct > MAX_BTC_VOLATILITY

            if is_turbulent and not bot_state.market_turbulent:
                bot_state.market_turbulent = True
                logger.warning(
                    f"[시장폭주] BTC 15분 변동성 {vol_pct:.2f}% > {MAX_BTC_VOLATILITY}% "
                    f"— 신규 진입 전면 차단"
                )
                await notifier.send_turbulence_alert(True, vol_pct)

            elif not is_turbulent and bot_state.market_turbulent:
                bot_state.market_turbulent = False
                logger.info(
                    f"[시장안정] BTC 15분 변동성 {vol_pct:.2f}% — 신규 진입 재개"
                )
                await notifier.send_turbulence_alert(False, vol_pct)

        except Exception as e:
            logger.debug(f"[BTC모니터] 예외: {e}")

        await asyncio.sleep(BTC_VOLATILITY_CHECK_INTERVAL)


# ─────────────────────────────────────────────────────────────────────────────
# 글로벌 킬 스위치 — 초기 자본 대비 손실률 검사
# ─────────────────────────────────────────────────────────────────────────────
async def check_kill_switch(

    bot_state: BotState,
    order_executor: OrderExecutor,
    notifier: TelegramNotifier,
    logger: logging.Logger,
):
    """
    현재 잔고를 초기 자본과 비교하여 MAX_DRAWDOWN_LIMIT 초과 시
    전 포지션 강제 청산 + 텔레그램 알림 + 봇 종료.
    """
    if bot_state.kill_switch_triggered:
        return
    if bot_state.initial_balance <= 0:
        return

    current_bal = await order_executor.get_total_balance()
    if current_bal is None:
        return  # 네트워크 오류 등으로 잔고 조회를 실패한 경우 킬스위치 판정 보류

    drawdown = (current_bal - bot_state.initial_balance) / bot_state.initial_balance

    if drawdown > MAX_DRAWDOWN_LIMIT:
        return  # 아직 한도 이내

    # ── 킬 스위치 발동 ──
    bot_state.kill_switch_triggered = True
    bot_state.is_accepting_entries = False
    drawdown_pct = drawdown * 100.0

    logger.critical(
        f"🚨 킬 스위치 발동! drawdown={drawdown_pct:+.2f}% | "
        f"초기={bot_state.initial_balance:.2f} → 현재={current_bal:.2f} USDT"
    )

    # 열린 동안 모두 강제 청산
    for prefix, pos in list(bot_state.positions.items()):
        prices = bot_state.latest_price.get(prefix, (0.0, 0.0))
        try:
            await order_executor.close_pair(
                sym_a=pos.sym_a, price_a=prices[0],
                sym_b=pos.sym_b, price_b=prices[1],
                reason="KILL_SWITCH", pair_prefix=prefix,
            )
        except Exception as e:
            logger.error(f"[{prefix}] 킬스위치 청산 실패: {e}")
        bot_state.positions.pop(prefix, None)

    await notifier.send_kill_switch(drawdown_pct, current_bal)
    logger.critical("킬 스위치: 전 포지션 청산 완료. 봇을 종료합니다.")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# 단일 페어 감시 루프 (Z-Score / 진입 / 청산 핵심 로직 유지)
# ─────────────────────────────────────────────────────────────────────────────
async def pair_loop(
    raw_sym_a: str,
    raw_sym_b: str,
    exchange: ccxt_async.Exchange,
    order_executor: OrderExecutor,
    bot_state: BotState,
    notifier: TelegramNotifier,
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
            # ── 0. 수동 청산 요청 확인 (텔레그램 버튼) ──────────────────────
            if prefix in bot_state.manual_close_requests:
                bot_state.manual_close_requests.discard(prefix)
                if risk_manager.has_position:
                    prices = bot_state.latest_price.get(prefix, (0.0, 0.0))
                    await _execute_close(
                        prefix, "MANUAL", prices[0], prices[1],
                        sym_a, sym_b, risk_manager, order_executor,
                        bot_state, notifier, logger, 0.0,
                    )
                await asyncio.sleep(POLL_INTERVAL_SEC)
                continue

            # ── 1. 가격 조회 ────────────────────────────────────────────────
            price_a, price_b = await asyncio.gather(
                fetch_mid_price(exchange, sym_a),
                fetch_mid_price(exchange, sym_b),
            )

            if math.isnan(price_a) or math.isnan(price_b) or price_b == 0:
                logger.warning(f"[{prefix}] 가격 조회 실패 — 다음 사이클 대기")
                await asyncio.sleep(POLL_INTERVAL_SEC)
                continue

            # 공유 상태 업데이트 (텔레그램 Status 명령에서 사용)
            bot_state.latest_price[prefix] = (price_a, price_b)

            # ── 2. 스프레드 계산 (이동평균 / Z-Score 핵심 로직 유지) ─────────
            state  = spread_engine.update(price_a, price_b)
            signal = state["signal"]
            dev    = state["dev_pct"]
            ratio  = state["ratio"]
            window = spread_engine.window_size

            bot_state.latest_dev[prefix] = dev

            # 주기적 상태 로그 (10초마다 DEBUG → bot.log에 기록)
            if int(time.time()) % 10 == 0:
                pnl_est = (
                    risk_manager.estimate_pnl_pct(ratio)
                    if risk_manager.has_position else 0.0
                )
                logger.debug(
                    f"[{prefix}] ratio={ratio:.6f} | dev={dev:+.3f}% | "
                    f"z={state['z_score']:+.3f} | window={window} | "
                    f"pos={'O' if risk_manager.has_position else '-'} | "
                    f"추정PnL={pnl_est:+.3f}%"
                )

            # ── 3. 동적 포지션 사이징 — 가용 잔고의 14% (핵심 자금 관리 유지) ─
            trade_usdt = 0.0
            if not risk_manager.has_position and signal.startswith("ENTRY"):
                # 봇 정지 상태면 진입 스킵 (기존 포지션 관리는 계속)
                if not bot_state.is_accepting_entries:
                    await asyncio.sleep(POLL_INTERVAL_SEC)
                    continue

                # BTC 시장 폭주 감지 시 신규 진입 차단 (청산 감시는 통과)
                if bot_state.market_turbulent:
                    await asyncio.sleep(POLL_INTERVAL_SEC)
                    continue

                free_bal   = await order_executor.get_free_balance()
                # 페어 총 배분 = 잔고 x 14%, 각 레그(롱/숏) = 그 절반 (7%)
                trade_usdt = (free_bal * ALLOCATION_PER_PAIR) / 2.0
                logger.info(
                    f"[{prefix}] 잔고={free_bal:.2f} USDT | "
                    f"레그당={trade_usdt:.2f} USDT (배분={ALLOCATION_PER_PAIR * 100:.0f}%)"
                )

            # ── 4. 신호 처리 (기존 진입/청산 조건 로직 유지) ─────────────────

            if not risk_manager.has_position:
                if signal == "ENTRY_SHORT_A_LONG_B" and trade_usdt > 0:
                    sizing = risk_manager.calc_qty(price_a, price_b, trade_usdt)
                    logger.info(
                        f"[{prefix}] 진입 시그널 발생 | A Short / B Long | "
                        f"z={state['z_score']:+.3f} dev={dev:+.3f}%"
                    )
                    try:
                        await order_executor.open_pair(
                            sym_a=sym_a, side_a="sell", qty_a=sizing["qty_a"], price_a=price_a,
                            sym_b=sym_b, side_b="buy",  qty_b=sizing["qty_b"], price_b=price_b,
                            pair_prefix=prefix,
                        )
                    except LeggingError as e:
                        logger.error(f"[{prefix}] {e} — 포지션 미기록")
                        await notifier.send_legging_alert(prefix, str(e))
                        await asyncio.sleep(POLL_INTERVAL_SEC)
                        continue
                    risk_manager.open_position("SHORT_A_LONG_B", ratio, trade_usdt, prefix)
                    bot_state.positions[prefix] = PairPosition(
                        prefix=prefix, side="SHORT_A_LONG_B",
                        entry_ratio=ratio, sym_a=sym_a, sym_b=sym_b,
                        price_a=price_a, price_b=price_b, trade_usdt=trade_usdt,
                        entry_time=_now_kst(),
                        entry_z_score=state["z_score"],
                    )
                    await notifier.send_entry(
                        prefix, sym_a, "sell", price_a, sym_b, "buy", price_b, trade_usdt
                    )
                    await save_state(bot_state)  # 진입 직후 상태 저장

                elif signal == "ENTRY_LONG_A_SHORT_B" and trade_usdt > 0:
                    sizing = risk_manager.calc_qty(price_a, price_b, trade_usdt)
                    logger.info(
                        f"[{prefix}] 진입 시그널 발생 | A Long / B Short | "
                        f"z={state['z_score']:+.3f} dev={dev:+.3f}%"
                    )
                    try:
                        await order_executor.open_pair(
                            sym_a=sym_a, side_a="buy",  qty_a=sizing["qty_a"], price_a=price_a,
                            sym_b=sym_b, side_b="sell", qty_b=sizing["qty_b"], price_b=price_b,
                            pair_prefix=prefix,
                        )
                    except LeggingError as e:
                        logger.error(f"[{prefix}] {e} — 포지션 미기록")
                        await notifier.send_legging_alert(prefix, str(e))
                        await asyncio.sleep(POLL_INTERVAL_SEC)
                        continue
                    risk_manager.open_position("LONG_A_SHORT_B", ratio, trade_usdt, prefix)
                    bot_state.positions[prefix] = PairPosition(
                        prefix=prefix, side="LONG_A_SHORT_B",
                        entry_ratio=ratio, sym_a=sym_a, sym_b=sym_b,
                        price_a=price_a, price_b=price_b, trade_usdt=trade_usdt,
                        entry_time=_now_kst(),
                        entry_z_score=state["z_score"],
                    )
                    await notifier.send_entry(
                        prefix, sym_a, "buy", price_a, sym_b, "sell", price_b, trade_usdt
                    )
                    await save_state(bot_state)  # 진입 직후 상태 저장

            else:
                # 청산 / 손절 판단 — 공통 핸들러로 위임
                if signal == "STOP" or risk_manager.should_stop_loss(dev):
                    await _execute_close(
                        prefix, "STOP_LOSS", price_a, price_b,
                        sym_a, sym_b, risk_manager, order_executor,
                        bot_state, notifier, logger, dev,
                        z_score=state["z_score"],
                    )

                elif signal == "EXIT":
                    # 익절 안전장치: Net PnL 산정 후 0 초과 시에만 청산
                    pos = bot_state.positions.get(prefix)
                    if pos:
                        net_pnl, _ = bot_state.calc_pnl(pos, price_a, price_b, LEVERAGE)
                        if net_pnl <= 0:
                            logger.debug(
                                f"[{prefix}] EXIT 시그널 무시 — "
                                f"Net PnL={net_pnl:+.4f} USDT (수수료 미충성)"
                            )
                            await asyncio.sleep(POLL_INTERVAL_SEC)
                            continue
                    await _execute_close(
                        prefix, "TAKE_PROFIT", price_a, price_b,
                        sym_a, sym_b, risk_manager, order_executor,
                        bot_state, notifier, logger, dev,
                        z_score=state["z_score"],
                    )

            # ── 5. 킬 스위치 검사 (30초마다) ────────────────────────────────────
            if int(time.time()) % 30 == 0:
                await check_kill_switch(
                    bot_state, order_executor, notifier, logger
                )

            # ── 6. 다음 사이클 대기 ──────────────────────────────────────────
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
# 메인 루프 — 5개 페어 병렬 + 텔레그램 앱 동시 실행
# ─────────────────────────────────────────────────────────────────────────────
async def main_loop():
    logger = logging.getLogger("pair_bot")

    mode_str = "PAPER TRADING (가상 시뮬레이션)" if IS_PAPER_TRADING else "LIVE TRADING (실계좌)"
    logger.info("=" * 60)
    logger.info(f"  페어 트레이딩 봇 시작 — 모드: {mode_str}")
    logger.info(f"  감시 페어 수: {len(PAIRS_TO_TRADE)}개")
    for sym_a, sym_b in PAIRS_TO_TRADE:
        logger.info(f"    [{make_prefix(sym_a, sym_b):12s}]  {sym_a}  /  {sym_b}")
    logger.info(f"  페어당 배분 비율: {ALLOCATION_PER_PAIR * 100:.0f}%")
    logger.info("=" * 60)

    # 공유 상태 & 실행 객체 초기화
    bot_state      = BotState()
    order_executor = OrderExecutor()
    notifier       = TelegramNotifier(bot_state, order_executor)

    # 가격 조회용 비동기 exchange (공개 REST API 전용 — API 키 불필요)
    # fetch_order_book / fetch_ticker 는 인증 없이 사용 가능한 public 엔드포인트
    exchange = ccxt_async.binanceusdm({
        "options"        : {"defaultType": "future"},
        "enableRateLimit": True,
    })
    logger.info("[거래소] 가격 조회 클라이언트 초기화 완료 (Public API, 인증 없음)")

    # 서버 재구동 시 상태 복구 (bot_state.json 존재 시 자동 로드)
    recovered = await load_state(bot_state)
    if not recovered:
        bot_state.initial_balance = await order_executor.get_free_balance()
        logger.info(f"  초기 잔고: {bot_state.initial_balance:.2f} USDT (킬스위치 기준)")
    else:
        live_bal = await order_executor.get_free_balance()
        logger.info(
            f"  복구 세션 잔고: {live_bal:.2f} USDT "
            f"(저장값={bot_state.initial_balance:.2f} USDT)"
        )
        bot_state.initial_balance = live_bal

    # API 연결 성공 확인 + 잔고 재출력
    confirmed_bal = await order_executor.get_free_balance()
    logger.info("=" * 60)
    logger.info(f"  [API 연결 성공] 모의투자 계좌 가용 잔고: {confirmed_bal:.2f} USDT")
    logger.info(f"  킬스위치 한도: {MAX_DRAWDOWN_LIMIT * 100:.1f}%")
    logger.info("=" * 60)

    # 레버리지 자동 세팅 (실거래 모드일 때 API로 전 페어 일괄 적용)
    await order_executor.setup_leverage()

    # CSV 매매 기록 초기화
    await init_csv()

    # ── 텔레그램 봇 시작 ────────────────────────────────────────────────────
    tg_app = None
    if TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_TOKEN != "YOUR_TELEGRAM_BOT_TOKEN":
        tg_app = notifier.build_app()
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling()
        logger.info("[텔레그램] 봇 폴링 시작")
    else:
        logger.warning("[텔레그램] 토큰 미설정 — 텔레그램 기능 비활성화")

    # ── 5개 페어 루프 + BTC 변동성 모니터 병렬 실행 ──────────────────────────
    tasks = [
        pair_loop(sym_a, sym_b, exchange, order_executor, bot_state, notifier)
        for sym_a, sym_b in PAIRS_TO_TRADE
    ]
    tasks.append(btc_turbulence_monitor(exchange, bot_state, notifier))

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        pass
    finally:
        if tg_app:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        await exchange.close()
        logger.info("교환소 연결 종료. 봇을 정상적으로 종료합니다.")


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    setup_logger()
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass
