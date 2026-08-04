"""캔들 차트 생성 (캔들 + EMA + 거래량 + 신호 마커)."""

import logging
import os

import matplotlib

matplotlib.use('Agg')

import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.dates import DateFormatter, date2num  # noqa: E402
from mplfinance.original_flavor import candlestick_ohlc  # noqa: E402

import config  # noqa: E402


def _to_kst_frame(df):
    """UTC 인덱스 OHLCV 를 KST 인덱스 + matplotlib 날짜 숫자 컬럼으로 변환."""
    out = df[['open', 'high', 'low', 'close', 'vol']].copy()
    index = pd.to_datetime(out.index)
    if index.tz is None:
        index = index.tz_localize('UTC')
    out.index = index.tz_convert(config.KST)
    out['dt_num'] = [date2num(ts.to_pydatetime()) for ts in out.index]
    return out


def _marker_points(ohlc, signals):
    """신호를 해당 캔들 위치에 매핑한다."""
    label_to_pos = {}
    for pos, ts in enumerate(ohlc.index):
        label_to_pos[ts] = pos

    buys, sells = [], []
    for s in signals:
        ts = pd.Timestamp(s.get('idx_label')) if s.get('idx_label') else None
        pos = None
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.tz_localize('UTC')
            pos = label_to_pos.get(ts.tz_convert(config.KST))
        if pos is None:
            # 라벨로 못 찾으면 시간이 가장 가까운 캔들로 붙인다.
            target = pd.Timestamp(s['time'])
            diffs = [abs((target - ts).total_seconds()) for ts in ohlc.index]
            pos = int(min(range(len(diffs)), key=diffs.__getitem__)) if diffs else None
        if pos is None:
            continue
        row = ohlc.iloc[pos]
        if s['signal'] == 'buy':
            buys.append((row['dt_num'], row['low'] * 0.995))
        else:
            sells.append((row['dt_num'], row['high'] * 1.005))
    return buys, sells


def render(asset, df, ind, signals, out_path):
    """차트를 그려 저장한다. 성공하면 True."""
    try:
        ohlc = _to_kst_frame(df)
        if len(ohlc) < 2:
            logging.warning('%s: 캔들이 부족해 차트를 건너뜁니다', asset)
            return False

        widths = ohlc['dt_num'].diff().dropna()
        width = (widths.median() if not widths.empty else 0.03) * 0.6

        fig = plt.figure(figsize=(14, 8))
        ax_price = plt.subplot2grid((5, 1), (0, 0), rowspan=4)
        ax_vol = plt.subplot2grid((5, 1), (4, 0), rowspan=1, sharex=ax_price)

        candlestick_ohlc(
            ax_price,
            ohlc[['dt_num', 'open', 'high', 'low', 'close']].values.tolist(),
            width=width, colorup='green', colordown='red',
        )
        ax_price.plot(ohlc['dt_num'], ind.ema_fast.values, color='blue',
                      linewidth=1.2, label=f'EMA{config.EMA_FAST}')
        ax_price.plot(ohlc['dt_num'], ind.ema_slow.values, color='orange',
                      linewidth=1.2, label=f'EMA{config.EMA_SLOW}')

        buys, sells = _marker_points(ohlc, signals)
        if buys:
            ax_price.scatter(*zip(*buys), marker='^', color='green', s=140, zorder=5, label='BUY')
        if sells:
            ax_price.scatter(*zip(*sells), marker='v', color='red', s=140, zorder=5, label='SELL')

        ax_vol.bar(ohlc['dt_num'], ohlc['vol'], width=width, color='grey')
        ax_vol.set_ylabel('Volume')

        last_kst = ohlc.index[-1]
        ax_price.set_title(f"{asset} {config.TIMEFRAME} ({last_kst.strftime('%Y-%m-%d %H:%M KST')})")
        ax_price.xaxis.set_major_formatter(DateFormatter('%m-%d %H:%M'))
        ax_price.legend(loc='upper left')
        ax_price.set_xlim(ohlc['dt_num'].iloc[0] - width, ohlc['dt_num'].iloc[-1] + width)
        plt.setp(ax_price.get_xticklabels(), rotation=45)
        plt.tight_layout()

        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        fig.savefig(out_path, dpi=150)
        plt.close(fig)
        return True
    except Exception:
        logging.exception('%s 차트 생성 실패', asset)
        plt.close('all')
        return False
