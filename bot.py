#!/usr/bin/env python3
"""
알림전용 EMA5/13 트레이딩 모니터
- 데이터는 Binance(USDT)에서 조회(USD 가격, EMA 신호 기준)
- 주문은 Bithumb(KRW)로 실행(실제 주문 훅은 주석처리 상태)
- .env 파일로 설정을 제공합니다. 키는 절대 여기로 붙여넣지 마세요.
"""

import os, time, math, logging
from datetime import datetime, timezone
import ccxt
import pandas as pd
import requests
from dotenv import load_dotenv

# plotting
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.dates import date2num, DateFormatter
from mplfinance.original_flavor import candlestick_ohlc

# --- 설정 불러오기 ---
# Ensure .env is loaded from the script directory so the daemon/launchd
# process finds the correct file regardless of current working directory.
_env_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(_env_path)
# Bithumb 키 (주문용, 실제 주문은 주석처리)
BITHUMB_API_KEY = os.getenv('BITHUMB_API_KEY')
BITHUMB_API_SECRET = os.getenv('BITHUMB_API_SECRET')
# Telegram
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
# 전략 파라미터
ALLOC_BTC = float(os.getenv('ALLOC_BTC', '0.5'))
ALLOC_ETH = float(os.getenv('ALLOC_ETH', '0.5'))
SLIPPAGE = float(os.getenv('SLIPPAGE', '0.001'))
BTC_INIT_STOP = float(os.getenv('BTC_INIT_STOP', '0.05'))
ETH_INIT_STOP = float(os.getenv('ETH_INIT_STOP', '0.07'))
TRAIL_ACTIVATE = float(os.getenv('TRAIL_ACTIVATE', '0.06'))
TRAIL_PCT = float(os.getenv('TRAIL_PCT', '0.03'))
POLL_INTERVAL_SEC = int(os.getenv('POLL_INTERVAL_SEC', '300'))  # default 5 minutes

# --- 로깅 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# --- 거래소 연결 ---
# Binance: 가격 데이터(USDT 마켓) - 공개 엔드포인트만 사용
binance = ccxt.binance({'enableRateLimit': True})
# Bithumb: 주문용 (API 키 필요)
bithumb = ccxt.bithumb({
    'apiKey': BITHUMB_API_KEY or '',
    'secret': BITHUMB_API_SECRET or '',
    'enableRateLimit': True,
})

# --- 유틸: 텔레그램 메시지 전송 ---
def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram 토큰/챗아이디 미설정. 메시지 생략.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': text}
    try:
        r = requests.post(url, json=payload, timeout=10)
        # log response for debugging
        logging.info('Telegram sendMessage status: %s response: %s', r.status_code, r.text)
        r.raise_for_status()
    except Exception as e:
        logging.exception("Telegram 전송 실패: %s", e)

# --- 유틸: 텔레그램 사진 전송 ---
def send_telegram_photo(path, caption=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram 토큰/챗아이디 미설정. 사진 전송 생략.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        with open(path, 'rb') as f:
            files = {'photo': f}
            data = {'chat_id': TELEGRAM_CHAT_ID}
            if caption:
                data['caption'] = caption
            r = requests.post(url, data=data, files=files, timeout=30)
            # log response for debugging
            logging.info('Telegram sendPhoto status: %s response: %s', r.status_code, r.text)
            r.raise_for_status()
    except Exception as e:
        logging.exception("Telegram photo 전송 실패: %s", e)

import fcntl
import sys

# --- single-instance lock (prevent duplicate runs) ---
_lockfile_path = '/tmp/trade_bot_bot.lock'
try:
    _lock_fh = open(_lockfile_path, 'w')
    fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
except Exception as e:
    print('Another instance is running or cannot acquire lock; exiting.', file=sys.stderr)
    sys.exit(0)

# --- 데이터 로드: Binance(USDT)에서 OHLCV 가져오기 (4h) ---
def fetch_ohlcv_binance(symbol='BTC/USDT', timeframe='4h', limit=200):
    data = binance.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(data, columns=['ts','open','high','low','close','vol'])
    df['datetime'] = pd.to_datetime(df['ts'], unit='ms')
    df.set_index('datetime', inplace=True)
    return df

# --- 현재 KRW 가격(빗썸) 조회 간단 유틸 ---
def fetch_ticker_bithumb(sym_k='BTC/KRW'):
    try:
        t = bithumb.fetch_ticker(sym_k)
        return t
    except Exception as e:
        logging.exception('Bithumb ticker fetch 실패: %s', e)
        return None

# --- 신호 계산 (EMA5/13) ---
def calc_signal(df):
    close = df['close'].astype(float)
    ema5 = close.ewm(span=5, adjust=False).mean()
    ema13 = close.ewm(span=13, adjust=False).mean()
    if len(ema5) < 2:
        return None, None
    prev_short = ema5.iloc[-2]; prev_long = ema13.iloc[-2]
    last_short = ema5.iloc[-1]; last_long = ema13.iloc[-1]
    # return signal plus latest EMA values for message
    if (prev_short <= prev_long) and (last_short > last_long):
        return 'buy', (last_short, last_long)
    if (prev_short >= prev_long) and (last_short < last_long):
        return 'sell', (last_short, last_long)
    return None, (last_short, last_long)

# --- place_order 실제 구현 (Bithumb v2) ---
import uuid, hashlib, jwt as _jwt, json as _json
from urllib.parse import urlencode

def place_order_bithumb(symbol_k, side, amount_krw):
    """Place an order on Bithumb using v2 JWT signing.
    - symbol_k: e.g. 'BTC/KRW' or 'ETH/KRW'
    - side: 'bid' or 'ask'
    - amount_krw: integer KRW to spend (for ord_type='price')
    Returns response dict or None on failure.
    """
    if not BITHUMB_API_KEY or not BITHUMB_API_SECRET:
        logging.warning('Bithumb API 키 미설정. 주문 생략.')
        return None
    market = symbol_k.replace('/','-') if '/' in symbol_k else symbol_k
    body = {'market': market, 'side': side, 'ord_type': 'price', 'price': str(int(amount_krw))}
    # create insertion-order urlencode query for hash
    qs = urlencode(list(body.items())).encode('utf-8')
    qh = hashlib.sha512(qs).hexdigest()
    payload = {'access_key': BITHUMB_API_KEY, 'nonce': str(uuid.uuid4()), 'timestamp': int(time.time()*1000), 'query_hash': qh, 'query_hash_alg': 'SHA512'}
    token = _jwt.encode(payload, BITHUMB_API_SECRET, algorithm='HS256')
    if isinstance(token, bytes):
        token = token.decode()
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    try:
        r = requests.post('https://api.bithumb.com/v1/orders', headers=headers, data=_json.dumps(body), timeout=10)
        out = {'request': body, 'status_code': r.status_code}
        try:
            out['response'] = r.json()
        except Exception:
            out['response_text'] = r.text
        # save
        os.makedirs('media/orders', exist_ok=True)
        fn = f"media/orders/live_order_{market}_{int(time.time())}.json"
        with open(fn, 'w') as f:
            _json.dump(out, f, indent=2, ensure_ascii=False)
        logging.info('Placed Bithumb order %s %sKRW status=%s', market, amount_krw, r.status_code)
        return out
    except Exception as e:
        logging.exception('Bithumb order failed: %s', e)
        return None


# --- 포지션 상태는 사용자 수동 관리(알림전용) ---
POSITIONS = {
    'BTC': {'in_position': False, 'entry_price_usd': None, 'entry_price_krw': None, 'peak_price_usd': None, 'init_stop': BTC_INIT_STOP},
    'ETH': {'in_position': False, 'entry_price_usd': None, 'entry_price_krw': None, 'peak_price_usd': None, 'init_stop': ETH_INIT_STOP},
}
# --- 중복 알림/실행 방지를 위한 상태 ---
LAST_NOTIFIED_CANDLE = {'BTC': None, 'ETH': None}  # stores timestamp of candle that triggered last notification
LAST_SIGNAL = {'BTC': None, 'ETH': None}  # stores last signal sent ('buy'/'sell'/None)
# --- last seen closed candle to avoid reprocessing same candle ---
LAST_SEEN_CLOSED_CANDLE = {'BTC': None, 'ETH': None}
# Feature flag: send chart on every closed candle (can be noisy).
# Set to False to only send on explicit signals.
SEND_ON_CANDLE_CLOSE = True

# --- Persistent last-notified storage (to survive restarts) ---
STATE_DIR = os.path.join(os.path.dirname(__file__), 'media', 'state')
STATE_FILE = os.path.join(STATE_DIR, 'last_notified.json')

def load_last_notified():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                import json
                data = json.load(f)
                for k in ('BTC','ETH'):
                    if k in data:
                        LAST_NOTIFIED_CANDLE[k] = int(data[k]) if data[k] is not None else None
                logging.info('Loaded LAST_NOTIFIED_CANDLE from %s', STATE_FILE)
    except Exception as e:
        logging.exception('Failed to load last_notified state: %s', e)


def save_last_notified():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        import json, tempfile
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(LAST_NOTIFIED_CANDLE, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logging.exception('Failed to save last_notified state: %s', e)


def set_last_notified(asset, ts):
    LAST_NOTIFIED_CANDLE[asset] = ts
    save_last_notified()

# load at startup
load_last_notified()

# --- Atomic per-candle per-asset per-type send gate ---
import json, tempfile

def _load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE,'r') as f:
                return json.load(f)
    except Exception as e:
        logging.exception('load_state fail: %s', e)
    return {}

def _save_state_atomic(data):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + '.tmp'
        with open(tmp,'w') as f:
            json.dump(data, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logging.exception('save_state_atomic fail: %s', e)


def _check_sent(asset, candle_ts, mtype):
    """Return True if already sent; otherwise False."""
    data = _load_state()
    asset_dict = data.get(asset, {})
    ts_str = str(candle_ts)
    entry = asset_dict.get(ts_str, {'text': False, 'photo': False})
    return bool(entry.get(mtype))

def _mark_sent(asset, candle_ts, mtype):
    """Mark given asset/candle/type as sent and persist state."""
    data = _load_state()
    asset_dict = data.get(asset, {})
    ts_str = str(candle_ts)
    entry = asset_dict.get(ts_str, {'text': False, 'photo': False})
    entry[mtype] = True
    asset_dict[ts_str] = entry
    # optionally prune old entries (keep last 20)
    if len(asset_dict) > 20:
        keys = sorted(asset_dict.keys())
        for k in keys[:-10]:
            asset_dict.pop(k, None)
    data[asset] = asset_dict
    _save_state_atomic(data)

# wrapper send functions (now mark only on success)
_old_send_telegram = send_telegram
_old_send_telegram_photo = send_telegram_photo

def send_once_text(asset, candle_ts, text):
    if _check_sent(asset, candle_ts, 'text'):
        logging.info('Text for %s @ %s already sent; skipping', asset, candle_ts)
        return True
    try:
        _old_send_telegram(text)
        _mark_sent(asset, candle_ts, 'text')
        return True
    except Exception as e:
        logging.exception('Failed to send text for %s @ %s: %s', asset, candle_ts, e)
        return False

def send_once_photo(asset, candle_ts, path, caption=None):
    if _check_sent(asset, candle_ts, 'photo'):
        logging.info('Photo for %s @ %s already sent; skipping', asset, candle_ts)
        return True
    try:
        _old_send_telegram_photo(path, caption)
        _mark_sent(asset, candle_ts, 'photo')
        return True
    except Exception as e:
        logging.exception('Failed to send photo for %s @ %s: %s', asset, candle_ts, e)
        return False


# --- 차트 생성 함수 (candlestick + EMA + volume + mapped signals) ---
def generate_chart_with_signals(asset, df_ohlcv, signals, out_path):
    """Generate candlestick chart with EMA overlays, volume panel, and mapped signals.
    - asset: 'BTC' or 'ETH'
    - df_ohlcv: DataFrame with columns ['kst_dt','open','high','low','close','vol','dt_num'] index kst_dt
    - signals: list of {'time': datetime, 'signal': 'buy'|'sell', 'price': float}
    - out_path: path to save PNG
    """
    try:
        ohlc = df_ohlcv.copy()
        # compute dt_num if missing
        if 'dt_num' not in ohlc.columns:
            ohlc['dt_num'] = ohlc.index.to_series().apply(lambda x: date2num(x.to_pydatetime()))
        # dynamic width
        deltas = ohlc['dt_num'].diff().dropna()
        med = deltas.median() if not deltas.empty else 0.03
        width = med * 0.6
        quote = ohlc[['dt_num','open','high','low','close']].values.tolist()
        # map signals to nearest candle index
        mapped = []
        for s in signals:
            st = s['time']
            diffs = [abs((st - t.to_pydatetime()).total_seconds()) for t in ohlc.index]
            import numpy as np
            idx = int(np.argmin(np.array(diffs)))
            mapped.append({'idx': idx, 'signal': s['signal'], 'price': s.get('price')})
        # prepare marker coords
        buy_x=[]; buy_y=[]; sell_x=[]; sell_y=[]
        for m in mapped:
            row = ohlc.iloc[m['idx']]
            dnum = row['dt_num']
            if m['signal']=='buy':
                buy_x.append(dnum); buy_y.append(row['low']*0.995)
            else:
                sell_x.append(dnum); sell_y.append(row['high']*1.005)
        # plot
        fig = plt.figure(figsize=(14,8))
        ax1 = plt.subplot2grid((5,1),(0,0),rowspan=4)
        ax2 = plt.subplot2grid((5,1),(4,0),rowspan=1, sharex=ax1)
        candlestick_ohlc(ax1, quote, width=width, colorup='green', colordown='red')
        closes = ohlc['close'].values
        ema5 = pd.Series(closes).ewm(span=5,adjust=False).mean().values
        ema13 = pd.Series(closes).ewm(span=13,adjust=False).mean().values
        ax1.plot(ohlc['dt_num'], ema5, color='blue', label='EMA5')
        ax1.plot(ohlc['dt_num'], ema13, color='orange', label='EMA13')
        if buy_x:
            ax1.scatter(buy_x, buy_y, marker='^', color='green', s=140, zorder=5, label='BUY')
        if sell_x:
            ax1.scatter(sell_x, sell_y, marker='v', color='red', s=140, zorder=5, label='SELL')
        # volume
        ax2.bar(ohlc['dt_num'], ohlc['vol'], width=width, color='grey')
        ax2.set_ylabel('Volume')
        # title (English) — omit 'Last candle:' prefix as requested
        last_kst = ohlc.index[-1]
        title = f"{asset} 4H ({last_kst.strftime('%Y-%m-%d %H:%M KST')})"
        ax1.set_title(title)
        ax1.xaxis.set_major_formatter(DateFormatter('%m-%d %H:%M'))
        plt.setp(ax1.get_xticklabels(), rotation=45)
        ax1.legend()
        ax1.set_xlim(ohlc['dt_num'].iloc[0]-width, ohlc['dt_num'].iloc[-1]+width)
        plt.tight_layout()
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        fig.savefig(out_path, dpi=150)
        plt.close(fig)
        return True
    except Exception as e:
        logging.exception('차트 생성 실패: %s', e)
        return False

# --- 메인 루프 ---
def main_loop():
    logging.info("모니터 시작: 신호는 Binance(USDT) 기준, 주문은 Bithumb(KRW)로 매뉴얼 실행 권장")
    # 매핑: Binance 심볼 -> Bithumb 심볼
    mapping = {
        'BTC': {'binance': 'BTC/USDT', 'bithumb': 'BTC/KRW'},
        'ETH': {'binance': 'ETH/USDT', 'bithumb': 'ETH/KRW'},
    }
    while True:
        try:
            for asset, syms in mapping.items():
                # Binance 데이터로 신호 계산
                try:
                    df = fetch_ohlcv_binance(syms['binance'], timeframe='4h', limit=100)
                except Exception as e:
                    logging.exception('Binance OHLCV fetch 실패 for %s: %s', syms['binance'], e)
                    continue
                # 4h timeframe in ms
                timeframe_ms = 4 * 60 * 60 * 1000
                last_candle_ts = int(df['ts'].iloc[-1]) if 'ts' in df.columns else int(df.index[-1].value//1)
                now_ms = int(datetime.utcnow().timestamp()*1000)
                # If the last candle hasn't closed yet, use closed candles only
                if now_ms < last_candle_ts + timeframe_ms:
                    # ongoing candle: use df excluding last row to compute signal on closed candles
                    df_closed = df.iloc[:-1]
                    closed_note = True
                else:
                    df_closed = df
                    closed_note = False
                if len(df_closed) < 2:
                    logging.info('데이터 부족으로 신호 계산 생략: %s', syms['binance'])
                    continue
                sig, emas = calc_signal(df_closed)
                last_usd = float(df_closed['close'].iloc[-1])
                # Bithumb 현재가도 가져와서 메시지에 포함
                ticker_k = fetch_ticker_bithumb(syms['bithumb'])
                last_krw = ticker_k['last'] if ticker_k and 'last' in ticker_k else None

                pos = POSITIONS.get(asset)
                current_candle_ts = int(df_closed.index[-1].timestamp())
                # skip processing if we already processed this closed candle
                if LAST_SEEN_CLOSED_CANDLE.get(asset) == current_candle_ts:
                    logging.info('이미 처리된 캔들입니다 - 스킵: %s %s', asset, df_closed.index[-1])
                    continue
                # compute integer EMAs and difference for messaging
                ema5_val = int(round(emas[0])) if emas and emas[0] is not None else None
                ema13_val = int(round(emas[1])) if emas and emas[1] is not None else None
                ema_diff = int(round((emas[0]-emas[1]))) if emas and emas[0] is not None and emas[1] is not None else None

                # compute RSI(14) on closed series
                try:
                    close_series = df_closed['close'].astype(float)
                    delta = close_series.diff()
                    up = delta.clip(lower=0)
                    down = -1 * delta.clip(upper=0)
                    # Use Wilder's smoothing (alpha = 1/period) for RSI
                    roll_up = up.ewm(alpha=1/14, adjust=False).mean()
                    roll_down = down.ewm(alpha=1/14, adjust=False).mean()
                    rs = roll_up / roll_down
                    rsi = 100 - (100 / (1 + rs))
                    rsi_val = int(round(rsi.iloc[-1]))
                except Exception:
                    rsi_val = None

                # Prepare status message for this closed candle and always send in debug mode
                # Use candle CLOSE time (start_time + timeframe) for both UTC and KR display
                candle_start = df_closed.index[-1]
                candle_close_utc = (candle_start + pd.Timedelta(hours=4))
                candle_time_utc = candle_close_utc.strftime('%Y-%m-%d %H:%M:%S')
                try:
                    candle_ts = pd.Timestamp(candle_close_utc).tz_localize('UTC').tz_convert('Asia/Seoul')
                    candle_time_krw_hm = candle_ts.strftime('%H:%M')
                    candle_time_krw_date = candle_ts.strftime('%m-%d %H:%M')
                except Exception:
                    candle_time_krw_hm = (candle_start + pd.Timedelta(hours=4)).strftime('%H:%M')
                    candle_time_krw_date = (candle_start + pd.Timedelta(hours=4)).strftime('%m-%d %H:%M')

                signal_label = sig.upper() if sig in ('buy','sell') else 'NONE'
                # formatted numbers with commas
                last_usd_fmt = f"{int(round(last_usd)):,}"
                last_krw_fmt = f"{int(round(last_krw)):,}" if isinstance(last_krw,(int,float)) else last_krw
                ema5_fmt = f"{ema5_val:,}" if ema5_val is not None else 'N/A'
                ema13_fmt = f"{ema13_val:,}" if ema13_val is not None else 'N/A'
                ema_diff_fmt = f"{ema_diff:,}" if ema_diff is not None else 'N/A'

                # Compact one-line summary + short detail lines (Markdown)
                status_summary = f"{asset} {last_usd_fmt} USD / {last_krw_fmt} KRW · Signal: {signal_label} · candle_close UTC {candle_time_utc} / (KR {candle_time_krw_date})"
                status_detail = (
                    f"• EMA5: `{ema5_fmt}`  • EMA13: `{ema13_fmt}`  • diff: `{ema_diff_fmt}`\n"
                    f"• RSI(14): `{rsi_val if rsi_val is not None else 'N/A'}`\n"
                    f"• 종가(USD): `{last_usd_fmt}`  • Bithumb(KRW): `{last_krw_fmt}`\n"
                    f"• 조회: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}`"
                )
                status_text = status_summary + "\n\n" + status_detail

                # send status message only when a signal exists (reduce noise)
                if sig in ('buy','sell'):
                    send_telegram(status_text)
                    logging.info('상태 전송: %s', status_summary)
                else:
                    logging.debug('No signal; skipping routine status send for %s', asset)

                # Prepare signals history for mapping (we'll forward recent signals including this one)
                signals_history = []
                # build signals from closed history (last 100 closed candles)
                raw = df_closed.copy()
                if raw.index.tz is None:
                    raw.index = pd.to_datetime(raw.index).tz_localize('UTC')
                raw_kst = raw.index.tz_convert('Asia/Seoul')
                for i in range(2, len(raw_kst)):
                    window = raw.iloc[:i+1]
                    s_sig, s_emas = calc_signal(window)
                    if s_sig in ('buy','sell'):
                        # use the exact window end index (UTC-aware) as canonical timestamp
                        window_end = window.index[-1]
                        if window_end.tzinfo is None:
                            window_end = pd.to_datetime(window_end).tz_localize('UTC')
                        time_kst = window_end.tz_convert('Asia/Seoul')
                        signals_history.append({'time': time_kst.to_pydatetime(), 'signal': s_sig, 'price': float(window['close'].iloc[-1]), 'idx_label': str(window_end)})
                # debug: log signals history summary
                try:
                    import json as _json_tmp
                    logging.info('signals_history len=%s last=%s', len(signals_history), _json_tmp.dumps(signals_history[-3:], default=str))
                except Exception:
                    pass

                # Generate chart on every new closed 4h candle (even if no signal), but avoid duplicating if signal chart is sent below
                # We'll generate the chart only when we haven't processed this candle before and there is no signal (sig is None)
                if SEND_ON_CANDLE_CLOSE and LAST_SEEN_CLOSED_CANDLE.get(asset) != current_candle_ts and sig is None:
                    try:
                        ohlc_df = df_closed.copy()
                        ohlc_df['kst_dt'] = pd.to_datetime(ohlc_df.index).tz_localize('UTC').tz_convert('Asia/Seoul')
                        ohlc_df.set_index('kst_dt', inplace=True)
                        ohlc_df.rename(columns={'open':'open','high':'high','low':'low','close':'close','vol':'vol'}, inplace=True)
                        out_path = f"media/charts/{asset}_4h.png"
                        gen_ok = generate_chart_with_signals(asset, ohlc_df[['open','high','low','close','vol']].assign(dt_num=lambda x: x.index.to_series().apply(lambda t: date2num(t.to_pydatetime()))), signals_history, out_path)
                        if gen_ok:
                            caption = f"{asset} 4H | Candle closed\nEMA5 {ema5_val} EMA13 {ema13_val} | Close {last_usd_fmt} USD / {last_krw_fmt} KRW"
                            send_telegram_photo(out_path, caption)
                            logging.info('Candle-close chart sent for %s', asset)
                    except Exception as e:
                        logging.exception('Candle-close chart generation failed: %s', e)

                # Then handle signal alerts (but still ensure duplicate protection)
                if sig == 'buy':
                    # determine spend amount based on holding threshold (10,000 KRW)
                    try:
                        # fetch latest accounts to compute available KRW and coin holdings
                        payload_acc={'access_key':BITHUMB_API_KEY,'nonce':str(uuid.uuid4()),'timestamp':int(time.time()*1000)}
                        token_acc = _jwt.encode(payload_acc, BITHUMB_API_SECRET, algorithm='HS256')
                        if isinstance(token_acc,bytes): token_acc=token_acc.decode()
                        headers_acc={'Authorization':f'Bearer {token_acc}'}
                        r_acc = requests.get('https://api.bithumb.com/v1/accounts', headers=headers_acc, timeout=10)
                        accounts = r_acc.json()
                        krw_avail=0.0
                        coin_avail=0.0
                        for it in accounts:
                            if it.get('currency')=='KRW':
                                krw_avail = float(it.get('balance') or 0) - float(it.get('locked') or 0)
                            if it.get('currency')==asset:
                                coin_avail = float(it.get('balance') or 0) - float(it.get('locked') or 0)
                        last_price = last_krw if isinstance(last_krw,(int,float)) else None
                        coin_krw_value = (coin_avail * last_price) if last_price else 0
                        # decide spend
                        fee_rate = 0.0025
                        usable_ratio = 0.995
                        min_total = 5000
                        if coin_krw_value >= 10000:
                            # already holds >=10k KRW worth -> spend all available
                            spend = int( (krw_avail / (1 + fee_rate)) * usable_ratio )
                        else:
                            # not held enough -> spend 50% of available
                            spend = int( (krw_avail * 0.5 / (1 + fee_rate)) * usable_ratio )
                        if spend < min_total:
                            logging.info('매수 금액(%s KRW) 최소 주문 금액 미만으로 주문 생략 for %s', spend, asset)
                        else:
                            # place order
                            market_sym = f"{asset}/KRW"
                            logging.info('Placing buy for %s amount KRW %s', asset, spend)
                            order_res = place_order_bithumb(market_sym, 'bid', spend)
                            if order_res:
                                send_telegram(f"[실행] BUY {asset} KRW {spend} status {order_res.get('status_code')}")
                    except Exception as e:
                        logging.exception('BUY 처리 중 오류: %s', e)
                    # mark notified and signal
                    set_last_notified(asset, current_candle_ts)
                    LAST_SIGNAL[asset] = 'buy'
# mark this closed candle as seen to avoid reprocessing on next poll
                LAST_SEEN_CLOSED_CANDLE[asset] = current_candle_ts
                # 알림전용: 포지션 자동 변경이나 주문 자동 실행은 하지 않습니다.
            time.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            logging.info('사용자 중단(KeyboardInterrupt). 종료합니다.')
            break
        except Exception as e:
            logging.exception('메인 루프 예외: %s', e)
            time.sleep(5)

if __name__ == '__main__':
    main_loop()
