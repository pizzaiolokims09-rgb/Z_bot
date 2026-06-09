# =============================================================================
# pair_bot/main.py
# 페어 트레이딩 봇 — 메인 진입점 (20개 페어 병렬 감시 + 텔레그램 연동)
# Phase 1: 공적분 검정 진입 + Maker-Chasing 익절 엔진
# 실행: python main.py
# =============================================================================

import asyncio
import logging
import math
import sys
import time
from collections import deque
from datetime import date, datetime
from logging.handlers import RotatingFileHandler

import ccxt.async_support as ccxt_async
import numpy as np
from statsmodels.tsa.stattools import coint

from config import (
    IS_PAPER_TRADING, PAIRS_TO_TRADE,
    ALLOCATION_PER_PAIR, LEVERAGE, MAX_ACTIVE_PAIRS,
    POLL_INTERVAL_SEC, LOG_FILE,
    TELEGRAM_BOT_TOKEN, MAX_DRAWDOWN_LIMIT,
    API_KEY, API_SECRET, USE_TESTNET,
    MAX_BTC_VOLATILITY, BTC_VOLATILITY_CHECK_INTERVAL,
    STOP_LOSS_COOLDOWN_SEC,
    MAX_STOP_LOSS_PER_PAIR, MAX_HOLD_SECONDS,
    WARMUP_BATCH_SIZE, EXIT_Z_SCORE, TARGET_NET_PNL_PCT,
    TAKER_FEE_RATE, MAKER_FEE_RATE,
    ENTRY_CONFIRMATION_SEC, MAX_LOSS_PCT,
    CORR_WINDOW_MIN, CORR_STOP_THRESHOLD, STOP_LOSS_Z_SCORE,
    ENTRY_MIN_CORRELATION,
    COINT_PVALUE_THRESHOLD, COINT_MIN_SAMPLES,
    SCANNER_INTERVAL_HOURS,
)
from spread_engine      import SpreadEngine
from risk_manager       import RiskManager
from order_executor     import OrderExecutor, LeggingError
from bot_state          import BotState, PairPosition
from telegram_bot       import TelegramNotifier
from trade_logger       import init_csv, log_trade, _now_kst
from state_persistence  import save_state, load_state
from scanner            import DynamicScanner


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


def _swap_pair_in_list(old_prefix: str, new_sym_a: str, new_sym_b: str) -> bool:
    """PAIRS_TO_TRADE 리스트에서 old_prefix에 해당하는 페어를 찾아 새 페어로 교체.
    
    중복 방어: 새 페어가 이미 리스트에 존재하면 추가하지 않음.
    고아 방어: old_prefix를 찾아 제거 후 새 페어로 교체.
    """
    new_prefix = f"{new_sym_a.split('/')[0]}-{new_sym_b.split('/')[0]}"

    # 1단계: old_prefix 위치 탐색
    old_idx = None
    for i, (a, b) in enumerate(PAIRS_TO_TRADE):
        if make_prefix(a, b) == old_prefix:
            old_idx = i
            break

    # 2단계: 새 페어가 이미 리스트에 존재하는지 확인
    new_already_exists = any(
        make_prefix(a, b) == new_prefix for a, b in PAIRS_TO_TRADE
    )

    if old_idx is not None:
        if new_already_exists:
            # 새 페어가 이미 있으면, 기존(old) 자리만 삭제 (중복 방지)
            del PAIRS_TO_TRADE[old_idx]
        else:
            # 기존 자리를 새 페어로 교체
            PAIRS_TO_TRADE[old_idx] = (new_sym_a, new_sym_b)
        return True
    else:
        # old_prefix를 못 찾음 — 새 페어 중복 없으면 추가
        if not new_already_exists:
            PAIRS_TO_TRADE.append((new_sym_a, new_sym_b))
        return False


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
    close_reason: str = "",    # 상세 청산 사유 (텔레그램 알림용)
):
    """청산 주문 → Net PnL 계산(실체결가 기반) → 상태 정리 → 텔레그램 알림 → CSV 기록 → 상태 저장.
    
    ABORTED 반환 시 청산을 포기하고 포지션을 유지(Hold)합니다.
    반환: True=청산 완료, False=ABORTED(포지션 유지)
    """
    pos = bot_state.positions.get(prefix)
    is_maker = (reason == "TAKE_PROFIT")

    # 익절 시 Maker-Chasing, 그 외 시장가(Taker) 청산
    results = None
    if is_maker and pos:
        # Chasing용 포지션 정보 구성 (예상 PnL 가계산에 필요)
        qty_a = (pos.margin_a * LEVERAGE) / pos.price_a
        qty_b = (pos.margin_b * LEVERAGE) / pos.price_b
        common_info = {
            "entry_price_a": pos.price_a,
            "entry_price_b": pos.price_b,
            "margin_a": pos.margin_a,
            "margin_b": pos.margin_b,
            "qty_a": qty_a,
            "qty_b": qty_b,
            "side": pos.side,
        }
        pos_info_a = {**common_info, "leg": "A", "current_price_other": price_b}
        pos_info_b = {**common_info, "leg": "B", "current_price_other": price_a}

        results = await order_executor.close_pair_chase(
            sym_a=sym_a, sym_b=sym_b, pair_prefix=prefix,
            pos_info_a=pos_info_a, pos_info_b=pos_info_b,
        )

        # ── ABORTED 처리: 한쪽이라도 추적 포기하면 청산 전체 포기 (Hold) ──
        if results:
            a_aborted = isinstance(results[0], dict) and results[0].get("status") == "ABORTED"
            b_aborted = isinstance(results[1], dict) and results[1].get("status") == "ABORTED"

            if a_aborted or b_aborted:
                abort_reasons = []
                if a_aborted:
                    abort_reasons.append(f"A: {results[0].get('reason', '?')}")
                if b_aborted:
                    abort_reasons.append(f"B: {results[1].get('reason', '?')}")
                logger.info(
                    f"[{prefix}] Chasing ABORTED → 청산 포기, 포지션 유지(Hold) | "
                    + " | ".join(abort_reasons)
                )
                return False  # ★ 청산 포기 — 포지션 유지
    else:
        results = await order_executor.close_pair(
            sym_a=sym_a, price_a=price_a,
            sym_b=sym_b, price_b=price_b,
            reason=reason, pair_prefix=prefix,
        )

    # 실체결 결과를 바탕으로 Net PnL 계산
    pnl_usdt, pnl_pct = 0.0, 0.0
    if pos:
        # ── 청산 성공 여부 검증 (양쪽 레그 모두 성공해야 포지션 정리) ──
        if not results:
            logger.critical(
                f"[{prefix}] 청산 결과 없음(results=None) — 포지션 유지, 다음 루프에서 재시도"
            )
            await notifier._send(
                f"🚨 [{prefix}] 청산 주문 실패!\n결과 없음 — 포지션이 거래소에 남아있습니다."
            )
            return

        a_failed = isinstance(results[0], Exception)
        b_failed = isinstance(results[1], Exception)

        if a_failed or b_failed:
            failed_info = []
            if a_failed:
                failed_info.append(f"A({sym_a}): {results[0]}")
            if b_failed:
                failed_info.append(f"B({sym_b}): {results[1]}")
            
            # 알림 스팸 방지: 페어당 60초에 한 번만 텔레그램 발송
            import time
            now = time.time()
            if not hasattr(bot_state, "last_error_alerts"):
                bot_state.last_error_alerts = {}
            
            last_alert = bot_state.last_error_alerts.get(prefix, 0)
            if now - last_alert > 60:
                logger.critical(
                    f"[{prefix}] 청산 실패! {' | '.join(failed_info)} — "
                    f"포지션 유지, 다음 루프에서 재시도"
                )
                await notifier._send(
                    f"🚨 [{prefix}] 청산 실패!\n"
                    + "\n".join(failed_info)
                    + "\n포지션이 거래소에 남아있습니다. 수동 확인 필요!"
                )
                bot_state.last_error_alerts[prefix] = now
            return  # ★ 핵심: 포지션 pop 하지 않음 → 다음 루프에서 재시도

        res_a, res_b = results
        pnl_usdt, pnl_pct = bot_state.calc_pnl(pos, res_a, res_b)
        bot_state.record_trade(pnl_usdt, prefix=prefix)
        bot_state.positions.pop(prefix, None)

    if reason == "STOP_LOSS":
        logger.warning(
            f"[{prefix}] 손절 발동 | dev={dev:+.3f}% | "
            f"Net PnL={pnl_usdt:+.4f} USDT ({pnl_pct:+.3f}%) [실체결가 기준]"
        )
    elif reason == "MANUAL":
        logger.info(
            f"[{prefix}] 수동 청산 | Net PnL={pnl_usdt:+.4f} USDT ({pnl_pct:+.3f}%) [실체결가 기준]"
        )
    elif reason == "KILL_SWITCH":
        logger.critical(
            f"[{prefix}] 킬스위치 청산 | Net PnL={pnl_usdt:+.4f} USDT [실체결가 기준]"
        )
    else:
        reason_tag = close_reason if close_reason else "괴리 회귀"
        logger.info(
            f"[{prefix}] 익절 | {reason_tag} | dev={dev:+.3f}% | "
            f"Net PnL={pnl_usdt:+.4f} USDT ({pnl_pct:+.3f}%) [실체결가 기준]"
        )

    risk_manager.close_position(prefix)
    await notifier.send_exit(prefix, pnl_usdt, pnl_pct, reason, close_reason=close_reason)

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
# 워밍업 데이터 조회 (과거 1m 봉 가져와서 윈도우 채우기)
# ─────────────────────────────────────────────────────────────────────────────
async def prefetch_warmup_data(
    exchange: ccxt_async.Exchange, sym_a: str, sym_b: str, 
    spread_engine: SpreadEngine, prefix: str,
    corr_prices_a: deque = None, corr_prices_b: deque = None
):
    """
    1분봉 1440개(24시간 분량)를 가져와서 10초 폴링(1분에 6개) 효과를 내도록
    듀얼 윈도우(스윙 8640 / 단기 4320)에 채워 넣습니다.
    """
    logger = logging.getLogger("pair_bot")
    try:
        limit = 1440  # 24시간 = 1440분
        # 비동기로 A, B 심볼 1분봉 가져오기
        ohlcv_a, ohlcv_b = await asyncio.gather(
            exchange.fetch_ohlcv(sym_a, timeframe="1m", limit=limit),
            exchange.fetch_ohlcv(sym_b, timeframe="1m", limit=limit),
            return_exceptions=True
        )
        
        if isinstance(ohlcv_a, Exception) or isinstance(ohlcv_b, Exception):
            logger.warning(f"[{prefix}] 과거 데이터 조회 실패 (예외 발생) - 워밍업 건너뜀")
            return
            
        if not ohlcv_a or not ohlcv_b:
            logger.warning(f"[{prefix}] 과거 데이터 없음 - 워밍업 건너뜀")
            return
            
        # 시간축 맞추기 위해 가장 최근 N개 맞춤
        min_len = min(len(ohlcv_a), len(ohlcv_b))
        ohlcv_a = ohlcv_a[-min_len:]
        ohlcv_b = ohlcv_b[-min_len:]
        
        # 4번 인덱스가 종가(Close)
        for i in range(min_len):
            price_a = ohlcv_a[i][4]
            price_b = ohlcv_b[i][4]
            if price_b > 0:
                # 1분(60초) = 3초 폴링 * 20회 반복 삽입으로 가중치 맞춤
                for _ in range(20):
                    spread_engine.update(price_a, price_b)
                
                # 상관계수 계산용 1분봉 데이터도 함께 채움
                if corr_prices_a is not None:
                    corr_prices_a.append(price_a)
                if corr_prices_b is not None:
                    corr_prices_b.append(price_b)
            
            # 이벤트 루프 블로킹 방지 (텔레그램 타임아웃 방어)
            if i % 100 == 0:
                await asyncio.sleep(0)
                
        logger.info(f"[{prefix}] 과거 데이터 {min_len}분치 로드 완료 (window={spread_engine.window_size}) -> 즉시 거래 가능!")
    except Exception as e:
        logger.warning(f"[{prefix}] 워밍업 데이터 로드 중 오류: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# 다이내믹 스캐너 전용: 즉시 주입 워밍업 (워밍업 트랩 파괴)
# ─────────────────────────────────────────────────────────────────────────────
async def pre_warmup_pair(
    exchange: ccxt_async.Exchange, sym_a: str, sym_b: str,
    prefix: str, bot_state: BotState,
) -> bool:
    """
    스캐너가 새 페어를 발굴할 때 pair_loop 생성 직전에 호출합니다.
    1분봉 1440개(24시간)를 가져와 SpreadEngine에 주입한 뒤,
    이 SpreadEngine을 pair_loop가 재사용하도록 bot_state에 캐싱합니다.
    
    pair_loop는 시작 시 이 캐시를 발견하면 자체 워밍업을 스킵하고
    즉시 매매 감시 루프에 진입합니다.
    
    반환: True=성공, False=실패 (pair_loop가 자체 워밍업 수행)
    """
    logger = logging.getLogger("pair_bot")
    try:
        futures_a = to_futures_symbol(sym_a)
        futures_b = to_futures_symbol(sym_b)
        limit = 1440  # 24시간 = 1440분

        ohlcv_a, ohlcv_b = await asyncio.gather(
            exchange.fetch_ohlcv(futures_a, timeframe="1m", limit=limit),
            exchange.fetch_ohlcv(futures_b, timeframe="1m", limit=limit),
            return_exceptions=True,
        )

        if isinstance(ohlcv_a, Exception) or isinstance(ohlcv_b, Exception):
            logger.warning(f"[PreWarmup][{prefix}] OHLCV 조회 실패 - pair_loop 자체 워밍업으로 대체")
            return False

        if not ohlcv_a or not ohlcv_b:
            logger.warning(f"[PreWarmup][{prefix}] OHLCV 데이터 없음")
            return False

        min_len = min(len(ohlcv_a), len(ohlcv_b))
        ohlcv_a = ohlcv_a[-min_len:]
        ohlcv_b = ohlcv_b[-min_len:]

        # SpreadEngine을 미리 생성하여 윈도우를 100% 채움
        engine = SpreadEngine()
        for i in range(min_len):
            price_a = ohlcv_a[i][4]
            price_b = ohlcv_b[i][4]
            if price_b > 0:
                # 1분(60초) = 3초 폴링 * 20회 반복 삽입
                for _ in range(20):
                    engine.update(price_a, price_b)

            # 이벤트 루프 블로킹 방지
            if i % 100 == 0:
                await asyncio.sleep(0)

        # bot_state에 캐싱 → pair_loop가 이 엔진을 가져다 씀
        bot_state.prewarmed_engines[prefix] = engine

        logger.info(
            f"[PreWarmup][{prefix}] 즉시 주입 완료 | "
            f"{min_len}분 데이터 → window={engine.window_size} | "
            f"즉시 매매 가능 상태로 pair_loop 시작"
        )
        return True

    except Exception as e:
        logger.warning(f"[PreWarmup][{prefix}] 사전 워밍업 오류: {e}")
        return False

# ─────────────────────────────────────────────────────────────────────────────
# 피어슨 상관계수 헬퍼 (스마트 손절용 — 유지)
# ─────────────────────────────────────────────────────────────────────────────
def calc_pearson_corr(prices_a: deque, prices_b: deque) -> float:
    """
    두 가격 시계열의 피어슨 상관계수를 수동 계산합니다.
    데이터가 부족하면(20개 미만) 1.0을 반환하여 손절을 방지합니다.
    """
    n = min(len(prices_a), len(prices_b))
    if n < 20:  # 최소 20분치 데이터가 있어야 유의미한 상관계수
        return 1.0

    a_list = list(prices_a)[-n:]
    b_list = list(prices_b)[-n:]

    sum_a = sum(a_list)
    sum_b = sum(b_list)
    sum_ab = sum(a * b for a, b in zip(a_list, b_list))
    sum_a2 = sum(a * a for a in a_list)
    sum_b2 = sum(b * b for b in b_list)

    numerator = n * sum_ab - sum_a * sum_b
    denom_a = n * sum_a2 - sum_a ** 2
    denom_b = n * sum_b2 - sum_b ** 2

    if denom_a <= 0 or denom_b <= 0:
        return 1.0  # 분산이 0이면 상관계수 계산 불가 → 안전 반환

    return numerator / (denom_a ** 0.5 * denom_b ** 0.5)


# ─────────────────────────────────────────────────────────────────────────────
# 공적분(Cointegration) 검정 헬퍼 (진입 필터 핵심 엔진)
# ─────────────────────────────────────────────────────────────────────────────
def calc_cointegration_pvalue(
    prices_a: deque, prices_b: deque,
    min_samples: int = COINT_MIN_SAMPLES,
) -> float:
    """
    두 가격 시계열의 Engle-Granger 공적분 검정 p-value를 반환합니다.

    p-value가 낮을수록(0.05 이하) 두 시계열이 장기적으로
    평균 회귀하는 관계(공적분 관계)에 있을 가능성이 높습니다.

    - 데이터가 min_samples 미만이면 1.0 반환 (진입 차단)
    - 계산 예외 발생 시에도 1.0 반환 (안전 우선)
    """
    n = min(len(prices_a), len(prices_b))
    if n < min_samples:
        return 1.0  # 데이터 부족 → 진입 차단

    try:
        a_arr = np.array(list(prices_a)[-n:], dtype=np.float64)
        b_arr = np.array(list(prices_b)[-n:], dtype=np.float64)

        # Engle-Granger 2단계 공적분 검정
        # 반환: (t-statistic, p-value, crit_values)
        _, pvalue, _ = coint(a_arr, b_arr)
        return float(pvalue)
    except Exception:
        return 1.0  # 계산 실패 → 안전하게 진입 차단


# ─────────────────────────────────────────────────────────────────────────────
# 전체 티커 캐싱 루프 (Dual Loop 아키텍처)
# ─────────────────────────────────────────────────────────────────────────────
async def global_ticker_loop(exchange: ccxt_async.Exchange, bot_state: BotState):
    logger = logging.getLogger("pair_bot")
    logger.info(f"Global Ticker Loop 시작 (주기: {POLL_INTERVAL_SEC}초)")
    while True:
        try:
            tickers = await exchange.fetch_tickers()
            if tickers:
                bot_state.global_tickers = tickers
        except Exception as e:
            logger.warning(f"[GlobalTicker] 티커 조회 실패: {e}")
        await asyncio.sleep(POLL_INTERVAL_SEC)


# ─────────────────────────────────────────────────────────────────────────────
# 단일 페어 감시 루프
async def pair_loop(
    raw_sym_a: str,
    raw_sym_b: str,
    exchange: ccxt_async.Exchange,
    order_executor: OrderExecutor,
    bot_state: BotState,
    notifier: TelegramNotifier,
    entry_lock: asyncio.Lock,
):
    """
    한 페어에 대한 독립적인 감시/매매 루프.
    SpreadEngine, RiskManager 인스턴스가 페어별로 완전히 분리됩니다.
    """
    logger = logging.getLogger("pair_bot")
    sym_a  = to_futures_symbol(raw_sym_a)
    sym_b  = to_futures_symbol(raw_sym_b)
    prefix = make_prefix(raw_sym_a, raw_sym_b)

    # ── 사전 워밍업 캐시 확인 (다이내믹 스캐너 경유 시 즉시 매매 가능) ──
    cached_engine = bot_state.prewarmed_engines.pop(prefix, None)
    if cached_engine is not None:
        spread_engine = cached_engine  # 이미 24시간치 데이터로 100% 채워진 엔진
        logger.info(f"[{prefix}] 사전 워밍업 캐시 사용 → 즉시 매매 가능!")
    else:
        spread_engine = SpreadEngine()  # 이 페어 전용 Z-Score 계산기

    risk_manager  = RiskManager()    # 이 페어 전용 포지션 상태

    entry_timestamp = 0.0      # 포지션 진입 시각 (time.time() 기반, 시간 기반 청산용)
    entry_confirm_ts = 0.0     # 30초 확인 매매: Z-Score가 문턴을 처음 넘은 시각
    reconnect_wait = 5

    # ── 스마트 손절용: 1분봉 종가 히스토리 (피어슨 상관계수 계산) ──
    corr_prices_a: deque = deque(maxlen=CORR_WINDOW_MIN)  # 최근 60분 A코인 종가
    corr_prices_b: deque = deque(maxlen=CORR_WINDOW_MIN)  # 최근 60분 B코인 종가
    corr_sample_counter = 0  # 3초 폴링을 1분 단위로 다운샘플링하기 위한 카운터
    last_pnl_fetch_time = 0.0  # 초기 1회는 즉시 조회하도록 0으로 설정

    # ── 봇 상태(bot_state)에서 기존 포지션 복구 ──
    if prefix in bot_state.positions:
        pos = bot_state.positions[prefix]
        risk_manager.position_side = pos.side
        risk_manager.entry_ratio = pos.entry_ratio
        risk_manager.entry_notional = pos.total_margin
        try:
            if isinstance(pos.entry_time, str):
                dt = datetime.strptime(pos.entry_time, "%Y-%m-%d %H:%M:%S")
                entry_timestamp = dt.timestamp()
            else:
                entry_timestamp = pos.entry_time.timestamp()
            logger.info(f"[{prefix}] 로컬 상태(RiskManager) 복구 완료: {pos.side}, ratio={pos.entry_ratio:.5f}")
        except Exception as e:
            logger.error(f"[{prefix}] 진입 시간 복구 실패: {e}")

    # ── 워밍업 데이터 프리패치 (캐시 없는 경우에만) ──
    if cached_engine is None:
        await prefetch_warmup_data(
            exchange, sym_a, sym_b, spread_engine, prefix,
            corr_prices_a=corr_prices_a, corr_prices_b=corr_prices_b
        )

    while True:
        try:
            # ── 감시 일시 정지(Paused) 페어 스킵 ──────────────────────────
            # (단, 활성 포지션이 남아 있으면 청산 감시를 위해 절대 스킵하지 않음)
            if prefix in bot_state.paused_pairs and prefix not in bot_state.positions:
                await asyncio.sleep(10)  # 10초 간격으로 재확인 (CPU 절약)
                continue

            # ── 자정 리셋 (일일 손절 카운터) ───────────────────────────────
            today_str = date.today().isoformat()
            if bot_state.daily_reset_date != today_str:
                bot_state.daily_stop_counts.clear()
                bot_state.daily_reset_date = today_str
                logger.info("일일 손절 카운터 전체 리셋")

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
                    # 청산 직후 pending_swap 바통 터치
                    if prefix in bot_state.pending_swaps:
                        new_a, new_b = bot_state.pending_swaps.pop(prefix)
                        new_prefix = make_prefix(new_a, new_b)
                        _swap_pair_in_list(prefix, new_a, new_b)
                        await save_state(bot_state)
                        await notifier._send(f"🔄 [{prefix}] → [{new_prefix}] 바통 터치! 새 페어 워밍업 시작")
                        asyncio.create_task(
                            pair_loop(new_a, new_b, exchange, order_executor, bot_state, notifier, entry_lock)
                        )
                        logger.info(f"[PendingSwap] {prefix} → {new_prefix} 교체 완료, 이 루프 종료")
                        return
                await asyncio.sleep(POLL_INTERVAL_SEC)
                continue

            # ── 1. 가격 조회 (Global Ticker 캐시 사용) ─────────────────────────
            ticker_a = bot_state.global_tickers.get(sym_a)
            ticker_b = bot_state.global_tickers.get(sym_b)
            
            if not ticker_a or not ticker_b or not ticker_a.get('last') or not ticker_b.get('last'):
                # 아직 캐시가 안 차오른 경우 대기
                await asyncio.sleep(POLL_INTERVAL_SEC)
                continue
                
            price_a = float(ticker_a['last'])
            price_b = float(ticker_b['last'])
            
            if math.isnan(price_a) or math.isnan(price_b) or price_b == 0:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                continue

            # 공유 상태 업데이트 (텔레그램 Status 명령에서 사용)
            bot_state.latest_price[prefix] = (price_a, price_b)

            # ── 스마트 손절용: 1분 단위 가격 샘플링 (3초 폴링 x 20 = 1분) ──
            corr_sample_counter += 1
            if corr_sample_counter >= 20:
                corr_sample_counter = 0
                corr_prices_a.append(price_a)
                corr_prices_b.append(price_b)

            # ── 2. 스프레드 계산 (이동평균 / Z-Score 핵심 로직 유지) ─────────
            state  = spread_engine.update(price_a, price_b)
            signal = state["signal"]
            dev    = state["dev_pct"]
            ratio  = state["ratio"]
            window = spread_engine.window_size

            bot_state.latest_dev[prefix] = dev
            bot_state.latest_corr[prefix] = calc_pearson_corr(corr_prices_a, corr_prices_b)

            # 주기적 상태 로그 (60초마다 DEBUG → bot.log에 기록)
            if int(time.time()) % 60 == 0:
                if risk_manager.has_position and prefix in bot_state.positions:
                    pos_ = bot_state.positions[prefix]
                    _, pnl_est = bot_state.calc_gross_pnl(pos_, price_a, price_b, LEVERAGE)
                else:
                    pnl_est = 0.0
                logger.debug(
                    f"[{prefix}] ratio={ratio:.6f} | dev={dev:+.3f}% | "
                    f"zSw={state['z_score_swing']:+.3f} zSh={state['z_score_short']:+.3f} | "
                    f"win={window} | slopeN={state['normalized_slope_pct']:.4f}% | "
                    f"trend={'Y' if state['is_trending'] else 'N'} | "
                    f"pos={'O' if risk_manager.has_position else '-'} | "
                    f"PnL={pnl_est:+.3f}% | SL={bot_state.daily_stop_counts.get(prefix, 0)}/{MAX_STOP_LOSS_PER_PAIR}"
                )

            # ── 3. 동적 포지션 사이징 — 가용 잔고의 14% (핵심 자금 관리 유지) ─
            total_margin = 0.0

            # Z-Score가 문턱 아래로 내려가면 30초 확인 타이머 리셋
            if not signal.startswith("ENTRY") and entry_confirm_ts > 0.0:
                logger.debug(f"[{prefix}] Z-Score 문턱 이탈 → {ENTRY_CONFIRMATION_SEC}초 확인 타이머 리셋")
                entry_confirm_ts = 0.0

            if not risk_manager.has_position and signal.startswith("ENTRY"):
                # 봇 정지 상태면 진입 스킵 (기존 포지션 관리는 계속)
                if not bot_state.is_accepting_entries:
                    entry_confirm_ts = 0.0  # 타이머 초기화
                    await asyncio.sleep(POLL_INTERVAL_SEC)
                    continue

                # BTC 시장 폭주 감지 시 신규 진입 차단 (청산 감시는 통과)
                if bot_state.market_turbulent:
                    entry_confirm_ts = 0.0  # 타이머 초기화
                    await asyncio.sleep(POLL_INTERVAL_SEC)
                    continue

                # ── 1단계 슬롯/쿨다운 사전 검증 (조건 미달 시 30초 필터 시작 안 함) ──
                # 손절 후 재진입 쿨다운
                elapsed = time.time() - bot_state.cooldowns.get(prefix, 0.0)
                if elapsed < STOP_LOSS_COOLDOWN_SEC:
                    entry_confirm_ts = 0.0
                    await asyncio.sleep(POLL_INTERVAL_SEC)
                    continue

                # 일일 손절 횟수 제한
                if bot_state.daily_stop_counts.get(prefix, 0) >= MAX_STOP_LOSS_PER_PAIR:
                    entry_confirm_ts = 0.0
                    await asyncio.sleep(POLL_INTERVAL_SEC)
                    continue

                # 활성 슬롯 체크 (미리 검사)
                if len(bot_state.positions) >= MAX_ACTIVE_PAIRS:
                    entry_confirm_ts = 0.0
                    await asyncio.sleep(POLL_INTERVAL_SEC)
                    continue

                # ── 진입 공적분 검정 사전 검증 (평균 회귀성 수학적 증명) ──────
                coint_pvalue = calc_cointegration_pvalue(
                    corr_prices_a, corr_prices_b, min_samples=COINT_MIN_SAMPLES
                )
                bot_state.latest_coint_pvalue[prefix] = coint_pvalue
                if coint_pvalue > COINT_PVALUE_THRESHOLD:
                    entry_confirm_ts = 0.0
                    logger.debug(
                        f"[{prefix}] 진입 차단 — 공적분 p-value={coint_pvalue:.4f} > "
                        f"{COINT_PVALUE_THRESHOLD} (평균 회귀 미증명)"
                    )
                    await asyncio.sleep(POLL_INTERVAL_SEC)
                    continue

                # ── 30초 확인 매매 필터 (Whipsaw Prevention) ──────────────────
                now_ts = time.time()
                if entry_confirm_ts == 0.0:
                    # Z-Score가 진입 문턴을 처음 넘은 시점 → 타이머 시작
                    entry_confirm_ts = now_ts
                    logger.info(
                        f"[{prefix}] {ENTRY_CONFIRMATION_SEC}초 확인 필터 시작 | z={state['z_score']:+.3f} | "
                        f"corr={entry_corr:.3f} | 신호={signal}"
                    )
                    await asyncio.sleep(POLL_INTERVAL_SEC)
                    continue

                elapsed_confirm = now_ts - entry_confirm_ts
                if elapsed_confirm < ENTRY_CONFIRMATION_SEC:
                    remaining = ENTRY_CONFIRMATION_SEC - elapsed_confirm
                    logger.debug(
                        f"[{prefix}] 확인 대기 중 | {elapsed_confirm:.0f}/{ENTRY_CONFIRMATION_SEC}초 | "
                        f"남은={remaining:.0f}초"
                    )
                    await asyncio.sleep(POLL_INTERVAL_SEC)
                    continue

                # 확인 시간 경과 → 진짜 타점으로 인정! 타이머 초기화 후 진입 진행
                logger.info(
                    f"[{prefix}] {ENTRY_CONFIRMATION_SEC}초 확인 필터 통과! | z={state['z_score']:+.3f} | "
                    f"corr={entry_corr:.3f} | 신호={signal} | 진입 실행"
                )
                entry_confirm_ts = 0.0

                # 2단계 슬롯 체크 (30초 대기 중 다른 페어가 진입했을 수 있으므로 락 걸고 최종 확인)
                should_skip = False
                async with entry_lock:
                    if len(bot_state.positions) >= MAX_ACTIVE_PAIRS:
                        logger.debug(
                            f"[{prefix}] 슬롯 꽉 차서 진입 스킵 (활성={len(bot_state.positions)}/{MAX_ACTIVE_PAIRS})"
                        )
                        should_skip = True

                if should_skip:
                    await asyncio.sleep(POLL_INTERVAL_SEC)
                    continue

                # 2단계 실제 진입 주문 실행 (주문 완료 및 bot_state.positions 등록 완료 시까지 락 점유)
                async with entry_lock:
                    # 락 획득 시점 재검증
                    if len(bot_state.positions) >= MAX_ACTIVE_PAIRS:
                        logger.debug(
                            f"[{prefix}] 진입 직전 슬롯 포화 감지 (활성={len(bot_state.positions)}/{MAX_ACTIVE_PAIRS})"
                        )
                        await asyncio.sleep(POLL_INTERVAL_SEC)
                        continue

                    free_bal   = await order_executor.get_free_balance()
                    # 페어 총 배분 = 잔고 x 14% (두 레그 합산 총액)
                    total_margin = free_bal * ALLOCATION_PER_PAIR
                    logger.info(
                        f"[{prefix}] 잔고={free_bal:.2f} USDT | "
                        f"페어총액={total_margin:.2f} USDT (배분={ALLOCATION_PER_PAIR * 100:.0f}%)"
                    )

                    # 변동성 가중치 계산 (워밍업 미완료 시 50:50 Fallback)
                    vol_a, vol_b = spread_engine.get_volatilities()

                    # ── 4. 신호 처리 (기존 진입 조건 로직 유지) ─────────────────
                    if signal == "ENTRY_SHORT_A_LONG_B" and total_margin > 0:
                        sizing = risk_manager.calc_qty(price_a, price_b, total_margin, vol_a, vol_b)
                        if sizing is None:
                            logger.warning(f"[{prefix}] Min Notional 미달 — 진입 스킵")
                            await asyncio.sleep(POLL_INTERVAL_SEC)
                            continue
                        logger.info(
                            f"[{prefix}] 진입 시그널 발생 | A Short / B Long | "
                            f"z={state['z_score']:+.3f} dev={dev:+.3f}%"
                        )
                        logger.info(
                            f"[{prefix}] 변동성 헷징: vol_A={vol_a:.6f} vol_B={vol_b:.6f} | "
                            f"배분 A={sizing['margin_a']:.1f} B={sizing['margin_b']:.1f} USDT"
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
                        risk_manager.open_position("SHORT_A_LONG_B", ratio, sizing['margin_a'] + sizing['margin_b'], prefix)
                        bot_state.positions[prefix] = PairPosition(
                            prefix=prefix, side="SHORT_A_LONG_B",
                            entry_ratio=ratio, sym_a=sym_a, sym_b=sym_b,
                            price_a=price_a, price_b=price_b,
                            margin_a=sizing['margin_a'], margin_b=sizing['margin_b'],
                            entry_time=_now_kst(),
                            entry_z_score=state["z_score"],
                        )
                        entry_timestamp = time.time()
                        await notifier.send_entry(
                            prefix, sym_a, "sell", price_a, sym_b, "buy", price_b,
                            sizing['margin_a'], sizing['margin_b'],
                        )
                        await save_state(bot_state)

                    elif signal == "ENTRY_LONG_A_SHORT_B" and total_margin > 0:
                        sizing = risk_manager.calc_qty(price_a, price_b, total_margin, vol_a, vol_b)
                        if sizing is None:
                            logger.warning(f"[{prefix}] Min Notional 미달 — 진입 스킵")
                            await asyncio.sleep(POLL_INTERVAL_SEC)
                            continue
                        logger.info(
                            f"[{prefix}] 진입 시그널 발생 | A Long / B Short | "
                            f"z={state['z_score']:+.3f} dev={dev:+.3f}%"
                        )
                        logger.info(
                            f"[{prefix}] 변동성 헷징: vol_A={vol_a:.6f} vol_B={vol_b:.6f} | "
                            f"배분 A={sizing['margin_a']:.1f} B={sizing['margin_b']:.1f} USDT"
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
                        risk_manager.open_position("LONG_A_SHORT_B", ratio, sizing['margin_a'] + sizing['margin_b'], prefix)
                        bot_state.positions[prefix] = PairPosition(
                            prefix=prefix, side="LONG_A_SHORT_B",
                            entry_ratio=ratio, sym_a=sym_a, sym_b=sym_b,
                            price_a=price_a, price_b=price_b,
                            margin_a=sizing['margin_a'], margin_b=sizing['margin_b'],
                            entry_time=_now_kst(),
                            entry_z_score=state["z_score"],
                        )
                        entry_timestamp = time.time()
                        await notifier.send_entry(
                            prefix, sym_a, "buy", price_a, sym_b, "sell", price_b,
                            sizing['margin_a'], sizing['margin_b'],
                        )
                        await save_state(bot_state)

            elif risk_manager.has_position:
                # ── 시간 기반 강제 청산 (MAX_HOLD_SECONDS 초과 + 손실 시) ────
                if entry_timestamp > 0:
                    hold_sec = time.time() - entry_timestamp
                    if hold_sec >= MAX_HOLD_SECONDS:
                        pos = bot_state.positions.get(prefix)
                        if pos:
                            res_a = {"average": price_a, "qty": (pos.margin_a * LEVERAGE)/pos.price_a, "maker": False}
                            res_b = {"average": price_b, "qty": (pos.margin_b * LEVERAGE)/pos.price_b, "maker": False}
                            net_pnl, _ = bot_state.calc_pnl(pos, res_a, res_b)
                            if net_pnl <= 0:
                                logger.warning(
                                    f"[{prefix}] 보유 시간 초과 ({hold_sec:.0f}초) + "
                                    f"손실({net_pnl:+.2f} USDT) → 강제 청산"
                                )
                                await _execute_close(
                                    prefix, "STOP_LOSS", price_a, price_b,
                                    sym_a, sym_b, risk_manager, order_executor,
                                    bot_state, notifier, logger, dev,
                                    z_score=state["z_score"],
                                )
                                bot_state.cooldowns[prefix] = time.time()
                                bot_state.daily_stop_counts[prefix] = bot_state.daily_stop_counts.get(prefix, 0) + 1
                                await save_state(bot_state)
                                entry_timestamp = 0.0
                                # 청산 직후 pending_swap 바통 터치
                                if prefix in bot_state.pending_swaps:
                                    new_a, new_b = bot_state.pending_swaps.pop(prefix)
                                    new_prefix = make_prefix(new_a, new_b)
                                    _swap_pair_in_list(prefix, new_a, new_b)
                                    await save_state(bot_state)
                                    await notifier._send(f"🔄 [{prefix}] → [{new_prefix}] 바통 터치! 새 페어 워밍업 시작")
                                    asyncio.create_task(
                                        pair_loop(new_a, new_b, exchange, order_executor, bot_state, notifier, entry_lock)
                                    )
                                    logger.info(f"[PendingSwap] {prefix} → {new_prefix} 교체 완료, 이 루프 종료")
                                    return
                                await asyncio.sleep(POLL_INTERVAL_SEC)
                                continue

                # ══════════════════════════════════════════════════════════
                # 실시간 청산 감시 — 통합 우선순위 체인
                # 순서: 익절 → 스마트손절(상관관계) → 하드스탑 → Z-Score손절 → Z-Score익절
                # ══════════════════════════════════════════════════════════
                pos = bot_state.positions.get(prefix)
                if pos:
                    # ── 공통: 미실현 Net PnL 계산 (거래소 unrealizedPnl 직접 조회) ──
                    unrealized_pct = 0.0
                    pnl_calc_ok = False
                    upnl_a = 0.0
                    upnl_b = 0.0
                    gross_pnl = 0.0
                    total_fee = 0.0
                    unrealized_net = 0.0
                    
                    now = time.time()
                    if now - last_pnl_fetch_time >= 15.0:
                        try:
                            if IS_PAPER_TRADING:
                                raise Exception("Paper Trading Mode (API 스킵)")

                            positions_raw = await asyncio.get_running_loop().run_in_executor(
                                None, order_executor._exchange.fetch_positions
                            )
                            upnl_a = 0.0
                            upnl_b = 0.0
                            for p in positions_raw:
                                sym = p.get("symbol", "")
                                contracts = abs(float(p.get("contracts", 0)))
                                if contracts == 0:
                                    continue
                                raw_upnl = float(p.get("unrealizedPnl", 0.0))
                                base_sym = sym.split(':')[0]
                                if base_sym == sym_a.split(':')[0]:
                                    upnl_a = raw_upnl
                                elif base_sym == sym_b.split(':')[0]:
                                    upnl_b = raw_upnl

                            gross_pnl = upnl_a + upnl_b
                            qty_a = (pos.margin_a * LEVERAGE) / pos.price_a
                            qty_b = (pos.margin_b * LEVERAGE) / pos.price_b
                            entry_notional = (qty_a * pos.price_a) + (qty_b * pos.price_b)
                            exit_notional  = (qty_a * price_a) + (qty_b * price_b)
                            total_fee = (entry_notional * TAKER_FEE_RATE) + (exit_notional * MAKER_FEE_RATE)
                            unrealized_net = gross_pnl - total_fee
                            total_margin   = pos.margin_a + pos.margin_b
                            unrealized_pct = (unrealized_net / total_margin * 100) if total_margin > 0 else 0.0
                            pnl_calc_ok = True
                            last_pnl_fetch_time = now
                        except Exception as e:
                            logger.warning(f"[{prefix}] 거래소 PnL 조회 실패 → 자체 수식 Fallback 발동: {e}")

                    if not pnl_calc_ok:
                        # 15초 쿨타임 중이거나 API 실패 시: 자체 수식 Fallback 발동 (API Weight 절감)
                        try:
                            fb_gross, _ = bot_state.calc_gross_pnl(pos, price_a, price_b, LEVERAGE)
                            fb_qty_a = (pos.margin_a * LEVERAGE) / pos.price_a
                            fb_qty_b = (pos.margin_b * LEVERAGE) / pos.price_b
                            fb_entry_notional = (fb_qty_a * pos.price_a) + (fb_qty_b * pos.price_b)
                            fb_exit_notional  = (fb_qty_a * price_a) + (fb_qty_b * price_b)
                            fb_total_fee = (fb_entry_notional * TAKER_FEE_RATE) + (fb_exit_notional * TAKER_FEE_RATE)
                            unrealized_net = fb_gross - fb_total_fee
                            total_margin   = pos.margin_a + pos.margin_b
                            unrealized_pct = (unrealized_net / total_margin * 100) if total_margin > 0 else 0.0
                            pnl_calc_ok = True
                            # logger를 debug로 낮추어 3초마다 로그 도배되는 것을 방지합니다.
                            logger.debug(
                                f"[{prefix}] Fallback PnL: gross={fb_gross:+.4f} "
                                f"fee={fb_total_fee:.4f} net={unrealized_net:+.4f} USDT ({unrealized_pct:+.2f}%)"
                            )
                        except Exception as e2:
                            logger.error(f"[{prefix}] Fallback PnL 계산도 실패 (방어막 무력화): {e2}")

                    # ── 공통: 상관계수 가져오기 ──
                    corr = bot_state.latest_corr.get(prefix, 0.0)

                    # ── 우선순위 1: 익절 (+3.5%) ──
                    if pnl_calc_ok and unrealized_pct >= TARGET_NET_PNL_PCT:
                        logger.info(
                            f"[{prefix}] 목표 순수익 도달! "
                            f"upnl_A={upnl_a:+.4f} upnl_B={upnl_b:+.4f} "
                            f"gross={gross_pnl:+.4f} fee={total_fee:.4f} "
                            f"net={unrealized_net:+.4f} USDT ({unrealized_pct:+.2f}%)"
                        )
                        close_result = await _execute_close(
                            prefix, "TAKE_PROFIT", price_a, price_b,
                            sym_a, sym_b, risk_manager, order_executor,
                            bot_state, notifier, logger, dev,
                            z_score=state["z_score"],
                            close_reason=f"목표 순수익(+{TARGET_NET_PNL_PCT}%) 도달 익절",
                        )
                        # Chasing ABORTED 시 포지션 유지, 다음 루프에서 재시도
                        if close_result is False:
                            await asyncio.sleep(POLL_INTERVAL_SEC)
                            continue
                        entry_timestamp = 0.0
                        if prefix in bot_state.pending_swaps:
                            new_a, new_b = bot_state.pending_swaps.pop(prefix)
                            new_prefix = make_prefix(new_a, new_b)
                            _swap_pair_in_list(prefix, new_a, new_b)
                            await save_state(bot_state)
                            await notifier._send(f"🔄 [{prefix}] → [{new_prefix}] 바통 터치!")
                            asyncio.create_task(pair_loop(new_a, new_b, exchange, order_executor, bot_state, notifier, entry_lock))
                            return
                        await asyncio.sleep(POLL_INTERVAL_SEC)
                        continue

                    # ── 우선순위 2: 스마트 손절 — 상관관계 붕괴 (corr <= 0) ──
                    elif corr <= CORR_STOP_THRESHOLD and len(corr_prices_a) >= 20:
                        logger.warning(
                            f"[{prefix}] 스마트 손절 발동! 단기 상관계수={corr:.4f} <= {CORR_STOP_THRESHOLD} "
                            f"(최근 {len(corr_prices_a)}분 데이터)"
                        )
                        await _execute_close(
                            prefix, "STOP_LOSS", price_a, price_b,
                            sym_a, sym_b, risk_manager, order_executor,
                            bot_state, notifier, logger, dev,
                            z_score=state["z_score"],
                            close_reason=f"단기 상관관계 붕괴(corr={corr:.3f} <= 0) 손절",
                        )
                        bot_state.cooldowns[prefix] = time.time()
                        bot_state.daily_stop_counts[prefix] = bot_state.daily_stop_counts.get(prefix, 0) + 1
                        await save_state(bot_state)
                        entry_timestamp = 0.0
                        if prefix in bot_state.pending_swaps:
                            new_a, new_b = bot_state.pending_swaps.pop(prefix)
                            new_prefix = make_prefix(new_a, new_b)
                            _swap_pair_in_list(prefix, new_a, new_b)
                            await save_state(bot_state)
                            await notifier._send(f"🔄 [{prefix}] → [{new_prefix}] 바통 터치!")
                            asyncio.create_task(pair_loop(new_a, new_b, exchange, order_executor, bot_state, notifier, entry_lock))
                            return
                        await asyncio.sleep(POLL_INTERVAL_SEC)
                        continue

                    # ── 우선순위 3: 하드 스탑 (-3.5%) ──
                    elif pnl_calc_ok and unrealized_pct <= MAX_LOSS_PCT:
                        logger.warning(
                            f"[{prefix}] 하드 스탑 발동! "
                            f"upnl_A={upnl_a:+.4f} upnl_B={upnl_b:+.4f} "
                            f"gross={gross_pnl:+.4f} fee={total_fee:.4f} "
                            f"net={unrealized_net:+.4f} USDT ({unrealized_pct:+.2f}%) <= {MAX_LOSS_PCT}%"
                        )
                        await _execute_close(
                            prefix, "STOP_LOSS", price_a, price_b,
                            sym_a, sym_b, risk_manager, order_executor,
                            bot_state, notifier, logger, dev,
                            z_score=state["z_score"],
                            close_reason=f"최대 허용 손실 초과({MAX_LOSS_PCT}%) 하드 스탑",
                        )
                        bot_state.cooldowns[prefix] = time.time()
                        bot_state.daily_stop_counts[prefix] = bot_state.daily_stop_counts.get(prefix, 0) + 1
                        await save_state(bot_state)
                        entry_timestamp = 0.0
                        if prefix in bot_state.pending_swaps:
                            new_a, new_b = bot_state.pending_swaps.pop(prefix)
                            new_prefix = make_prefix(new_a, new_b)
                            _swap_pair_in_list(prefix, new_a, new_b)
                            await save_state(bot_state)
                            await notifier._send(f"🔄 [{prefix}] → [{new_prefix}] 바통 터치!")
                            asyncio.create_task(pair_loop(new_a, new_b, exchange, order_executor, bot_state, notifier, entry_lock))
                            return
                        await asyncio.sleep(POLL_INTERVAL_SEC)
                        continue

                    # ── 우선순위 4: 통계 손절 — Z-Score >= 4.5 ──
                    elif abs(state["z_score"]) >= STOP_LOSS_Z_SCORE:
                        logger.warning(
                            f"[{prefix}] 통계적 디커플링 손절! Z={state['z_score']:+.3f} >= {STOP_LOSS_Z_SCORE}"
                        )
                        await _execute_close(
                            prefix, "STOP_LOSS", price_a, price_b,
                            sym_a, sym_b, risk_manager, order_executor,
                            bot_state, notifier, logger, dev,
                            z_score=state["z_score"],
                            close_reason=f"통계적 디커플링(Z={state['z_score']:+.2f} >= {STOP_LOSS_Z_SCORE}) 꼬리 끊기",
                        )
                        bot_state.cooldowns[prefix] = time.time()
                        bot_state.daily_stop_counts[prefix] = bot_state.daily_stop_counts.get(prefix, 0) + 1
                        await save_state(bot_state)
                        entry_timestamp = 0.0
                        if prefix in bot_state.pending_swaps:
                            new_a, new_b = bot_state.pending_swaps.pop(prefix)
                            new_prefix = make_prefix(new_a, new_b)
                            _swap_pair_in_list(prefix, new_a, new_b)
                            await save_state(bot_state)
                            await notifier._send(f"🔄 [{prefix}] → [{new_prefix}] 바통 터치!")
                            asyncio.create_task(pair_loop(new_a, new_b, exchange, order_executor, bot_state, notifier, entry_lock))
                            return

                    # ── Z-Score Zero-Cross 익절 (급등락 대비 보완) ──
                    else:
                        if pos.entry_z_score > 0 and state["z_score"] <= EXIT_Z_SCORE:
                            signal = "EXIT"
                        elif pos.entry_z_score < 0 and state["z_score"] >= -EXIT_Z_SCORE:
                            signal = "EXIT"

                        if signal == "EXIT":
                            # 슬리피지 방어: 실시간 unrealized_pct가 +1.5% 이상일 때만 청산
                            # (시장가 슬리피지 + 왕복 수수료 버퍼 확보)
                            if not pnl_calc_ok or unrealized_pct < 1.5:
                                logger.debug(
                                    f"[{prefix}] EXIT 시그널 무시 — "
                                    f"unrealized={unrealized_pct:+.2f}% < +1.5% (슬리피지 방어 홀딩)"
                                )
                                await asyncio.sleep(POLL_INTERVAL_SEC)
                                continue
                            close_result = await _execute_close(
                                prefix, "TAKE_PROFIT", price_a, price_b,
                                sym_a, sym_b, risk_manager, order_executor,
                                bot_state, notifier, logger, dev,
                                z_score=state["z_score"],
                            )
                            # Chasing ABORTED 시 포지션 유지, 다음 루프에서 재시도
                            if close_result is False:
                                await asyncio.sleep(POLL_INTERVAL_SEC)
                                continue
                            entry_timestamp = 0.0
                            if prefix in bot_state.pending_swaps:
                                new_a, new_b = bot_state.pending_swaps.pop(prefix)
                                new_prefix = make_prefix(new_a, new_b)
                                _swap_pair_in_list(prefix, new_a, new_b)
                                await save_state(bot_state)
                                await notifier._send(f"🔄 [{prefix}] → [{new_prefix}] 바통 터치!")
                                asyncio.create_task(pair_loop(new_a, new_b, exchange, order_executor, bot_state, notifier, entry_lock))
                                return

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
# 다이내믹 스캐너 루프 — 백그라운드에서 주기적으로 최적 페어 교체
# ─────────────────────────────────────────────────────────────────────────────
async def dynamic_scanner_loop(
    exchange, bot_state, notifier, order_executor,
    entry_lock, active_tasks: dict,
):
    """
    백그라운드에서 SCANNER_INTERVAL_HOURS마다 다이내믹 스캐너를 실행하고
    PAIRS_TO_TRADE를 무중단 핫 스왑합니다.

    핵심 안전장치:
    1. 활성 포지션이 있는 페어는 '좀비(Zombie)'로 보호 → 포지션 청산 시 자연 소멸
    2. paused_pairs에 있는 페어는 스캐너가 찾아와도 제외
    3. 교체 시 빠진 페어의 pair_loop Task를 cancel()하여 메모리 누수(유령 루프) 완벽 차단
    """
    logger = logging.getLogger("pair_bot")
    scanner = DynamicScanner()
    interval_sec = SCANNER_INTERVAL_HOURS * 3600

    # 봇 시작 시 워밍업 대기 (기존 루프들이 안정화될 시간)
    await asyncio.sleep(30)
    logger.info(
        f"[DynScanner] 다이내믹 스캐너 루프 시작 | "
        f"주기: {SCANNER_INTERVAL_HOURS}시간"
    )

    while True:
        try:
            # 텔레그램에서 강제 스캔 요청이 들어온 경우 플래그 해제
            if bot_state.force_scan:
                bot_state.force_scan = False
                logger.info("[DynScanner] 텔레그램 강제 스캔 요청 감지!")
            
            logger.info("[DynScanner] ═══ 스캔 사이클 시작 ═══")
            bot_state.scan_in_progress = True

            # 스캔 전용 exchange (공용 API, 키 불필요)
            import ccxt.async_support as _ccxt
            scan_exchange = _ccxt.binanceusdm({
                "options": {
                    "defaultType": "future",
                    "adjustForTimeDifference": True,
                },
                "enableRateLimit": True,
            })

            try:
                new_pairs = await scanner.run_full_scan(scan_exchange)
            finally:
                await scan_exchange.close()

            if not new_pairs:
                logger.warning(
                    "[DynScanner] 유효 페어 없음 — 기존 유니버스 유지"
                )
                bot_state.scan_in_progress = False
                await _wait_for_next_scan(interval_sec, bot_state)
                continue

            # ── 결과를 bot_state에 캐시 ──
            import time as _time
            bot_state.dynamic_universe = new_pairs
            bot_state.last_scan_time = _time.time()

            # 새 유니버스 prefix 집합
            new_prefixes = set()
            new_pair_map = {}  # {prefix: (sym_a, sym_b)}
            for p in new_pairs:
                pf = p["pair"]
                new_prefixes.add(pf)
                new_pair_map[pf] = (p["sym_a"], p["sym_b"])

            # paused_pairs 제외
            new_prefixes -= bot_state.paused_pairs
            for pp in bot_state.paused_pairs:
                new_pair_map.pop(pp, None)

            # 기존 active_tasks의 prefix 집합
            old_prefixes = set(active_tasks.keys())

            # ── DIFF 계산 ──
            to_add = new_prefixes - old_prefixes     # 새로 감시할 페어
            to_remove = old_prefixes - new_prefixes  # 감시 해제할 페어
            to_keep = old_prefixes & new_prefixes    # 유지할 페어

            # ── 1) 감시 해제 대상 처리 (Task Cancel + 좀비 보호) ──
            killed_count = 0
            zombie_count = 0
            for pf in to_remove:
                if pf in bot_state.positions:
                    # 활성 포지션 보유 → 좀비로 보호 (Task 유지!)
                    bot_state.zombie_pairs.add(pf)
                    zombie_count += 1
                    logger.info(
                        f"[DynScanner] 🧟 좀비 보호: {pf} "
                        f"(활성 포지션 보유 → 청산 시 자동 제거)"
                    )
                else:
                    # 포지션 없음 → Task 완전 Kill
                    task = active_tasks.pop(pf, None)
                    if task and not task.done():
                        task.cancel()
                        killed_count += 1
                        logger.info(
                            f"[DynScanner] 💀 Task Kill: {pf} "
                            f"(포지션 없음 → pair_loop 제거)"
                        )

            # ── 좀비 페어 정리: 포지션이 이미 청산된 좀비 제거 ──
            cleared_zombies = []
            for zp in list(bot_state.zombie_pairs):
                if zp not in bot_state.positions:
                    # 포지션 청산 완료 → 좀비에서 해제 + Task Kill
                    bot_state.zombie_pairs.discard(zp)
                    task = active_tasks.pop(zp, None)
                    if task and not task.done():
                        task.cancel()
                    cleared_zombies.append(zp)
            if cleared_zombies:
                logger.info(
                    f"[DynScanner] 🧹 좀비 정리 완료: "
                    f"{', '.join(cleared_zombies)}"
                )

            # ── 2) 새 페어 추가 (사전 워밍업 + pair_loop Task 생성) ──
            added_count = 0
            for pf in to_add:
                sym_a, sym_b = new_pair_map[pf]
                
                # 워밍업 트랩 파괴: pair_loop 시작 전 24시간치 데이터 즉시 주입
                await pre_warmup_pair(exchange, sym_a, sym_b, pf, bot_state)
                
                task = asyncio.create_task(
                    pair_loop(
                        sym_a, sym_b, exchange,
                        order_executor, bot_state, notifier, entry_lock,
                    ),
                    name=f"pair_loop:{pf}",
                )
                active_tasks[pf] = task
                added_count += 1
                logger.info(
                    f"[DynScanner] ✅ 신규 등록: {pf} "
                    f"({sym_a} / {sym_b})"
                )

            # ── 3) PAIRS_TO_TRADE 리스트 동기화 ──
            PAIRS_TO_TRADE.clear()
            # 새 유니버스 (paused 제외)
            for pf in new_prefixes:
                if pf in new_pair_map:
                    sym_a, sym_b = new_pair_map[pf]
                    PAIRS_TO_TRADE.append((sym_a, sym_b))
            # 좀비 페어도 PAIRS_TO_TRADE에 추가 (감시 유지)
            for zp in bot_state.zombie_pairs:
                pos = bot_state.positions.get(zp)
                if pos:
                    sym_a = pos.sym_a.split(':')[0]
                    sym_b = pos.sym_b.split(':')[0]
                    if (sym_a, sym_b) not in PAIRS_TO_TRADE:
                        PAIRS_TO_TRADE.append((sym_a, sym_b))

            # ── 4) 레버리지 세팅 (새 코인에 대해) ──
            try:
                await order_executor.setup_leverage()
            except Exception as e:
                logger.warning(f"[DynScanner] 레버리지 세팅 오류: {e}")

            # ── 5) 상태 저장 ──
            await save_state(bot_state)

            # ── 6) 텔레그램 알림 ──
            summary_lines = [
                "🔄 [다이내믹 스캐너] 유니버스 교체 완료\n",
                f"🆕 신규: {added_count}개 | "
                f"💀 제거: {killed_count}개 | "
                f"🧟 좀비: {zombie_count}개 | "
                f"🔒 유지: {len(to_keep)}개\n",
            ]
            for i, p in enumerate(new_pairs[:15], 1):
                pf = p["pair"]
                mark = "🧟" if pf in bot_state.zombie_pairs else "✅"
                summary_lines.append(
                    f"  {i:2d}. {mark} {pf} "
                    f"(corr={p['corr']:.2f} | "
                    f"coint={p['coint_pvalue']:.4f})"
                )
            try:
                await notifier._send("\n".join(summary_lines))
            except Exception as e:
                logger.debug(f"[DynScanner] 텔레그램 알림 실패: {e}")

            logger.info(
                f"[DynScanner] ═══ 스캔 사이클 완료 ═══ | "
                f"active_tasks={len(active_tasks)}개 | "
                f"다음 스캔: {SCANNER_INTERVAL_HOURS}시간 후"
            )

        except asyncio.CancelledError:
            logger.info("[DynScanner] 스캐너 루프 종료 요청")
            break
        except Exception as e:
            logger.error(
                f"[DynScanner] 스캔 사이클 오류: {e}", exc_info=True
            )
        finally:
            bot_state.scan_in_progress = False

        await _wait_for_next_scan(interval_sec, bot_state)


async def _wait_for_next_scan(interval_sec: int, bot_state):
    """지정된 시간 동안 대기하되, 강제 스캔 요청이 오면 즉시 대기를 중단합니다."""
    waited = 0
    while waited < interval_sec:
        if bot_state.force_scan:
            break
        await asyncio.sleep(5)
        waited += 5


# ─────────────────────────────────────────────────────────────────────────────
# 메인 루프 — 페어 병렬 + 텔레그램 앱 동시 실행
# ─────────────────────────────────────────────────────────────────────────────
async def main_loop():
    logger = logging.getLogger("pair_bot")

    mode_str = "PAPER TRADING (가상 시뮬레이션)" if IS_PAPER_TRADING else "LIVE TRADING (실계좌)"
    logger.info("=" * 60)
    logger.info(f"  페어 트레이딩 봇 시작 — 모드: {mode_str}")
    logger.info(f"  감시 페어 수: {len(PAIRS_TO_TRADE)}개 | 최대 동시 진입: {MAX_ACTIVE_PAIRS}개")
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
        "options"        : {"defaultType": "future", "adjustForTimeDifference": True},
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

        # ── 저장된 페어 리스트로 PAIRS_TO_TRADE 덮어쓰기 (기억 상실 방지) ──
        if bot_state.active_pairs_override:
            PAIRS_TO_TRADE.clear()
            PAIRS_TO_TRADE.extend(bot_state.active_pairs_override)
            logger.info(
                f"[PairOverride] bot_state.json의 페어 리스트로 교체 완료 | "
                f"{len(PAIRS_TO_TRADE)}개 페어"
            )
            for sym_a, sym_b in PAIRS_TO_TRADE:
                logger.info(f"    [{make_prefix(sym_a, sym_b):12s}]  {sym_a}  /  {sym_b}")
        else:
            logger.info("[PairOverride] 저장된 페어 리스트 없음 → config.py 기본값 유지")

    # ── PAIRS_TO_TRADE 중복 제거 (유령 중복 방어) ──
    seen = set()
    deduped = []
    for pair in PAIRS_TO_TRADE:
        prefix = make_prefix(pair[0], pair[1])
        if prefix not in seen:
            seen.add(prefix)
            deduped.append(pair)
    if len(deduped) != len(PAIRS_TO_TRADE):
        removed = len(PAIRS_TO_TRADE) - len(deduped)
        logger.warning(f"[Dedup] PAIRS_TO_TRADE에서 중복 {removed}개 제거")
        PAIRS_TO_TRADE.clear()
        PAIRS_TO_TRADE.extend(deduped)

    # ── 꼬인 pending_swaps 안전 리셋 (임시 치료용) ──
    if bot_state.pending_swaps:
        # 실제 활성 포지션이 없는 pending만 제거
        stale_keys = [
            k for k in bot_state.pending_swaps
            if k not in bot_state.positions
        ]
        if stale_keys:
            for k in stale_keys:
                del bot_state.pending_swaps[k]
            logger.warning(
                f"[PendingSwap] 포지션 없는 좀비 대기열 {len(stale_keys)}건 제거: "
                f"{stale_keys}"
            )
            await save_state(bot_state)

    # ── 바이낸스 API와의 실시간 교차 검증 (Source of Truth) ──
    if bot_state.positions:
        logger.info("[포지션동기화] 로컬 저장 포지션 교차 검증 시작...")
        try:
            real_positions = await order_executor.get_active_positions()
            sync_needed = False
            for prefix, pos in list(bot_state.positions.items()):
                sym_a = to_futures_symbol(pos.sym_a)
                sym_b = to_futures_symbol(pos.sym_b)
                
                pos_a = real_positions.get(sym_a)
                pos_b = real_positions.get(sym_b)
                
                is_valid = True
                if not pos_a or not pos_b:
                    is_valid = False
                else:
                    if pos.side == "SHORT_A_LONG_B":
                        if pos_a["side"] != "short" or pos_b["side"] != "long":
                            is_valid = False
                    elif pos.side == "LONG_A_SHORT_B":
                        if pos_a["side"] != "long" or pos_b["side"] != "short":
                            is_valid = False
                
                if not is_valid:
                    logger.warning(
                        f"[포지션동기화] 불일치 감지 | {prefix} 페어가 실제 거래소 포지션과 일치하지 않아 "
                        f"JSON 복구 데이터에서 제거합니다. (실제: A={pos_a}, B={pos_b})"
                    )
                    bot_state.positions.pop(prefix, None)
                    sync_needed = True
            
            if sync_needed:
                await save_state(bot_state)
                logger.info("[포지션동기화] 불일치 포지션 정리 후 bot_state.json 업데이트 완료")
            else:
                logger.info("[포지션동기화] 모든 복구 포지션이 거래소 상태와 일치합니다.")
        except Exception as e:
            logger.error(f"[포지션동기화] 교차 검증 중 오류 발생: {e}")

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

    # ── active_tasks: 페어별 pair_loop Task 추적 딕셔너리 (유령 Task 방지) ───
    # dynamic_scanner_loop에서 페어 교체 시 cancel()로 루프 정리
    active_tasks: dict = {}   # {prefix: asyncio.Task}
    entry_lock = asyncio.Lock()  # 신규 진입 동시성 제어용 Lock
    
    # 1. PAIRS_TO_TRADE에 있는 페어들 워밍업/루프 등록
    for batch_start in range(0, len(PAIRS_TO_TRADE), WARMUP_BATCH_SIZE):
        batch = PAIRS_TO_TRADE[batch_start:batch_start + WARMUP_BATCH_SIZE]
        for sym_a, sym_b in batch:
            prefix = make_prefix(sym_a, sym_b)
            if prefix not in active_tasks:
                task = asyncio.create_task(
                    pair_loop(sym_a, sym_b, exchange, order_executor, bot_state, notifier, entry_lock),
                    name=f"pair_loop:{prefix}",
                )
                active_tasks[prefix] = task
        if batch_start + WARMUP_BATCH_SIZE < len(PAIRS_TO_TRADE):
            logger.info(f"[워밍업 배치] {batch_start + WARMUP_BATCH_SIZE}/{len(PAIRS_TO_TRADE)}개 시작, 2초 대기...")
            await asyncio.sleep(2)  # 배치 간 2초 딜레이
            
    # 2. PAIRS_TO_TRADE에는 없지만 아직 안 닫힌 활성 포지션(고아 포지션) 강제 루프 할당
    for prefix, pos in bot_state.positions.items():
        if prefix not in active_tasks:
            logger.warning(f"[고아 포지션 복구] {prefix} 페어가 감시 목록에 없지만 활성 포지션이 존재하여 강제로 감시 스레드를 생성합니다.")
            sym_a = pos.sym_a.split(':')[0]
            sym_b = pos.sym_b.split(':')[0]
            task = asyncio.create_task(
                pair_loop(sym_a, sym_b, exchange, order_executor, bot_state, notifier, entry_lock),
                name=f"pair_loop:{prefix}",
            )
            active_tasks[prefix] = task
            bot_state.zombie_pairs.add(prefix)  # 스캐너 유니버스 외부이므로 좀비 표시

    logger.info(f"[Task관리] 초기 active_tasks: {len(active_tasks)}개 페어 루프 등록")

    # ── 시스템 루프들 (개별 Task로 생성) ──────────────────────────────────────
    system_tasks = [
        asyncio.create_task(global_ticker_loop(exchange, bot_state), name="global_ticker"),
        asyncio.create_task(btc_turbulence_monitor(exchange, bot_state, notifier), name="btc_monitor"),
        asyncio.create_task(
            dynamic_scanner_loop(
                exchange, bot_state, notifier,
                order_executor, entry_lock, active_tasks,
            ),
            name="dynamic_scanner",
        ),
    ]

    try:
        # 모든 Task를 모아서 대기 (페어 루프 + 시스템 루프)
        all_tasks = list(active_tasks.values()) + system_tasks
        await asyncio.gather(*all_tasks, return_exceptions=True)
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
