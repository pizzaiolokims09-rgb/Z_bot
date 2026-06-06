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

# ── 거래 대상 페어 목록 (A심볼, B심볼) — 20개 유니버스 ────────────────────────
# 바이낸스 선물 표기법: spot 심볼로 입력하면 내부에서 :USDT 변환
PAIRS_TO_TRADE = [
    ("BTC/USDT",      "ETH/USDT"),         # 시총 1위-2위
    ("LTC/USDT",      "BCH/USDT"),         # OG 결제형
    ("XRP/USDT",      "XLM/USDT"),         # 크로스보더 결제
    ("ADA/USDT",      "TRX/USDT"),         # L1 스마트컨트랙트
    ("ATOM/USDT",     "TIA/USDT"),         # 코스모스 생태계
    ("SOL/USDT",      "AVAX/USDT"),        # 고성능 L1
    ("ARB/USDT",      "OP/USDT"),          # 이더리움 L2
    ("APT/USDT",      "SUI/USDT"),         # Move 기반 L1
    ("POL/USDT",      "NEAR/USDT"),        # L1 인프라
    ("UNI/USDT",      "AAVE/USDT"),        # DeFi 블루칩
    ("HBAR/USDT",     "ALGO/USDT"),        # 엔터프라이즈 L1
    ("SEI/USDT",      "INJ/USDT"),         # 신세대 L1
    ("LINK/USDT",     "DOT/USDT"),         # 인프라/크로스체인
    ("FIL/USDT",      "AR/USDT"),          # 탈중앙 스토리지
    ("STX/USDT",      "ORDI/USDT"),        # 비트코인 생태계
    ("RENDER/USDT",   "FET/USDT"),         # AI 섹터
    ("WLD/USDT",      "ARKM/USDT"),        # 샘 알트먼 & AI 테마
    # ("JUP/USDT",      "RAY/USDT"),         # 솔라나 DEX 테마 (Testnet 미지원, 실계좌 전환 시 주석 해제)
    # ("DOGE/USDT",     "1000SHIB/USDT"),    # 밈 대형 (Top 15)
    # ("1000PEPE/USDT", "1000BONK/USDT"),    # 밈 대형 (Top 30)
]

# ── 레버리지 ────────────────────────────────────────────────────────────────
LEVERAGE = 5   # 바이낸스 선물 레버리지 배수 (봇 시작 시 API로 자동 세팅)

# ── 최대 동시 진입 슬롯 ─────────────────────────────────────────────────────
# 20개 페어를 감시하되, 동시에 포지션을 보유할 수 있는 최대 페어 수
MAX_ACTIVE_PAIRS = int(os.getenv("MAX_ACTIVE_PAIRS", "5"))

# ── 동적 비중 설정 ──────────────────────────────────────────────────────────
# 진입 시 계좌 가용 잔고(Free USDT)의 이 비율만큼 한 페어에 배분
# 14% x MAX_ACTIVE_PAIRS(5)개 = 70% 사용 → 30% 여유 증거금 상시 유지
# 예: 0.14 → 잔고 1000 USDT → 해당 페어 총 140 USDT (롱 70 / 숏 70)
ALLOCATION_PER_PAIR = 0.14

# 페이퍼 트레이딩 시 가상 초기 잔고 (실거래에서는 무시됨)
PAPER_INITIAL_BALANCE = 1000.0

# ── 듀얼 윈도우 스프레드/Z-Score 엔진 설정 ───────────────────────────────────
# Window A (스윙 타점): 24시간 — 묵직한 펀더멘털 괴리 사냥
RATIO_WINDOW_SWING = 28800   # 3초 폴링 x 28800 = 24시간
ENTRY_Z_SCORE_SWING = 2.5   # |Z| >= 2.5 → 스윙 진입

# Window B (단기 타점): 12시간 — 급등락 순간 극단적 이격 사냥
RATIO_WINDOW_SHORT = 14400  # 3초 폴링 x 14400 = 12시간
ENTRY_Z_SCORE_SHORT = 3.0   # |Z| >= 3.0 → 단기 진입

# [레거시] 고정 % 기반 임계치 (하위 호환용 — 실 판단은 Z-Score 기반으로 전환됨)
ENTRY_THRESHOLD_PCT = 0.3
EXIT_THRESHOLD_PCT  = 0.05
STOP_LOSS_PCT       = 2.0    # 2.0% — 고정 괴리율 손절 (1.5 → 2.0 상향, 휩쏘 방어)

# ── 볼린저 밴드 기반 동적 진입선 (Z-Score) ────────────────────────────────────
# 하이브리드 진입: 아래 두 조건을 동시에 만족해야 진입 발생 (AND 조건)
#   조건 A: |Z-Score| >= ENTRY_Z_SCORE_SWING or ENTRY_Z_SCORE_SHORT
#   조건 B: |괴리율(dev_pct)| >= MIN_SPREAD_THRESHOLD (%)
MIN_SPREAD_THRESHOLD = 2.5    # 2.5% — 잔파도 진입 원천 차단 (1.5 → 2.5 상향)

EXIT_Z_SCORE       = 0.7    # |Z| <= 0.7 → 빠른 회전율 확보 (0.3 → 0.7 완화)
STOP_LOSS_Z_SCORE  = 3.8    # |Z| >= 3.8 → 통계적 꼬리 끊기 (4.5 → 3.8 축소, 손익비 교정)

# ── 목표 순수익(Net PnL) 기반 익절 ────────────────────────────────────────────
# Z-Score 회귀를 기다리지 않아도, 왕복 수수료 차감 후 순수익이
# 이 비율(%)에 도달하면 즉시 전량 청산 (Take Profit)
TARGET_NET_PNL_PCT = 3.5    # +3.5% 순수익 도달 시 익절

# ── 하드 스탑 (Hard Stop) — 최대 허용 손실률 (손익비 1:1) ─────────────────────
# Z-Score 손절(STOP_LOSS_Z_SCORE)에 도달하지 않았더라도,
# 실시간 미실현 순손실(Net PnL %)이 이 비율보다 더 내려가면 즉시 시장가 전량 손절
MAX_LOSS_PCT = -12.0    # -12.0% 이하 시 하드 스탑 발동 (블랙스완 방어용)

# ── 스마트 손절: 단기 상관관계 붕괴 감지 ──────────────────────────────────────
# 페어 두 코인의 최근 N분 1분봉 종가로 피어슨 상관계수를 실시간 계산
# 상관계수가 임계치 이하(음수 = 역방향 진행)면 커플링 전제 붕괴로 즉시 손절
CORR_WINDOW_MIN    = 60     # 상관계수 계산 윈도우 (60분)
CORR_STOP_THRESHOLD = 0.0   # 상관계수가 이 값 이하면 스마트 손절 발동

# ── 손절 후 재진입 쿨다운 ────────────────────────────────────────────────────
# 손절 발생 후 동일 페어에 재진입하기까지 최소 대기 시간 (초)
# 진입→손절 무한 루프 방지용 (30분 -> 4시간으로 대폭 연장)
STOP_LOSS_COOLDOWN_SEC = int(os.getenv("STOP_LOSS_COOLDOWN_SEC", "14400"))  # 4시간

# ── 페어별 일일 손절 제한 ────────────────────────────────────────────────────
# 한 페어에서 이 횟수 이상 손절 발생 시 당일 해당 페어 거래 중단
MAX_STOP_LOSS_PER_PAIR = 3

# ── 최대 보유 시간 ───────────────────────────────────────────────────────────
# 포지션 보유 시간이 이 값(초)을 초과하고 Net PnL이 0 이하이면 강제 청산
MAX_HOLD_SECONDS = 172800   # 48시간 (7200 → 172800 대폭 상향, 평균 회귀 대기)

# ── 추세 필터 ────────────────────────────────────────────────────────────────
# ratio의 선형 회귀 기울기가 이 값을 초과하면 추세로 판정 → 신규 진입 차단
TREND_SLOPE_THRESHOLD = 0.00015   # 0.0003 → 0.00015 하향 (추세 필터 강화)

# ── 워밍업 최소 비율 ─────────────────────────────────────────────────────────
# RATIO_WINDOW의 이 비율 이상 채워야 신호 활성화 (0.5 = 50%)
MIN_WARMUP_RATIO = 0.5

# ── 글로벌 킬 스위치 ─────────────────────────────────────────────────────────
# 봇 시작 시점 자본금 대비 이 비율만큼 손실 발생 시 전 포지션 강제 청산 + 봇 종료
# -0.05 = -5%
MAX_DRAWDOWN_LIMIT = -0.05

# ── 폴링 간격 (초) ──────────────────────────────────────────────────────────
POLL_INTERVAL_SEC = 3.0   # 3초 폴링 (Dual Loop 적용으로 Rate Limit 방어 & 슬리피지 최소화)

# ── 주문 재시도 설정 ────────────────────────────────────────────────────────
ORDER_RETRY_COUNT   = 3
ORDER_RETRY_WAIT    = 2.0   # 초

# ── 로그 파일 경로 ──────────────────────────────────────────────────────────
LOG_FILE = "bot.log"

# ── 바이낸스 수수료 ──────────────────────────────────────────────────────────
# 시장가(Taker) 1회당 0.05%
TAKER_FEE_RATE = 0.0005   # 0.05%
# 지정가(Maker) 1회당 0.02%
MAKER_FEE_RATE = 0.0002   # 0.02%

# 바이낸스 선물 최소 주문 명목가치 (USDT) — 이 미만이면 진입 스킵
MIN_NOTIONAL_USDT = 5.5   # 실제 규칙 5 USDT, 여유분 포함 5.5

# ── 지정가(Maker) 익절 설정 ──────────────────────────────────────────────────
# 지정가 익절 주문 후 미체결 시 시장가 Fallback 전환 대기 시간 (초)
MAKER_ORDER_TIMEOUT = 5   # 60초에서 5초로 단축

# ── 서버 재구동 시 상태 복구 파일 ────────────────────────────────────────────
STATE_FILE = "bot_state.json"

# ── BTC 시장 폭주 감지 필터 ─────────────────────────────────────────────────
# BTC 15분봉의 (High - Low) / Low * 100 이 이 값을 초과하면
# 전 페어 신규 진입을 일시 차단 (기존 포지션 청산 감시는 계속 작동)
MAX_BTC_VOLATILITY = 2.0   # 15분 변동성 2% 초과 시 폭주 판정
BTC_VOLATILITY_CHECK_INTERVAL = 30   # BTC 변동성 체크 주기 (초)

# ── 워밍업 배치 크기 ─────────────────────────────────────────────────────────
# 20페어 동시 워밍업 시 Rate Limit 방어를 위해 이 개수씩 배치 처리
WARMUP_BATCH_SIZE = 5

# ── 휩쏘 방지용 진입 확인 필터 ────────────────────────────────────────────────
# Z-Score가 진입 문턱을 넘은 후, 이 시간(초) 동안 계속 유지될 때만 진입
# 휩쏘(1분 내 급등락 후 복귀)로 인한 허위 진입을 방지
ENTRY_CONFIRMATION_SEC = 30   # 30초 확인 매매

# ── 섹터 제한형 스캐너 설정 ──────────────────────────────────────────────────
# 스캐너가 작동할 때, 전체 시장이 아닌 선택된 섹터 안에서만 페어 조합을 검증
SECTORS = {
    "L1_Major":  ["BTC", "ETH", "SOL", "AVAX"],
    "L1_NewGen": ["APT", "SUI", "SEI", "INJ"],
    "L1_Infra":  ["ADA", "TRX", "POL", "NEAR", "HBAR", "ALGO"],
    "L2":        ["ARB", "OP"],
    "DeFi":      ["UNI", "AAVE", "LINK", "DOT"],
    "AI":        ["RENDER", "FET", "WLD", "ARKM"],
    "Storage":   ["FIL", "AR"],
    "BTC_Eco":   ["STX", "ORDI"],
    "Payment":   ["XRP", "XLM", "LTC", "BCH"],
    "Meme":      ["DOGE", "1000SHIB", "1000PEPE", "1000BONK"],
}

# 스캔 파라미터
SCAN_CORR_THRESHOLD = 0.7      # 상관계수 최소값
SCAN_ADF_PVALUE     = 0.05     # ADF 검정 p-value 최대값 (공적분 유의수준)
SCAN_DATA_HOURS     = 336      # 스캔 데이터 기간 (14일 x 24h = 336개 1h봉, 표본 N>=300 확보)
SCAN_TIMEFRAME      = "1h"     # 스캔용 캔들 타임프레임
SCAN_FETCH_DELAY    = 0.3      # 코인별 fetch_ohlcv 호출 간 딜레이 (초, Rate Limit 방어)
