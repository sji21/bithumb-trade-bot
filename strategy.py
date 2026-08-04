"""지표 계산과 EMA 크로스 신호.

기존 구현은 캔들 하나하나마다 앞부분을 잘라내어 EMA 를 처음부터 다시 계산했다
(캔들 100개 × 자산 2개 × 폴링마다 2회). 여기서는 전체 시리즈에 대해 한 번만
계산하고 교차 지점을 벡터 연산으로 찾는다. 부수 효과로 차트에 찍히는 과거
신호가 실매매에 쓰는 신호와 같은 방식으로 계산되어 서로 어긋나지 않는다.
"""

from collections import namedtuple

import pandas as pd

import config

Indicators = namedtuple(
    'Indicators',
    'ema_fast ema_slow diff signal rsi atr close prev_close volume avg_volume',
)


def _ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def _rsi(close, period):
    """Wilder 방식 RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def _atr(df, period):
    """True Range 의 Wilder 평균."""
    high, low, close = df['high'], df['low'], df['close']
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def signal_series(ema_fast, ema_slow):
    """캔들별 'buy'/'sell'/None 시리즈.

    교차 판정은 단일 캔들 판정과 동일하다:
        buy  : 직전 diff <= 0 이고 현재 diff > 0
        sell : 직전 diff >= 0 이고 현재 diff < 0
    """
    diff = ema_fast - ema_slow
    prev = diff.shift(1)
    out = pd.Series([None] * len(diff), index=diff.index, dtype=object)
    out[(prev <= 0) & (diff > 0)] = 'buy'
    out[(prev >= 0) & (diff < 0)] = 'sell'
    return out


def analyze(df):
    """닫힌 캔들 DataFrame 을 받아 지표 일체를 계산한다."""
    close = df['close'].astype(float)
    ema_fast = _ema(close, config.EMA_FAST)
    ema_slow = _ema(close, config.EMA_SLOW)
    volume = df['vol'].astype(float)
    return Indicators(
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        diff=ema_fast - ema_slow,
        signal=signal_series(ema_fast, ema_slow),
        rsi=_rsi(close, config.RSI_PERIOD),
        atr=_atr(df, config.ATR_PERIOD),
        close=close,
        prev_close=close.shift(1),
        volume=volume,
        avg_volume=volume.rolling(20, min_periods=1).mean(),
    )


def latest_signal(ind):
    """마지막 닫힌 캔들의 신호('buy'/'sell'/None)."""
    if len(ind.signal) == 0:
        return None
    value = ind.signal.iloc[-1]
    return value if value in ('buy', 'sell') else None


def signal_history(df, ind, tz=config.KST):
    """차트에 찍을 과거 신호 목록."""
    index = df.index
    if index.tz is None:
        index = pd.to_datetime(index).tz_localize('UTC')
    out = []
    for ts, sig, price in zip(index, ind.signal, ind.close):
        if sig in ('buy', 'sell'):
            out.append({
                'time': ts.tz_convert(tz).to_pydatetime(),
                'signal': sig,
                'price': float(price),
                'idx_label': str(ts),
            })
    return out


def last_signal_before(ind, offset=1):
    """마지막 캔들 이전의 가장 최근 신호. (인덱스 라벨, 신호, 종가) 또는 None."""
    sigs = ind.signal.iloc[:-offset] if offset else ind.signal
    for i in range(len(sigs) - 1, -1, -1):
        if sigs.iloc[i] in ('buy', 'sell'):
            return sigs.index[i], sigs.iloc[i], float(ind.close.iloc[i])
    return None
