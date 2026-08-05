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


# ── 일봉 3신호 비중 전략 ────────────────────────────────────────────────────
DailySignals = namedtuple(
    'DailySignals',
    'close ma_fast ma_mid ma_slow_fast ma_slow bb_up bb_lo rsi vol '
    'st_up st_line sig_fast sig_slow sig_st score weight',
)


def supertrend(df, period=None, mult=None):
    """ATR 기반 슈퍼트렌드. (상승 여부 시리즈, 추세선 시리즈)."""
    period = period or config.ST_PERIOD
    mult = mult if mult is not None else config.ST_MULT
    high, low, close = (df[k].astype(float) for k in ('high', 'low', 'close'))
    hl2 = (high + low) / 2
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    upper, lower = hl2 + mult * atr, hl2 - mult * atr

    fu, fl = upper.copy(), lower.copy()
    for i in range(1, len(close)):
        fu.iloc[i] = (upper.iloc[i] if (upper.iloc[i] < fu.iloc[i-1]
                                        or close.iloc[i-1] > fu.iloc[i-1]) else fu.iloc[i-1])
        fl.iloc[i] = (lower.iloc[i] if (lower.iloc[i] > fl.iloc[i-1]
                                        or close.iloc[i-1] < fl.iloc[i-1]) else fl.iloc[i-1])

    up = pd.Series(True, index=close.index)
    line = pd.Series(float('nan'), index=close.index)
    for i in range(1, len(close)):
        if close.iloc[i] > fu.iloc[i-1]:
            up.iloc[i] = True
        elif close.iloc[i] < fl.iloc[i-1]:
            up.iloc[i] = False
        else:
            up.iloc[i] = up.iloc[i-1]
        line.iloc[i] = fl.iloc[i] if up.iloc[i] else fu.iloc[i]
    return up, line


def analyze_daily(df):
    """일봉 3신호와 목표 비중을 계산한다.

    신호는 모두 shift(1) 로 전일 확정값을 쓴다. 당일 종가로 판단하면
    미래를 참조하게 된다.
    """
    c = df['close'].astype(float)
    ma_fast = c.rolling(config.DAILY_FAST).mean()
    ma_mid = c.rolling(config.DAILY_MID).mean()
    ma_sf = c.rolling(config.DAILY_SLOW_FAST).mean()
    ma_slow = c.rolling(config.DAILY_SLOW).mean()
    sd = c.rolling(config.DAILY_FAST).std()
    st_up, st_line = supertrend(df)

    sig_fast = (ma_fast > ma_mid).shift(1)
    sig_slow = (ma_sf > ma_slow).shift(1)
    sig_st = st_up.shift(1)
    score = (sig_fast.astype(float) * config.W_FAST
             + sig_st.astype(float) * config.W_ST
             + sig_slow.astype(float) * config.W_SLOW)
    weight = score.where(score >= config.SCORE_FLOOR - 1e-9, 0.0)

    return DailySignals(
        close=c, ma_fast=ma_fast, ma_mid=ma_mid, ma_slow_fast=ma_sf, ma_slow=ma_slow,
        bb_up=ma_fast + 2 * sd, bb_lo=ma_fast - 2 * sd,
        rsi=_rsi(c, config.RSI_PERIOD), vol=df['vol'].astype(float),
        st_up=st_up, st_line=st_line,
        sig_fast=sig_fast, sig_slow=sig_slow, sig_st=sig_st,
        score=score, weight=weight,
    )


def daily_state(ds):
    """마지막 일봉 기준 현재 상태. 리포트용 dict."""
    return {
        'fast': bool(ds.sig_fast.iloc[-1]),
        'slow': bool(ds.sig_slow.iloc[-1]),
        'st': bool(ds.sig_st.iloc[-1]),
        'score': float(ds.score.iloc[-1]),
        'weight': float(ds.weight.iloc[-1]),
        'prev_weight': float(ds.weight.iloc[-2]) if len(ds.weight) > 1 else None,
        'rsi': float(ds.rsi.iloc[-1]),
        'close': float(ds.close.iloc[-1]),
    }


def last_signal_before(ind, offset=1):
    """마지막 캔들 이전의 가장 최근 신호. (인덱스 라벨, 신호, 종가) 또는 None."""
    sigs = ind.signal.iloc[:-offset] if offset else ind.signal
    for i in range(len(sigs) - 1, -1, -1):
        if sigs.iloc[i] in ('buy', 'sell'):
            return sigs.index[i], sigs.iloc[i], float(ind.close.iloc[i])
    return None
