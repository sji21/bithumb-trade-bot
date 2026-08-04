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
ASSETS = {
    'BTC': {'binance': 'BTC/USDT', 'bithumb': 'BTC/KRW'},
    'ETH': {'binance': 'ETH/USDT', 'bithumb': 'ETH/KRW'},
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


def ensure_dirs():
    for d in (CHART_DIR, ORDER_DIR, STATE_DIR):
        os.makedirs(d, exist_ok=True)


def mode_label():
    if not LIVE_TRADING:
        return 'OFF (주문 안 함)'
    if LIVE_SIMULATION:
        return 'SIM (모의 주문)'
    return 'LIVE (실주문)'
