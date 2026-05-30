# =============================================================================
# pair_bot/config.py
# 모든 설정값 관리 파일
# 민감 정보(API키, 토큰 등)는 .env 파일에서 읽어옵니다.
# 매매 전략 파라미터는 이 파일 하단에서 직접 수정하세요.
# =============================================================================

import os
from dotenv import load_dotenv

# 이 파일 기준으로 .env 탐색 (pair_bot/.env 또는 부모 폴더까지 자동 탐색)
load_dotenv()

# ── 민감 정보 (.env에서 로드) ────────────────────────────────────────────────
API_KEY    = os.getenv("BINANCE_API_KEY",    "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")

# ── 거래 모드 및 모의투자(Testnet) ──────────────────────────────────────────
# IS_PAPER_TRADING
#   True : API 연결 없이 내부에서 가상 잔고로만 시뮬레이션 (안전)
#   False: 실제 바이낸스 거래소로 주문 집행 (아래 USE_TESTNET에 따라 실거래/모의투자 결정)
IS_PAPER_TRADING = os.getenv("IS_PAPER_TRADING", "true").lower() == "true"

# USE_TESTNET (IS_PAPER_TRADING이 False일 때만 유효)
#   True : 바이낸스 모의투자(Testnet) 네트워크 사용
#   False: 바이낸스 실거래(Mainnet) 네트워크 사용
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"

# ── 거래 대상 페어 목록 (A심볼, B심볼) ──────────────────────────────────────
# 바이낸스 선물 표기법: spot 심볼로 입력하면 내부에서 :USDT 변환
PAIRS_TO_TRADE = [
    ("BTC/USDT",      "ETH/USDT"),
    ("SOL/USDT",      "AVAX/USDT"),
    ("XRP/USDT",      "XLM/USDT"),
    ("DOGE/USDT",     "1000SHIB/USDT"),
    ("LINK/USDT",     "DOT/USDT"),
]

# ── 레버리지 ────────────────────────────────────────────────────────────────
LEVERAGE = 5   # 바이낸스 선물 레버리지 배수 (봇 시작 시 API로 자동 세팅)

# ── 동적 비중 설정 ──────────────────────────────────────────────────────────
# 진입 시 계좌 가용 잔고(Free USDT)의 이 비율만큼 한 페어에 배분
# 14% x 5개 페어 = 70% 사용 → 30% 여유 증거금 상시 유지
# 예: 0.14 → 잔고 1000 USDT → 해당 페어 총 140 USDT (롱 70 / 숏 70)
ALLOCATION_PER_PAIR = 0.14

# 페이퍼 트레이딩 시 가상 초기 잔고 (실거래에서는 무시됨)
PAPER_INITIAL_BALANCE = 1000.0

# ── 스프레드/Z-Score 엔진 설정 ───────────────────────────────────────────────
# 가격 비율(ratio = price_A / price_B) 계산에 사용할 데이터 포인트 수
# 1초 폴링 기준: 3600 = 약 1시간
RATIO_WINDOW = 3600

# [레거시] 고정 % 기반 임계치 (하위 호환용 — 실 판단은 Z-Score 기반으로 전환됨)
ENTRY_THRESHOLD_PCT = 0.3
EXIT_THRESHOLD_PCT  = 0.05
STOP_LOSS_PCT       = 1.5

# ── 볼린저 밴드 기반 동적 진입선 (Z-Score) ────────────────────────────────────
# 하이브리드 진입: 아래 두 조건을 동시에 만족해야 진입 발생 (AND 조건)
#   조건 A: |Z-Score| >= ENTRY_Z_SCORE
#   조건 B: |괴리율(dev_pct)| >= MIN_SPREAD_THRESHOLD (%)
MIN_SPREAD_THRESHOLD = 0.5    # 0.5% — Ratio가 이동평균 대비 최소 이만큼 벌어져야 진입

ENTRY_Z_SCORE      = 2.0    # |Z| >= 2.0 → 밴드 상/하단 터치 시 진입
EXIT_Z_SCORE       = 0.5    # |Z| <= 0.5 → 평균 회귀 시 익절
STOP_LOSS_Z_SCORE  = 4.0    # |Z| >= 4.0 → 극단적 디커플링 손절

# ── 글로벌 킬 스위치 ─────────────────────────────────────────────────────────
# 봇 시작 시점 자본금 대비 이 비율만큼 손실 발생 시 전 포지션 강제 청산 + 봇 종료
# -0.05 = -5%
MAX_DRAWDOWN_LIMIT = -0.05

# ── 폴링 간격 (초) ──────────────────────────────────────────────────────────
POLL_INTERVAL_SEC = 1.0

# ── 주문 재시도 설정 ────────────────────────────────────────────────────────
ORDER_RETRY_COUNT   = 3
ORDER_RETRY_WAIT    = 2.0   # 초

# ── 로그 파일 경로 ──────────────────────────────────────────────────────────
LOG_FILE = "bot.log"

# ── 바이낸스 Taker 수수료 ────────────────────────────────────────────────────
# 시장가 1회당 0.05% → 1 사이클(진입 2회 + 청산 2회) = 0.05% * 4 = 0.2%
TAKER_FEE_RATE = 0.0005   # 0.05%

# ── 서버 재구동 시 상태 복구 파일 ────────────────────────────────────────────
STATE_FILE = "bot_state.json"

# ── BTC 시장 폭주 감지 필터 ─────────────────────────────────────────────────
# BTC 15분봉의 (High - Low) / Low * 100 이 이 값을 초과하면
# 전 페어 신규 진입을 일시 차단 (기존 포지션 청산 감시는 계속 작동)
MAX_BTC_VOLATILITY = 2.0   # 15분 변동성 2% 초과 시 폭주 판정
BTC_VOLATILITY_CHECK_INTERVAL = 30   # BTC 변동성 체크 주기 (초)
