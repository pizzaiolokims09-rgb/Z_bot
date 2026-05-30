# Z_bot — 통계적 차익거래 페어 트레이딩 봇

바이낸스 선물(USDT-M) 기반 5개 페어 동시 감시, Z-Score 하이브리드 진입, 자동 위험 관리, 텔레그램 실시간 제어가 통합된 자동화 트레이딩 봇입니다.

---

## 프로젝트 구조

```
Z_bot/
└── pair_bot/
    ├── main.py              # 메인 실행 진입점 (5개 페어 병렬 루프)
    ├── config.py            # 전역 설정값 (임계치, 레버리지, 수수료 등)
    ├── spread_engine.py     # Z-Score / 괴리율 계산 (하이브리드 진입 신호)
    ├── risk_manager.py      # 포지션 사이징 / 손절 판단
    ├── order_executor.py    # CCXT 비동기 주문 실행 + 레버리지 세팅
    ├── bot_state.py         # 공유 상태 객체 (포지션, PnL, 잔고)
    ├── telegram_bot.py      # 텔레그램 알림 + 인라인 컨트롤 패널
    ├── trade_logger.py      # 매매 결과 CSV 비동기 기록
    ├── state_persistence.py # bot_state.json 저장/복구
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
pip install ccxt python-telegram-bot aiofiles python-dotenv
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

# 완전 내부 가상 시뮬레이션(API 연결 없음) 시:
# IS_PAPER_TRADING=true
# USE_TESTNET=false
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

정상 실행 시 터미널 출력:

```
[거래소] 데모 트레이딩 모드 활성화 (demo.binance.com)
[API 연결 성공] 모의투자 계좌 가용 잔고: 5000.00 USDT
[레버리지] BTC/USDT:USDT → 5x 세팅 완료
...
[텔레그램] 봇 폴링 시작
```

종료: `Ctrl+C`

---

## 주요 설정값 (config.py)

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `LEVERAGE` | 5 | 선물 레버리지 배수 (봇 시작 시 API 자동 적용) |
| `ALLOCATION_PER_PAIR` | 0.14 | 페어당 자금 배분 비율 (14%) |
| `ENTRY_Z_SCORE` | 2.0 | 진입 Z-Score 임계치 |
| `EXIT_Z_SCORE` | 0.5 | 익절 Z-Score 임계치 |
| `STOP_LOSS_Z_SCORE` | 4.0 | 손절 Z-Score 임계치 |
| `MIN_SPREAD_THRESHOLD` | 0.5 | 하이브리드 진입 최소 괴리율 (%) |
| `MAX_DRAWDOWN_LIMIT` | -0.05 | 킬 스위치 발동 손실 한도 (-5%) |
| `MAX_BTC_VOLATILITY` | 2.0 | BTC 15분 변동성 필터 임계치 (%) |
| `TAKER_FEE_RATE` | 0.0005 | 바이낸스 Taker 수수료 (0.05%) |
| `POLL_INTERVAL_SEC` | 1.0 | 가격 조회 주기 (초) |

---

## 핵심 매매 로직

### 감시 페어 (5개)

| 페어 ID | 심볼 A | 심볼 B |
|---------|--------|--------|
| BTC-ETH | BTC/USDT | ETH/USDT |
| SOL-AVAX | SOL/USDT | AVAX/USDT |
| XRP-ADA | XRP/USDT | ADA/USDT |
| DOGE-1000SHIB | DOGE/USDT | 1000SHIB/USDT |
| LINK-DOT | LINK/USDT | DOT/USDT |

### 하이브리드 진입 조건 (AND)

아래 두 조건을 동시에 충족할 때만 양방향 시장가 주문 실행:

- **조건 A**: 가격 비율의 Z-Score ≥ 2.0 (볼린저 밴드 기준)
- **조건 B**: 실시간 가격 괴리율 ≥ 0.5%

### 청산 조건

| 유형 | 조건 |
|------|------|
| 익절 | Z-Score ≤ 0.5 이면서 Net PnL > 0 |
| 손절 | Z-Score ≥ 4.0 |
| 수동 | 텔레그램 버튼 |

### Net PnL 계산

```
Net PnL = 총 실현손익 - 수수료 (Taker 0.05% × 4회 = 0.2%)
```

---

## 방어 로직 3종

### 1. Legging Risk (짝짝이 체결) 방어

양방향 주문 실행 후 한쪽만 체결됐을 경우, 체결된 쪽을 즉시 시장가 롤백하여 단방향 노출(Naked Position) 원천 차단.

### 2. 글로벌 킬 스위치

초기 잔고 대비 누적 손실이 **-5%** 를 초과하면 전 포지션 강제 청산 후 봇 자동 종료.

### 3. BTC 시장 폭주 감지 필터

BTC/USDT 15분봉 변동성 `(High-Low)/Low×100` 이 **2%** 초과 시 신규 진입 전면 차단.
기존 포지션의 익절/손절 감시는 계속 작동.

---

## 텔레그램 제어

텔레그램에서 `/start` 입력 시 하단 고정 키보드 활성화:

| 버튼 | 기능 |
|------|------|
| 🔘 수동 청산 | 특정 페어 즉시 시장가 청산 |
| 🛑 봇 정지 | 신규 진입 일시 중단 (기존 포지션 감시 유지) |
| ▶️ 봇 재시작 | 신규 진입 재개 |
| 📊 상태 확인 | 현재 잔고 + 활성 포지션 목록 |
| 💰 승률 & PnL | 누적 거래 통계 |

수동 청산 / 봇 정지 / 봇 재시작은 오터치 방지를 위해 확인 버튼 1회 추가.

---

## 데이터 파일

### trade_history.csv

매 거래 완료 시 자동 기록 (ML 파라미터 최적화 활용 목적):

```
Entry_Time, Exit_Time, Pair, Trade_Duration_Sec,
Entry_Z_Score, Exit_Z_Score, PnL_USDT, PnL_Percent, Exit_Reason
```

### bot_state.json

봇 재구동 시 이전 포지션 상태와 누적 통계 자동 복구.
진입/청산 시마다 즉시 덮어쓰기 저장.

---

## 모드 전환 안내

| 목적 | IS_PAPER_TRADING | USE_TESTNET |
|------|-----------------|-------------|
| 내부 가상 시뮬레이션 | `true` | 무관 |
| 바이낸스 데모 계정 연동 | `false` | `true` |
| 실거래 (Mainnet) | `false` | `false` |

> 실거래 전환 시 반드시 Mainnet API 키로 교체 후 충분한 데모 검증을 완료하십시오.
