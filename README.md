# Z_bot — 통계적 차익거래 페어 트레이딩 봇

바이낸스 선물(USDT-M) 기반 동시 감시, Z-Score 하이브리드 진입, 자동 위험 관리, **이중 방어 체계(스마트 손절 + 하드 스탑)** 및 텔레그램 실시간 제어가 통합된 자동화 트레이딩 봇입니다.

---

## 프로젝트 구조

```text
Z_bot/
└── pair_bot/
    ├── main.py              # 메인 실행 진입점 (통합 청산 체인 및 페어 병렬 루프)
    ├── config.py            # 전역 설정값 (임계치, 레버리지, 수수료, 섹터 등)
    ├── spread_engine.py     # Z-Score / 괴리율 계산 (하이브리드 진입 신호)
    ├── risk_manager.py      # 포지션 사이징 / 마진 관리
    ├── pair_scanner.py      # 비동기 섹터 스캐너 (ADF/상관계수 기반 페어 발굴)
    ├── order_executor.py    # CCXT 비동기 주문 실행 + 레버리지 세팅
    ├── bot_state.py         # 공유 상태 객체 (포지션, PnL, 잔고, 대기 중인 교체 큐)
    ├── telegram_bot.py      # 텔레그램 알림 + 인라인 컨트롤 패널 (스캔/교체 UI)
    ├── trade_logger.py      # 매매 결과 CSV 비동기 기록
    ├── state_persistence.py # bot_state.json 저장/복구 (기억 상실 방지)
    ├── .env                 # API 키, 텔레그램 토큰 (Git 제외)
    ├── .env.example         # 환경 변수 작성 예시
    ├── trade_history.csv    # 매매 기록 (자동 생성)
    └── bot_state.json       # 재구동 시 상태 복구용 (자동 생성)
```

---

## 환경 설정

### 1. 가상환경 생성 및 패키지 설치

```powershell
# Windows PowerShell (pair_bot 폴더 기준)
python -m venv venv
venv\Scripts\Activate.ps1
pip install ccxt python-telegram-bot aiofiles python-dotenv numpy statsmodels
```

### 2. `.env` 파일 작성

`pair_bot/.env` 파일을 아래 형식으로 생성합니다:

```env
# 바이낸스 API
BINANCE_API_KEY=여기에_API_키
BINANCE_API_SECRET=여기에_시크릿_키

# 텔레그램 봇
TELEGRAM_BOT_TOKEN=여기에_봇_토큰
TELEGRAM_CHAT_ID=여기에_채팅_ID

# 거래 모드
# 바이낸스 데모 계정(demo.binance.com) 연동 시:
IS_PAPER_TRADING=false
USE_TESTNET=true
```

| 변수 | 설명 |
|------|------|
| `BINANCE_API_KEY` | 바이낸스 데모 계정 API 키 (demo.binance.com > API 관리) |
| `BINANCE_API_SECRET` | API 시크릿 키 |
| `TELEGRAM_BOT_TOKEN` | @BotFather에서 발급한 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 봇과 대화한 채팅방 ID |
| `IS_PAPER_TRADING` | `false` = 바이낸스 API 연동, `true` = 내부 가상 계좌 |
| `USE_TESTNET` | `true` = demo.binance.com 연결 |

---

## 구동 방법

```powershell
# 1. 가상환경 활성화
venv\Scripts\Activate.ps1

# 2. pair_bot 폴더에서 실행
cd pair_bot
python main.py
```

종료: `Ctrl+C`

---

## 주요 설정값 (config.py)

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `LEVERAGE` | 5 | 선물 레버리지 배수 |
| `ALLOCATION_PER_PAIR` | 0.14 | 페어당 자금 배분 비율 (14%) |
| `TARGET_NET_PNL_PCT` | 3.5 | **목표 순수익 익절 한도 (%)** |
| `MAX_LOSS_PCT` | -3.5 | **하드 스탑 최대 허용 손실률 (%)** |
| `ENTRY_Z_SCORE` | 2.0 | 진입 Z-Score 임계치 |
| `STOP_LOSS_Z_SCORE` | 4.5 | **통계적 디커플링 손절 임계치** (잔파도 무시용) |
| `CORR_WINDOW_MIN` | 60 | **스마트 손절** 상관계수 측정 단위 (분) |
| `CORR_STOP_THRESHOLD`| 0.0 | **스마트 손절** 발동 임계치 (0 이하 역방향 시 손절) |
| `MAX_DRAWDOWN_LIMIT` | -0.05 | 글로벌 킬 스위치 발동 전체 손실 한도 (-5%) |

---

## 핵심 매매 로직 및 5단계 청산 체인

### 하이브리드 진입 조건 (AND)

아래 두 조건을 동시에 충족할 때만 양방향 시장가 주문 실행:
- **조건 A**: 가격 비율의 Z-Score ≥ 2.0 (볼린저 밴드 기준)
- **조건 B**: 실시간 가격 괴리율 ≥ 0.5%

### 통합 청산 우선순위 체인 (손익비 1:1 이중 방어)

포지션 진입 후 10초마다 아래 5단계 우선순위로 상태를 검사합니다. 계산 시 수수료를 차감한 진짜 **Net PnL**을 사용합니다.

| 우선순위 | 조건 | 실행 내용 |
|:---:|:---|:---|
| **1** | **Net PnL ≥ +3.5%** | **목표 수익 익절:** 즉각적인 시장가 익절 처리 |
| **2** | **피어슨 상관계수 ≤ 0.0** | **스마트 손절:** 60분 간의 1분봉 상관계수가 0 이하(역방향 진행)일 경우 즉각 꼬리 끊기 |
| **3** | **Net PnL ≤ -3.5%** | **하드 스탑:** Z-Score 도달 여부와 무관하게 순손실이 -3.5%를 넘어가면 무조건 손절 |
| **4** | **abs(Z-Score) ≥ 4.5** | **통계 손절:** 통계적으로 극단적인 디커플링 진입 시 손절 |
| **5** | **Z-Score 회귀 (EXIT)** | **회귀 익절:** Z-Score가 0을 향해 완전히 관통하고 Net PnL이 양수일 경우 익절 |

---

## 자동화된 섹터 스캐너 및 안전 교체 (Pending Swap)

### 1. 백그라운드 스캐너 (`pair_scanner.py`)
텔레그램의 "페어 스캔" 버튼을 통해 L1 메이저, DeFi, AI, Meme 등 **10가지 섹터**의 코인들을 비동기/논블로킹으로 스캔합니다.
ADF(Augmented Dickey-Fuller) 공적분 테스트와 피어슨 상관계수를 조합하여 가장 매매하기 좋은 최적의 페어를 발굴합니다.

### 2. 안전 교체 시스템 (Pending Swap)
기존에 진입해 있는 포지션이 있을 경우 즉시 교체하지 않고 **예약 대기(Pending) 상태**로 둡니다. 이후 5단계 청산 체인에 의해 기존 포지션이 익절/손절되어 빈자리가 나면, 봇이 즉시 새로운 페어로 바통 터치를 수행하여 끊김 없이 매매를 이어갑니다.

---

## 방어 로직 3종

### 1. Legging Risk (짝짝이 체결) 방어
양방향 주문 실행 후 한쪽만 체결됐을 경우, 체결된 쪽을 즉시 시장가 롤백하여 단방향 노출(Naked Position) 원천 차단.

### 2. 글로벌 킬 스위치
초기 잔고 대비 누적 손실이 **-5%** 를 초과하면 전 포지션 강제 청산 후 봇 자동 종료.

### 3. BTC 시장 폭주 감지 필터
BTC/USDT 15분봉 변동성 `(High-Low)/Low×100` 이 **2%** 초과 시 신규 진입 전면 차단. 기존 포지션의 익절/손절 감시는 유지.

---

## 텔레그램 제어

텔레그램에서 `/start` 입력 시 하단 고정 키보드 활성화:

| 버튼 | 기능 |
|------|------|
| 🔘 수동 청산 | 특정 페어 즉시 시장가 청산 |
| 🛑 봇 정지 / ▶️ 봇 재시작 | 신규 진입 일시 중단 및 재개 (기존 포지션 유지) |
| 📊 상태 확인 | 현재 잔고 + 활성 포지션 목록 |
| 💰 승률 & PnL | 누적 거래 통계 |
| 🔍 페어 스캔 | 10개 섹터 스캔 및 실시간 페어 교체 (Pending Swap) |

---

> 실거래 전환 시 반드시 Mainnet API 키로 교체 후 충분한 데모 검증을 완료하십시오.
