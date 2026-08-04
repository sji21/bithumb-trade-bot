"""바이낸스 OHLCV 조회 (신호 계산용 USD 시세, 공개 엔드포인트만 사용)."""

import logging

import ccxt
import pandas as pd

import config

_exchange = ccxt.binance({'enableRateLimit': True})

_COLUMNS = ['ts', 'open', 'high', 'low', 'close', 'vol']


def fetch_ohlcv(symbol, timeframe=None, limit=None):
    """UTC 인덱스를 가진 OHLCV DataFrame. 실패하면 None."""
    timeframe = timeframe or config.TIMEFRAME
    limit = limit or config.CANDLE_LIMIT
    try:
        rows = _exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception as e:
        logging.warning('바이낸스 OHLCV 조회 실패 (%s): %s', symbol, e)
        return None
    if not rows:
        logging.warning('바이낸스 OHLCV 응답 없음: %s', symbol)
        return None
    df = pd.DataFrame(rows, columns=_COLUMNS)
    df.index = pd.to_datetime(df['ts'], unit='ms', utc=True)
    df.index.name = 'datetime'
    return df


def drop_unclosed(df, hours=None):
    """진행 중인 마지막 캔들을 제외한다.

    신호는 닫힌 캔들로만 계산해야 한다. 진행 중인 캔들을 쓰면 같은 캔들 안에서
    EMA 가 교차했다 풀렸다 하면서 주문이 오갈 수 있다.
    """
    hours = hours or config.TIMEFRAME_HOURS
    if df is None or df.empty:
        return df
    span_ms = hours * 3600 * 1000
    last_open = int(df['ts'].iloc[-1])
    now_ms = int(pd.Timestamp.utcnow().timestamp() * 1000)
    if now_ms < last_open + span_ms:
        return df.iloc[:-1]
    return df
