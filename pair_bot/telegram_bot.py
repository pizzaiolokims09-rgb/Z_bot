# =============================================================================
# pair_bot/telegram_bot.py
# 텔레그램 연동 모듈 — 자동 알림 + 인라인 컨트롤 패널
# python-telegram-bot v20+ (asyncio 네이티브) 사용
# =============================================================================

from __future__ import annotations
import logging
from typing import TYPE_CHECKING

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.error import TelegramError

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from bot_state import BotState

if TYPE_CHECKING:
    from order_executor import OrderExecutor

logger = logging.getLogger("pair_bot")


class TelegramNotifier:
    """
    자동 알림 전송 + 인라인 버튼 컨트롤 패널을 담당합니다.
    python-telegram-bot v20+ Application 위에서 동작하며,
    기존 asyncio 이벤트 루프와 함께 실행됩니다.
    """

    def __init__(self, bot_state: BotState, order_executor: "OrderExecutor"):
        self._state    = bot_state
        self._executor = order_executor
        self._app: Application | None = None

    # ── 앱 초기화 (main.py에서 호출) ─────────────────────────────────────────

    # 하단 고정 키보드 버튼 텍스트 상수
    BTN_CLOSE   = "🔘 수동 청산"
    BTN_STOP    = "🛑 봇 정지"
    BTN_RESUME  = "▶️ 봇 재시작"
    BTN_STATUS  = "📊 상태 확인"
    BTN_STATS   = "💰 승률 & PnL"

    def _reply_keyboard(self) -> ReplyKeyboardMarkup:
        """항상 입력창 위에 고정되는 하단 키보드를 반환합니다."""
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton(self.BTN_CLOSE)],
                [KeyboardButton(self.BTN_STOP), KeyboardButton(self.BTN_RESUME)],
                [KeyboardButton(self.BTN_STATUS), KeyboardButton(self.BTN_STATS)],
            ],
            resize_keyboard=True,
            one_time_keyboard=False,
        )

    def build_app(self) -> Application:
        """Application 객체를 생성하고 핸들러를 등록합니다."""
        app = (
            Application.builder()
            .token(TELEGRAM_BOT_TOKEN)
            .build()
        )
        app.add_handler(CommandHandler(["start", "menu"], self._cmd_menu))
        app.add_handler(CallbackQueryHandler(self._on_callback))
        # 하단 고정 키보드 버튼 텍스트 수신 핸들러
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_button_text))
        self._app = app
        return app

    # =========================================================================
    # 자동 알림 메서드 (pair_loop에서 호출)
    # =========================================================================

    async def send_entry(
        self,
        prefix: str,
        sym_a: str, side_a: str, price_a: float,
        sym_b: str, side_b: str, price_b: float,
        trade_usdt: float,
    ) -> None:
        """진입 알림 전송."""
        # side_a == "buy" 이면 A가 Long, B가 Short
        long_sym  = sym_a if side_a == "buy" else sym_b
        short_sym = sym_b if side_a == "buy" else sym_a
        lp        = price_a if side_a == "buy" else price_b
        sp        = price_b if side_a == "buy" else price_a

        msg = (
            f"🟢 [진입] {prefix} 헷징 시작\n"
            f"Long  : {long_sym.split(':')[0]}\n"
            f"Short : {short_sym.split(':')[0]}\n"
            f"진입가: L={lp:.4f}  S={sp:.4f} USDT\n"
            f"레그당 증거금: {trade_usdt:.2f} USDT"
        )
        await self._send(msg)

    async def send_exit(
        self,
        prefix: str,
        pnl_usdt: float,
        pnl_pct: float,
        reason: str,
    ) -> None:
        """청산/손절 알림 + PnL 전송."""
        if reason == "TAKE_PROFIT":
            tag   = "청산"
            desc  = "괴리 회귀 완료"
            emoji = "📈" if pnl_usdt >= 0 else "📉"
        elif reason == "MANUAL":
            tag   = "수동 청산"
            desc  = "사용자 요청"
            emoji = "🔘"
        else:
            tag   = "손절"
            desc  = "디커플링 손절"
            emoji = "🛑"

        msg = (
            f"{emoji} [{tag}] {prefix} 포지션 종료\n"
            f"사유: {desc}\n"
            f"실현 PnL: {pnl_usdt:+.4f} USDT  ({pnl_pct:+.3f}%)"
        )
        await self._send(msg)

    async def send_kill_switch(self, drawdown_pct: float, current_bal: float) -> None:
        """글로벌 킬 스위치 발동 시 긴급 알림 전송."""
        msg = (
            "🚨🚨🚨 [긴급] 글로벌 킬 스위치 발동!\n"
            f"누적 손실: {drawdown_pct:+.2f}%\n"
            f"현재 잔고: {current_bal:.2f} USDT\n"
            "전 포지션 강제 청산 및 봇 완전 종료"
        )
        await self._send(msg)

    async def send_legging_alert(self, prefix: str, detail: str) -> None:
        """짝짝이 체결(Legging Risk) 롤백 알림 전송."""
        msg = (
            f"⚠️ [Legging Risk] {prefix}\n"
            f"{detail}\n"
            "성공 레그 자동 롤백 완료 — 포지션 미기록"
        )
        await self._send(msg)

    async def send_turbulence_alert(self, is_turbulent: bool, btc_vol_pct: float) -> None:
        """BTC 시장 폭주 감지/해제 알림 전송."""
        if is_turbulent:
            msg = (
                f"⚠️ [시장 폭주 감지] BTC 15분 변동성 {btc_vol_pct:.2f}%\n"
                "신규 진입 일시 정지 — 기존 포지션 청산 감시는 계속 작동"
            )
        else:
            msg = (
                f"✅ [시장 안정화] BTC 15분 변동성 {btc_vol_pct:.2f}%\n"
                "신규 진입 재개"
            )
        await self._send(msg)

    # =========================================================================
    # 명령어 핸들러
    # =========================================================================

    async def _cmd_menu(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """/start 또는 /menu — 하단 고정 키보드를 활성화합니다."""
        status = "🟢 Running" if self._state.is_accepting_entries else "🔴 Stopped"
        await update.message.reply_text(
            f"🤖 페어 트레이딩 봇 컨트롤 패널\n현재 상태: {status}\n"
            f"아래 버튼으로 봇을 제어하세요.",
            reply_markup=self._reply_keyboard(),
        )

    async def _handle_button_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """하단 고정 키보드 버튼 텍스트를 받아 처리합니다."""
        text = update.message.text.strip()

        _confirm_kb = lambda cd: InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ 확인", callback_data=cd),
             InlineKeyboardButton("❌ 취소", callback_data="cancel_action")]
        ])

        if text == self.BTN_CLOSE:
            active = list(self._state.positions.keys())
            if not active:
                await update.message.reply_text(
                    "현재 진입 중인 포지션이 없습니다.",
                    reply_markup=self._reply_keyboard(),
                )
                return
            # 페어 선택 버튼 (선택 후 확인 단계)
            buttons = [
                [InlineKeyboardButton(f"🔘 {p}", callback_data=f"close_pair:{p}")]
                for p in active
            ]
            await update.message.reply_text(
                "⚠️ 청산할 페어를 선택하세요:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )

        elif text == self.BTN_STOP:
            await update.message.reply_text(
                "⚠️ 정말 신규 진입 감시를 중단하시겠습니까?\n(기존 포지션 익절·손절은 유지됩니다)",
                reply_markup=_confirm_kb("confirm_stop"),
            )

        elif text == self.BTN_RESUME:
            await update.message.reply_text(
                "⚠️ 신규 진입 감시를 재시작하시겠습니까?",
                reply_markup=_confirm_kb("confirm_resume"),
            )

        elif text == self.BTN_STATUS:
            status_str = "🟢 Running" if self._state.is_accepting_entries else "🔴 Stopped"
            try:
                free_bal = await self._executor.get_free_balance()
                bal_str  = f"{free_bal:.2f} USDT"
            except Exception:
                bal_str  = "조회 실패"
            lines = []
            for prefix, pos in self._state.positions.items():
                dev  = self._state.latest_dev.get(prefix, 0.0)
                side = "L-A/S-B" if pos.side == "LONG_A_SHORT_B" else "S-A/L-B"
                lines.append(f"  • [{prefix}] {side} | dev={dev:+.3f}%")
            pos_text = "\n".join(lines) if lines else "  없음"
            await update.message.reply_text(
                f"📊 봇 상태: {status_str}\n"
                f"가용 잔고: {bal_str}\n\n"
                f"활성 포지션 ({len(self._state.positions)}개):\n{pos_text}",
                reply_markup=self._reply_keyboard(),
            )

        elif text == self.BTN_STATS:
            s     = self._state
            emoji = "📈" if s.cumulative_pnl >= 0 else "📉"
            try:
                free_bal = await self._executor.get_free_balance()
                bal_str  = f"{free_bal:.2f} USDT"
            except Exception:
                bal_str  = "조회 실패"
            await update.message.reply_text(
                f"💰 누적 통계\n"
                f"총 거래: {s.total_trades}회\n"
                f"승률: {s.win_rate:.1f}%  ({s.wins}승 / {s.total_trades - s.wins}패)\n"
                f"누적 PnL: {emoji} {s.cumulative_pnl:+.4f} USDT\n"
                f"현재 계좌 잔고: {bal_str}",
                reply_markup=self._reply_keyboard(),
            )

    # =========================================================================
    # 콜백 핸들러 (인라인 버튼 클릭)
    # =========================================================================

    async def _on_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        await query.answer()
        data  = query.data

        _confirm_kb = lambda cd: InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ 확인", callback_data=cd),
             InlineKeyboardButton("❌ 취소", callback_data="cancel_action")]
        ])

        if data == "manual_close":
            await self._show_close_list(query)

        elif data.startswith("close_pair:"):
            # 페어 선택 → 확인 단계
            prefix = data.split(":", 1)[1]
            if prefix not in self._state.positions:
                await query.edit_message_text(f"[{prefix}] 포지션이 이미 없습니다.")
                return
            await query.edit_message_text(
                f"⚠️ [{prefix}] 포지션을 수동 청산하시겠습니까?\n시장가로 즉시 체결됩니다.",
                reply_markup=_confirm_kb(f"confirm_close:{prefix}"),
            )

        elif data.startswith("confirm_close:"):
            # 확인 → 실제 청산
            prefix = data.split(":", 1)[1]
            await self._do_manual_close(query, prefix)

        elif data == "confirm_stop":
            self._state.is_accepting_entries = False
            logger.info("[텔레그램] 신규 진입 감시 중단 명령")
            await query.edit_message_text(
                "🛑 신규 진입 감시 중단\n기존 포지션 익절·손절 감시는 계속 유지됩니다."
            )

        elif data == "confirm_resume":
            self._state.is_accepting_entries = True
            logger.info("[텔레그램] 신규 진입 감시 재개 명령")
            await query.edit_message_text("▶️ 신규 진입 감시 재개됨.")

        elif data == "cancel_action":
            await query.edit_message_text("❌ 취소되었습니다.")

        elif data == "status":
            await self._show_status(query)

        elif data == "stats":
            await self._show_stats(query)

        elif data == "back_to_menu":
            await self._back_to_menu(query)

    # ── 수동 청산 서브 핸들러 ─────────────────────────────────────────────────

    async def _show_close_list(self, query) -> None:
        """활성 페어 목록을 버튼으로 표시합니다."""
        active = list(self._state.positions.keys())
        if not active:
            await query.edit_message_text("현재 진입 중인 포지션이 없습니다.")
            return

        buttons = [
            [InlineKeyboardButton(f"❌ {p}", callback_data=f"close_pair:{p}")]
            for p in active
        ]
        buttons.append([InlineKeyboardButton("← 뒤로", callback_data="back_to_menu")])
        await query.edit_message_text(
            "✖️ 청산할 페어를 선택하세요:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def _do_manual_close(self, query, prefix: str) -> None:
        """수동 청산 요청을 bot_state에 등록합니다. pair_loop가 다음 틱에 처리."""
        if prefix not in self._state.positions:
            await query.edit_message_text(f"[{prefix}] 포지션이 이미 없습니다.")
            return

        self._state.manual_close_requests.add(prefix)
        logger.info(f"[텔레그램] 수동 청산 요청: {prefix}")
        await query.edit_message_text(
            f"🔘 [{prefix}] 수동 청산 요청이 접수됐습니다.\n"
            f"다음 폴링 틱(최대 1초)에 시장가로 청산됩니다."
        )

    # ── 상태 / 통계 출력 ──────────────────────────────────────────────────────

    async def _show_status(self, query) -> None:
        """📊 상태 확인 — 잔고 + 활성 포지션 목록 + 실시간 괴리율."""
        state_str = "🟢 Running" if self._state.is_accepting_entries else "🔴 Stopped"

        try:
            free_bal = await self._executor.get_free_balance()
            bal_str  = f"{free_bal:.2f} USDT"
        except Exception:
            bal_str  = "조회 실패"

        lines = []
        for prefix, pos in self._state.positions.items():
            dev  = self._state.latest_dev.get(prefix, 0.0)
            side = "L-A/S-B" if pos.side == "LONG_A_SHORT_B" else "S-A/L-B"
            lines.append(f"  • [{prefix}] {side} | dev={dev:+.3f}%")

        pos_text = "\n".join(lines) if lines else "  없음"
        msg = (
            f"📊 봇 상태\n"
            f"상태: {state_str}\n"
            f"가용 잔고: {bal_str}\n\n"
            f"활성 포지션 ({len(self._state.positions)}개):\n{pos_text}"
        )
        await query.edit_message_text(msg)

    async def _show_stats(self, query) -> None:
        """💰 승률 & PnL — 누적 통계."""
        s     = self._state
        emoji = "📈" if s.cumulative_pnl >= 0 else "📉"

        try:
            free_bal = await self._executor.get_free_balance()
            bal_str  = f"{free_bal:.2f} USDT"
        except Exception:
            bal_str  = "조회 실패"

        msg = (
            f"💰 누적 통계\n"
            f"총 거래: {s.total_trades}회\n"
            f"승률: {s.win_rate:.1f}%  "
            f"({s.wins}승 / {s.total_trades - s.wins}패)\n"
            f"누적 PnL: {emoji} {s.cumulative_pnl:+.4f} USDT\n\n"
            f"현재 계좌 잔고: {bal_str}"
        )
        await query.edit_message_text(msg)

    async def _back_to_menu(self, query) -> None:
        """← 뒤로 — 메인 컨트롤 패널로 복귀."""
        status   = "🟢 Running" if self._state.is_accepting_entries else "🔴 Stopped"
        keyboard = [
            [InlineKeyboardButton("🔘 수동 청산", callback_data="manual_close")],
            [
                InlineKeyboardButton("🛑 봇 정지",   callback_data="bot_stop"),
                InlineKeyboardButton("▶️ 봇 재시작", callback_data="bot_resume"),
            ],
            [InlineKeyboardButton("📊 상태 확인", callback_data="status")],
            [InlineKeyboardButton("💰 승률 & PnL", callback_data="stats")],
        ]
        await query.edit_message_text(
            f"🤖 페어 트레이딩 봇 컨트롤 패널\n현재 상태: {status}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    # =========================================================================
    # 내부 전송 헬퍼
    # =========================================================================

    async def _send(self, text: str) -> None:
        """TELEGRAM_BOT_TOKEN / CHAT_ID가 설정된 경우에만 전송합니다."""
        if (
            not self._app
            or not TELEGRAM_BOT_TOKEN
            or TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN"
            or not TELEGRAM_CHAT_ID
            or TELEGRAM_CHAT_ID == "YOUR_TELEGRAM_CHAT_ID"
        ):
            logger.debug(f"[텔레그램] 토큰 미설정, 로컬 로그만: {text[:60]}")
            return
        try:
            await self._app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
        except TelegramError as e:
            logger.warning(f"[텔레그램] 전송 실패: {e}")
