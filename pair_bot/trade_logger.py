# =============================================================================
# pair_bot/trade_logger.py
# 매매 결과 CSV 자동 기록 모듈 (ML 파라미터 최적화용 데이터 수집)
# aiofiles를 사용해 비동기 루프 블로킹 없이 안전하게 기록
# =============================================================================

import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta

import aiofiles

from config import LOG_FILE  # LOG_FILE과 같은 폴더에 CSV 생성

logger = logging.getLogger("pair_bot")

# CSV 파일 경로 (main.py와 동일 디렉터리)
CSV_PATH = "trade_history.csv"

# CSV 헤더 컬럼 정의
CSV_HEADER = (
    "Entry_Time,Exit_Time,Pair,Trade_Duration_Sec,"
    "Entry_Z_Score,Exit_Z_Score,"
    "PnL_USDT,PnL_Percent,Exit_Reason\n"
)

# Exit_Reason 코드 → CSV 표기 매핑
REASON_MAP = {
    "TAKE_PROFIT" : "Normal_Target",
    "STOP_LOSS"   : "Decoupling_StopLoss",
    "KILL_SWITCH" : "Kill_Switch",
    "MANUAL"      : "Manual_Close",
}

# KST 시간대 (UTC+9)
KST = timezone(timedelta(hours=9))


def _now_kst() -> datetime:
    return datetime.now(KST)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def init_csv() -> None:
    """
    봇 시작 시 1회 호출.
    trade_history.csv가 없으면 헤더를 포함해 새로 생성합니다.
    이미 있으면 기존 데이터를 보존합니다.
    """
    if os.path.exists(CSV_PATH):
        logger.info(f"[TradeLogger] 기존 CSV 발견: {CSV_PATH} — 이어쓰기 모드")
        return
    async with aiofiles.open(CSV_PATH, mode="w", encoding="utf-8") as f:
        await f.write(CSV_HEADER)
    logger.info(f"[TradeLogger] CSV 생성 완료: {CSV_PATH}")


async def log_trade(
    pair: str,
    entry_time: datetime,
    exit_time: datetime,
    entry_z: float,
    exit_z: float,
    pnl_usdt: float,
    pnl_pct: float,
    reason: str,
) -> None:
    """
    청산 완료 시 호출. 매매 결과를 CSV 맨 아랫줄에 한 행 추가합니다.
    비동기(aiofiles)로 작성하므로 메인 루프에 블로킹이 없습니다.
    """
    duration_sec = int((exit_time - entry_time).total_seconds())
    reason_str   = REASON_MAP.get(reason, reason)

    row = (
        f"{_fmt(entry_time)},{_fmt(exit_time)},{pair},{duration_sec},"
        f"{entry_z:.4f},{exit_z:.4f},"
        f"{pnl_usdt:.4f},{pnl_pct:.4f},{reason_str}\n"
    )

    try:
        async with aiofiles.open(CSV_PATH, mode="a", encoding="utf-8") as f:
            await f.write(row)
        logger.debug(
            f"[TradeLogger] 기록 완료 | {pair} | {reason_str} | "
            f"PnL={pnl_usdt:+.4f} USDT"
        )
    except Exception as e:
        logger.error(f"[TradeLogger] CSV 기록 실패: {e}")
