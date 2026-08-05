"""설정 · 경로 · 로깅.

이 모듈은 import 하는 것만으로 로깅을 초기화한다.
logging.basicConfig() 는 프로세스에서 처음 logging 을 호출하기 전에 실행돼야 하며,
그렇지 않으면 파이썬이 기본 핸들러를 붙여버려 이후 설정이 조용히 무시된다.
"""

import logging
import os

from dotenv import load_dotenv

# ── 로깅 (다른 무엇보다 먼저) ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)

# ── 경로 ────────────────────────────────────────────────────────────────────
# launchd 는 WorkingDirectory 를 workspace 로 두고 실행하므로 상대경로를 쓰면
# 산출물이 스크립트 디렉터리 밖에 흩어진다. 모든 경로를 이 파일 기준으로 고정한다.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = os.path.join(BASE_DIR, 'media')
CHART_DIR = os.path.join(MEDIA_DIR, 'charts')
ORDER_DIR = os.path.join(MEDIA_DIR, 'orders')
STATE_DIR = os.path.join(MEDIA_DIR, 'state')

SEND_STATE_FILE = os.path.join(STATE_DIR, 'last_notified.json')       # 캔들별 전송 여부
SIGNAL_STATE_FILE = os.path.join(STATE_DIR, 'last_signal_candle.json')  # 주문 처리한 캔들

LOCK_FILE = '/tmp/trade_bot_bot.lock'

load_dotenv(os.path.join(BASE_DIR, '.env'))


def _bool_env(name, default=False):
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on')


def _float_env(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        logging.warning('%s 값을 해석할 수 없어 기본값 %s 사용', name, default)
        return float(default)


def _int_env(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        logging.warning('%s 값을 해석할 수 없어 기본값 %s 사용', name, default)
        return int(default)


# ── 자격증명 ────────────────────────────────────────────────────────────────
BITHUMB_API_KEY = os.getenv('BITHUMB_API_KEY')
BITHUMB_API_SECRET = os.getenv('BITHUMB_API_SECRET')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# ── 실행 모드 ───────────────────────────────────────────────────────────────
# LIVE_TRADING 이 꺼져 있으면 주문 자체를 만들지 않는다.
# LIVE_SIMULATION 이 켜져 있으면 LIVE_TRADING 과 무관하게 실주문을 보내지 않고
# mock 영수증만 남긴다(라이브와 동일한 경로를 안전하게 밟아보기 위한 스위치).
LIVE_TRADING = _bool_env('ENABLE_LIVE_TRADING', False)
LIVE_SIMULATION = _bool_env('ENABLE_LIVE_SIMULATION', False)

# ── 전략 · 실행 파라미터 ────────────────────────────────────────────────────
# alloc = 포트폴리오 배분 비중. 0 이면 차트·신호만 보고 매매하지 않는다(관찰용).
ASSETS = {
    'BTC': {'binance': 'BTC/USDT', 'bithumb': 'BTC/KRW', 'alloc': 0.70},
    'ETH': {'binance': 'ETH/USDT', 'bithumb': 'ETH/KRW', 'alloc': 0.30},
    'SOL': {'binance': 'SOL/USDT', 'bithumb': 'SOL/KRW', 'alloc': 0.00},
}

TIMEFRAME = os.getenv('TIMEFRAME', '4h')
TIMEFRAME_HOURS = _int_env('TIMEFRAME_HOURS', 4)
CANDLE_LIMIT = _int_env('CANDLE_LIMIT', 100)
POLL_INTERVAL_SEC = _int_env('POLL_INTERVAL_SEC', 300)

EMA_FAST = _int_env('EMA_FAST', 5)
EMA_SLOW = _int_env('EMA_SLOW', 13)
RSI_PERIOD = _int_env('RSI_PERIOD', 14)
ATR_PERIOD = _int_env('ATR_PERIOD', 14)

FEE_RATE = _float_env('FEE_RATE', 0.0025)          # 빗썸 시장가 수수료 0.25%
USABLE_RATIO = _float_env('USABLE_RATIO', 0.995)   # 잔고 전량 주문 시 여유분
MIN_ORDER_KRW = _int_env('MIN_ORDER_KRW', 5000)    # 빗썸 최소 주문 금액
HOLD_THRESHOLD_KRW = _int_env('HOLD_THRESHOLD_KRW', 10000)

# 인증 실패 알림 재발송 간격(초). 같은 사유로 도배되지 않도록 제한한다.
AUTH_ALERT_INTERVAL_SEC = _int_env('AUTH_ALERT_INTERVAL_SEC', 3600)

# 신호가 없는 캔들에도 차트를 보낼지 여부.
SEND_ON_CANDLE_CLOSE = _bool_env('SEND_ON_CANDLE_CLOSE', True)

KST = 'Asia/Seoul'

# ── 일봉 3신호 비중 전략 ────────────────────────────────────────────────────
# 백테스트로 채택한 규칙. 4시간봉 EMA 크로스는 시세 확인용으로만 남기고,
# 실제 판단은 이 일봉 전략을 따른다.
#   점수 = 0.45×(MA20>MA60) + 0.35×(슈퍼트렌드 상승) + 0.20×(MA50>MA200)
#   점수 < 0.5 → 0%,  점수 ≥ 0.5 → 점수만큼 (55~100%)
DAILY_FAST, DAILY_MID = _int_env('DAILY_FAST', 20), _int_env('DAILY_MID', 60)
DAILY_SLOW_FAST, DAILY_SLOW = _int_env('DAILY_SLOW_FAST', 50), _int_env('DAILY_SLOW', 200)
ST_PERIOD = _int_env('ST_PERIOD', 10)
ST_MULT = _float_env('ST_MULT', 3.0)

W_FAST = _float_env('W_FAST', 0.45)     # MA20/60
W_ST = _float_env('W_ST', 0.35)         # 슈퍼트렌드
W_SLOW = _float_env('W_SLOW', 0.20)     # MA50/200
SCORE_FLOOR = _float_env('SCORE_FLOOR', 0.50)
REBAL_BAND = _float_env('REBAL_BAND', 0.10)

DAILY_CHART_DAYS = _int_env('DAILY_CHART_DAYS', 180)   # 차트에 보여줄 기간
DAILY_HISTORY_DAYS = _int_env('DAILY_HISTORY_DAYS', 480)  # MA200 워밍업 포함 수집량
# 일봉은 UTC 00:00 (KST 09:00) 에 마감된다. 그 직후 리포트를 보낸다.
DAILY_REPORT_DELAY_MIN = _int_env('DAILY_REPORT_DELAY_MIN', 5)


def ensure_dirs():
    for d in (CHART_DIR, ORDER_DIR, STATE_DIR):
        os.makedirs(d, exist_ok=True)


def mode_label():
    if not LIVE_TRADING:
        return 'OFF (주문 안 함)'
    if LIVE_SIMULATION:
        return 'SIM (모의 주문)'
    return 'LIVE (실주문)'
