# =============================================================================
# pair_bot/state_persistence.py
# 봇 상태(활성 포지션 + 누적 통계)를 JSON으로 저장/복구하는 모듈
# 서버 재구동 시 이전 포지션 그대로 이어서 감시할 수 있게 해줍니다.
# aiofiles 기반으로 비동기 안전하게 기록합니다.
# =============================================================================

import json
import logging
from datetime import datetime

import aiofiles

from config import STATE_FILE
from bot_state import BotState, PairPosition
from trade_logger import KST

logger = logging.getLogger("pair_bot")

_DT_FMT = "%Y-%m-%d %H:%M:%S"


def _dt_to_str(dt: datetime) -> str:
    return dt.strftime(_DT_FMT)


def _str_to_dt(s: str) -> datetime:
    return datetime.strptime(s, _DT_FMT).replace(tzinfo=KST)


# ── 저장 ──────────────────────────────────────────────────────────────────────

async def save_state(bot_state: BotState) -> None:
    """
    현재 활성 포지션과 누적 통계를 bot_state.json에 즉시 덮어씁니다.
    진입/청산 직후 호출하세요.
    """
    positions_data = {}
    for prefix, pos in bot_state.positions.items():
        positions_data[prefix] = {
            "prefix"       : pos.prefix,
            "side"         : pos.side,
            "entry_ratio"  : pos.entry_ratio,
            "sym_a"        : pos.sym_a,
            "sym_b"        : pos.sym_b,
            "price_a"      : pos.price_a,
            "price_b"      : pos.price_b,
            "trade_usdt"   : pos.trade_usdt,
            "entry_time"   : _dt_to_str(pos.entry_time),
            "entry_z_score": pos.entry_z_score,
        }

    data = {
        "positions"     : positions_data,
        "total_trades"  : bot_state.total_trades,
        "wins"          : bot_state.wins,
        "cumulative_pnl": bot_state.cumulative_pnl,
        "initial_balance": bot_state.initial_balance,
        "saved_at"      : _dt_to_str(datetime.now(KST)),
    }

    try:
        async with aiofiles.open(STATE_FILE, mode="w", encoding="utf-8") as f:
            await f.write(json.dumps(data, ensure_ascii=False, indent=2))
        logger.debug(f"[StateStore] 상태 저장 완료 | 포지션={len(positions_data)}개")
    except Exception as e:
        logger.error(f"[StateStore] 상태 저장 실패: {e}")


# ── 복구 ──────────────────────────────────────────────────────────────────────

async def load_state(bot_state: BotState) -> bool:
    """
    bot_state.json이 존재하면 읽어와 bot_state 메모리를 복구합니다.
    복구 성공 시 True, 파일 없음 또는 실패 시 False 반환.
    """
    import os
    if not os.path.exists(STATE_FILE):
        logger.info("[StateStore] 저장 파일 없음 — 새 세션으로 시작")
        return False

    try:
        async with aiofiles.open(STATE_FILE, mode="r", encoding="utf-8") as f:
            raw = await f.read()
        data = json.loads(raw)

        # 누적 통계 복구
        bot_state.total_trades   = data.get("total_trades", 0)
        bot_state.wins           = data.get("wins", 0)
        bot_state.cumulative_pnl = data.get("cumulative_pnl", 0.0)
        bot_state.initial_balance = data.get("initial_balance", 0.0)

        # 포지션 복구
        positions_data = data.get("positions", {})
        for prefix, pd in positions_data.items():
            bot_state.positions[prefix] = PairPosition(
                prefix       = pd["prefix"],
                side         = pd["side"],
                entry_ratio  = pd["entry_ratio"],
                sym_a        = pd["sym_a"],
                sym_b        = pd["sym_b"],
                price_a      = pd["price_a"],
                price_b      = pd["price_b"],
                trade_usdt   = pd["trade_usdt"],
                entry_time   = _str_to_dt(pd["entry_time"]),
                entry_z_score= pd.get("entry_z_score", 0.0),
            )

        saved_at = data.get("saved_at", "알 수 없음")
        logger.info(
            f"[StateStore] 상태 복구 완료 | 저장시각: {saved_at} | "
            f"포지션={len(bot_state.positions)}개 | "
            f"누적PnL={bot_state.cumulative_pnl:+.4f} USDT"
        )
        return True

    except Exception as e:
        logger.error(f"[StateStore] 상태 복구 실패 — 새 세션으로 시작: {e}")
        return False
