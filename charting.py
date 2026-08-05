"""캔들 차트 생성 (캔들 + EMA + 거래량 + 신호 마커)."""

import logging
import os

import matplotlib

matplotlib.use('Agg')

import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.dates import DateFormatter, date2num  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402
from mplfinance.original_flavor import candlestick_ohlc  # noqa: E402

import config  # noqa: E402

# 차트 라벨이 한글이라 한글 글리프가 있는 폰트를 우선한다.
# 없으면 matplotlib 기본 폰트로 떨어지며 글자가 네모로 나온다.
plt.rcParams['font.family'] = ['AppleGothic', 'Apple SD Gothic Neo', 'Malgun Gothic',
                               'NanumGothic', 'sans-serif']
plt.rcParams['axes.unicode_minus'] = False


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


# ── 일봉 리포트 차트 (5단) ──────────────────────────────────────────────────
_SURFACE, _INK, _INK2, _INK3, _GRID = '#fcfcfb', '#171a1d', '#565d64', '#8a8984', '#e6e5e1'
_MA_F, _MA_M, _MA_S = '#2a78d6', '#eb6834', '#1baf7a'   # 범주형 (팔레트 검증 통과)
_ST_UP, _ST_DN = '#0f7a45', '#b03026'                    # 슈퍼트렌드 방향 (의미색)
_RSI_C = '#4a3aa7'
_UP, _DOWN = '#1a9c5b', '#d94436'                        # 캔들
_BB_FILL, _BB_EDGE = '#eceef0', '#c9ccd0'
_ON, _OFF = '#3c5a78', '#e4e7ea'
_WEIGHT = '#9a6b24'


def render_daily(asset, df, ds, out_path, alloc=None):
    """일봉 5단 리포트 차트.

    ① 가격(캔들·볼린저·MA3종·슈퍼트렌드·비중전환 마커) ② 거래량 ③ RSI
    ④ 신호 스트립 ⑤ 목표 비중 계단
    """
    try:
        import matplotlib.pyplot as plt
        n = min(config.DAILY_CHART_DAYS, len(df))
        if n < 30:
            logging.warning('%s: 일봉이 부족해 리포트 차트를 건너뜁니다', asset)
            return False
        sl = slice(-n, None)
        v = df.iloc[sl]
        idx = pd.to_datetime(v.index)
        if idx.tz is None:
            idx = idx.tz_localize('UTC')
        idx = idx.tz_convert(config.KST)
        x = [date2num(t.to_pydatetime()) for t in idx]
        w = ds.weight.iloc[sl]

        fig = plt.figure(figsize=(15, 12.5), facecolor=_SURFACE)
        gs = fig.add_gridspec(5, 1, height_ratios=[3.2, .7, 1.0, .55, 1.05], hspace=.09)
        axp, axv, axr, axs, axw = (fig.add_subplot(gs[i]) for i in range(5))
        for ax in (axp, axv, axr, axs, axw):
            ax.set_facecolor(_SURFACE)
            for sp in ax.spines.values():
                sp.set_visible(False)
            ax.tick_params(colors=_INK2, labelsize=9, length=0)
            ax.set_xlim(x[0] - 2, x[-1] + 2)

        # ① 가격
        axp.fill_between(x, ds.bb_lo.iloc[sl], ds.bb_up.iloc[sl], color=_BB_FILL, zorder=1)
        for band in (ds.bb_up, ds.bb_lo):
            axp.plot(x, band.iloc[sl], color=_BB_EDGE, lw=.8, ls='--', zorder=2)
        o, c, h, l = (v[k].astype(float) for k in ('open', 'close', 'high', 'low'))
        cols = [_UP if b >= a else _DOWN for a, b in zip(o, c)]
        axp.vlines(x, l, h, color=cols, lw=.8, zorder=3)
        axp.bar(x, (c - o).abs().clip(lower=(h.max() - l.min()) * .001),
                bottom=pd.concat([o, c], axis=1).min(axis=1), width=.62, color=cols, zorder=4)
        for series, col, lab in ((ds.ma_fast, _MA_F, f'MA{config.DAILY_FAST}'),
                                 (ds.ma_mid, _MA_M, f'MA{config.DAILY_MID}'),
                                 (ds.ma_slow, _MA_S, f'MA{config.DAILY_SLOW}')):
            axp.plot(x, series.iloc[sl], color=col, lw=1.5, zorder=5, label=lab)

        stv, stl = ds.st_up.iloc[sl].values, ds.st_line.iloc[sl].values
        seg = 0
        for i in range(1, len(stv) + 1):
            if i == len(stv) or stv[i] != stv[seg]:
                axp.plot(x[seg:i], stl[seg:i], color=_ST_UP if stv[seg] else _ST_DN,
                         lw=1.9, zorder=5, solid_capstyle='round')
                seg = i
        axp.plot([], [], color=_ST_UP, lw=1.9, label='슈퍼트렌드 상승')
        axp.plot([], [], color=_ST_DN, lw=1.9, label='슈퍼트렌드 하락')

        lo_r, hi_r = float(l.min()), float(h.max())
        pad = (hi_r - lo_r) * .035
        for i in range(1, len(w)):
            if w.iloc[i] == w.iloc[i-1]:
                continue
            rise = w.iloc[i] > w.iloc[i-1]
            col = _UP if rise else _DOWN
            anchor = float(l.iloc[i]) - pad if rise else float(h.iloc[i]) + pad
            axp.scatter([x[i]], [anchor], marker='^' if rise else 'v', s=90,
                        color=col, edgecolors=_SURFACE, linewidths=1.0, zorder=8)
            axp.annotate(f'{w.iloc[i]*100:.0f}%', (x[i], anchor), color=col,
                         fontsize=8.5, fontweight='bold', zorder=9, ha='center',
                         va='top' if rise else 'bottom',
                         xytext=(0, -15 if rise else 15), textcoords='offset points',
                         bbox=dict(boxstyle='round,pad=0.18', facecolor=_SURFACE,
                                   edgecolor='none', alpha=.88))
        axp.set_ylim(lo_r - pad * 5.0, hi_r + pad * 7.5)
        axp.grid(True, color=_GRID, lw=.6, zorder=0); axp.set_axisbelow(True)
        axp.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f'{y:,.0f}'))
        axp.legend(loc='upper left', fontsize=8.5, framealpha=.95, ncol=5)
        axp.set_ylabel('USD', color=_INK2, fontsize=9)
        chg = (float(c.iloc[-1]) / float(c.iloc[0]) - 1) * 100
        tag = '  ·  관찰용' if alloc == 0 else ''
        axp.set_title(f'{asset}   ·   최근 {n}일 일봉   ·   {idx[0]:%Y-%m-%d} ~ {idx[-1]:%Y-%m-%d}   '
                      f'({chg:+.1f}%)   ·   ▲▼ = 비중 전환{tag}',
                      color=_INK, fontsize=12.5, loc='left', pad=12, fontweight='bold')

        # ② 거래량
        vol = ds.vol.iloc[sl]
        axv.bar(x, vol, width=.62, color=cols, alpha=.5)
        axv.plot(x, vol.rolling(20).mean(), color=_INK3, lw=1.1)
        axv.annotate('거래량 · 20일 평균', (.008, .78), xycoords='axes fraction',
                     color=_INK2, fontsize=8.5)
        axv.set_yticks([]); axv.grid(True, color=_GRID, lw=.6, zorder=0); axv.set_axisbelow(True)

        # ③ RSI
        rr = ds.rsi.iloc[sl]
        axr.axhspan(30, 70, color='#f3f2ef', zorder=0)
        axr.fill_between(x, 0, 30, where=(rr < 30).values, color=_UP, alpha=.16, zorder=1)
        for lv in (30, 50, 70):
            axr.axhline(lv, color=_INK3, lw=.8, ls='--' if lv != 50 else ':', zorder=2)
            axr.annotate(str(lv), (x[0], lv), color=_INK3, fontsize=7.5, va='center',
                         ha='right', xytext=(-5, 0), textcoords='offset points')
        axr.plot(x, rr, color=_RSI_C, lw=1.3, zorder=3)
        axr.set_ylim(0, 100); axr.set_yticks([])
        axr.annotate(f'RSI({config.RSI_PERIOD}) — 현재 {float(rr.iloc[-1]):.0f}',
                     (.008, .80), xycoords='axes fraction', color=_INK2, fontsize=8.5)

        # ④ 신호 스트립
        rows = [(f'단기 MA{config.DAILY_FAST}/{config.DAILY_MID}', ds.sig_fast.iloc[sl]),
                ('슈퍼트렌드', ds.sig_st.iloc[sl]),
                (f'장기 MA{config.DAILY_SLOW_FAST}/{config.DAILY_SLOW}', ds.sig_slow.iloc[sl])]
        for j, (name, s) in enumerate(rows):
            y = len(rows) - 1 - j
            vals = s.fillna(False).values.astype(bool)
            start = 0
            for i in range(1, len(vals) + 1):
                if i == len(vals) or vals[i] != vals[start]:
                    axs.barh(y, x[i-1] - x[start] + 1, left=x[start], height=.62,
                             color=_ON if vals[start] else _OFF, zorder=3)
                    start = i
            axs.annotate(name, (x[0] - 4, y), color=_INK2, fontsize=8.5, va='center',
                         ha='right', annotation_clip=False)
        axs.set_ylim(-.6, len(rows) - .4); axs.set_yticks([]); axs.set_xticks([])
        axs.annotate('신호 (진하면 켜짐)', (.008, 1.06), xycoords='axes fraction',
                     color=_INK2, fontsize=8.5)

        # ⑤ 목표 비중
        cur = float(w.iloc[-1]) * 100
        axw.step(x, w.values * 100, where='post', color=_WEIGHT, lw=1.8, zorder=3)
        axw.fill_between(x, 0, w.values * 100, step='post', color=_WEIGHT, alpha=.15, zorder=2)
        for lv in (0, 55, 80, 100):
            axw.axhline(lv, color=_GRID, lw=.7, zorder=1)
        axw.set_ylim(-4, 128); axw.set_yticks([0, 55, 80, 100])
        axw.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f'{y:.0f}%'))
        n_chg = int((w != w.shift()).sum() - 1)
        extra = (f'  (배분 {alloc:.0%} 적용 시 전체 {cur*alloc:.0f}%)'
                 if alloc else '  (배분 0% — 관찰용)')
        axw.annotate(f'목표 비중 — 현재 {cur:.0f}%{extra}   ·   {n}일간 변경 {n_chg}회',
                     (.008, 1.03), xycoords='axes fraction', color=_INK2, fontsize=8.5)
        axw.plot([x[-1]], [cur], marker='o', ms=7, color=_WEIGHT, zorder=5, clip_on=False)
        axw.xaxis.set_major_formatter(DateFormatter('%Y-%m'))

        for ax in (axp, axv, axr, axs):
            ax.set_xticklabels([])
        fig.tight_layout()
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        fig.savefig(out_path, dpi=130, facecolor=_SURFACE, bbox_inches='tight')
        plt.close(fig)
        return True
    except Exception:
        logging.exception('%s 일봉 리포트 차트 생성 실패', asset)
        import matplotlib.pyplot as plt
        plt.close('all')
        return False


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
