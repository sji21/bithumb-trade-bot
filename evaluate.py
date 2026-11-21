#!/usr/bin/env python3
"""채택 전략 재검토. docs/review-2026-11.md 에 싣는 숫자는 전부 이 스크립트의 출력이다.

    python3 evaluate.py --end 2026-11-19

같은 --end 로 실행하면 같은 숫자가 나온다. 질문 여섯 개에 차례로 답한다.

  1. 전체 구간과 연도별로 단순 보유와 무엇이 다른가
  2. 평가를 시작하는 날을 바꿔도 결론이 유지되는가
  3. 낙폭이 줄어든 것은 덜 들고 있어서인가, 드나든 시점 덕인가
  4. 신호 셋을 섞은 것이 흔한 추세 필터 하나보다 나은가
  5. 가중치를 조금 바꾸면 결과가 달라지는가
  6. 수수료와 체결 시점에 얼마나 민감한가

신호와 매매 규칙은 봇이 쓰는 strategy · backtest 모듈을 그대로 불러 쓴다.
모든 평가는 지표가 온전히 계산되는 날(데이터 시작 후 201봉째)부터 한다.
"""

import argparse
import sys

import numpy as np
import pandas as pd

import backtest
import config
import strategy

ALLOC = {a: v['alloc'] for a, v in config.ASSETS.items() if v['alloc']}
# 2026-08 문서가 채택 근거로 든 6년 구간. 가중치는 이 구간에서 골랐다고 적혀 있다.
PICK_START, PICK_END = '2020-08-07', '2026-08-06'


def ts(value):
    return backtest._ts(value)


# ── 비중 계산 ────────────────────────────────────────────────────────────

def three_signals(df):
    ds = strategy.analyze_daily(df)
    return (ds.sig_fast.astype(float), ds.sig_st.astype(float), ds.sig_slow.astype(float))


def blend(sig, w_fast, w_st, w_slow, floor=None):
    floor = config.SCORE_FLOOR if floor is None else floor
    score = sig[0] * w_fast + sig[1] * w_st + sig[2] * w_slow
    return score.where(score >= floor - 1e-9, 0.0)


def single_filters(df):
    """흔한 추세 필터 하나씩. 채택 전략과 같이 전일 확정값을 쓴다."""
    c = df['close'].astype(float)
    return {
        '종가 > MA200': (c > c.rolling(200).mean()).shift(1).astype(float),
        'MA20 > MA60': (c.rolling(20).mean() > c.rolling(60).mean()).shift(1).astype(float),
        'MA50 > MA200': (c.rolling(50).mean() > c.rolling(200).mean()).shift(1).astype(float),
        '슈퍼트렌드(10,3)': strategy.supertrend(df)[0].shift(1).astype(float),
    }


# ── 평가 ─────────────────────────────────────────────────────────────────

def const_mix(close, w, fee):
    """비중 w 를 매달 첫 거래일에 다시 맞추는 고정 비중. 시작값 1."""
    cash, coin, eq, month = 1.0, 0.0, [], None
    for stamp, price in close.astype(float).items():
        if month != (stamp.year, stamp.month):
            delta = (cash + coin * price) * w - coin * price
            if delta > 0:
                spend = min(delta, cash / (1 + fee))
                coin += spend / price
                cash -= spend * (1 + fee)
            else:
                coin += delta / price
                cash += -delta * (1 - fee)
            month = (stamp.year, stamp.month)
        eq.append(cash + coin * price)
    return pd.Series(eq, index=close.index)


class Book:
    """자산별 데이터와 비중을 한 번만 계산해 두고 구간별로 평가한다."""

    def __init__(self, end):
        self.df = {a: backtest.fetch_daily(config.ASSETS[a]['binance'], end) for a in ALLOC}
        first = self.df[next(iter(ALLOC))]
        self.first = first.index[strategy.required_bars()]
        self.last = first.index[-1]
        self.sig = {a: three_signals(d) for a, d in self.df.items()}
        self.w = {'채택 3신호': {a: strategy.analyze_daily(d).weight for a, d in self.df.items()}}
        for a, d in self.df.items():
            for name, w in single_filters(d).items():
                self.w.setdefault(name, {})[a] = w

    def run(self, weights, start, end, fee=None, band=None, trade='close', lag=0):
        """(전략 곡선, 보유 곡선, 평균 노출, 연간 조정 횟수). 자산별 곡선을 배분 비율로 더한다."""
        fee = config.FEE_RATE if fee is None else fee
        start, end = ts(start), ts(end)
        strat = hold = None
        expo = n = 0.0
        for a, alloc in ALLOC.items():
            d = self.df[a].loc[start:end]
            w = weights[a].shift(lag) if lag else weights[a]
            w = w.loc[start:end]
            tp = d['open'] if trade == 'open' else None
            curve, n_rebal, _ = backtest.simulate_weights(d['close'], w, fee=fee, band=band, trade_price=tp)
            h = d['close'].astype(float) / float(d['close'].iloc[0])
            strat = curve * alloc if strat is None else strat + curve * alloc
            hold = h * alloc if hold is None else hold + h * alloc
            expo += float(w.fillna(0).clip(0, 1).mean()) * alloc
            n += n_rebal * alloc
        years = max((end - start).days, 1) / 365.25
        return strat, hold, expo, n / years

    def fixed(self, w, start, end, fee=None):
        fee = config.FEE_RATE if fee is None else fee
        start, end = ts(start), ts(end)
        return sum(const_mix(self.df[a]['close'].loc[start:end], w, fee) * alloc for a, alloc in ALLOC.items())


def stat(curve):
    """(누적 수익률 %, 연환산 %, 최대 낙폭 %, 샤프). 곡선은 시작 자본 1 기준이다.

    첫날 값으로 나누지 않는다 — 첫날 매수 수수료가 빠진 값이라 수익률이 부풀려진다.
    """
    days = max((curve.index[-1] - curve.index[0]).days, 1)
    total = float(curve.iloc[-1])
    r = curve.pct_change().dropna()
    sharpe = float(r.mean() / r.std() * np.sqrt(365)) if r.std() > 0 else 0.0
    peak = curve.cummax()
    return ((total - 1) * 100, (total ** (365.25 / days) - 1) * 100,
            float(((curve - peak) / peak * 100).min()), sharpe)


def hac_t(y, x=None, lags=10):
    """y 의 평균(또는 y = a + b·x 의 a)과 그 t 값. 자기상관을 감안한 표준오차(Newey-West)."""
    y = np.asarray(y, float)
    X = np.ones((len(y), 1)) if x is None else np.column_stack([np.ones(len(y)), np.asarray(x, float)])
    # 일부 BLAS(맥의 Accelerate)는 행렬 곱에서 헛된 부동소수점 경고를 낸다. 값은 닫힌 식 검산과 일치한다
    with np.errstate(all='ignore'):
        beta = np.linalg.lstsq(X, y, rcond=None)[0]
        u = y - X @ beta
        xu = X * u[:, None]
        S = xu.T @ xu
        for k in range(1, lags + 1):
            g = xu[k:].T @ xu[:-k]
            S += (1 - k / (lags + 1)) * (g + g.T)
        inv = np.linalg.inv(X.T @ X)
        cov = inv @ S @ inv
    return beta, beta[0] / np.sqrt(cov[0, 0])


def line(label, curve, extra=''):
    tot, cagr, mdd, sh = stat(curve)
    print(f'  {label:<18}{tot:>9.0f}%{cagr:>8.1f}%{mdd:>8.1f}%{sh:>7.2f}  {extra}')


HEAD = f'  {"":<18}{"누적":>10}{"연환산":>8}{"최대낙폭":>7}{"샤프":>6}'


# ── 질문별 출력 ──────────────────────────────────────────────────────────

def q1(b):
    print(f'\n[1] 전체 구간과 연도별  ({b.first:%Y-%m-%d} ~ {b.last:%Y-%m-%d}, 수수료 편도 {config.FEE_RATE:.2%})')
    s, h, expo, n = b.run(b.w['채택 3신호'], b.first, b.last)
    print(HEAD)
    line('채택 3신호', s, f'평균 노출 {expo:.0%} · 조정 연 {n:.1f}회(자산당)')
    line('단순 보유', h)
    print('  연도별 수익률 (전략 / 보유 / 평균 노출) · 낙폭 (전략 / 보유) · 비중 조정 횟수 (자산별)')
    for year in range(b.first.year, b.last.year + 1):
        a, z = max(ts(f'{year}-01-01'), b.first), min(ts(f'{year}-12-31'), b.last)
        s, h, expo, _ = b.run(b.w['채택 3신호'], a, z)
        # 그 해에 목표 비중이 밴드 이상 바뀐 날 수 (연초에 새로 사는 한 번은 세지 않는다)
        moves = ' · '.join(
            f"{x} {int((b.w['채택 3신호'][x].loc[a:z].diff().abs() >= config.REBAL_BAND).sum())}"
            for x in ALLOC)
        print(f'    {year}: {stat(s)[0]:>7.0f}% / {stat(h)[0]:>7.0f}% / {expo:>4.0%}'
              f'   낙폭 {stat(s)[2]:>5.0f}% / {stat(h)[2]:>5.0f}%   조정 {moves}')


def q2(b):
    print('\n[2] 평가 시작일을 바꾸면 (30일 간격으로 시작일을 옮긴 모든 구간)')
    for label, days in (('1년', 365), ('3년', 1095), ('6년', 2190)):
        rows, a = [], b.first
        while a + pd.Timedelta(days=days) <= b.last:
            s, h, _, _ = b.run(b.w['채택 3신호'], a, a + pd.Timedelta(days=days))
            rows.append((stat(s)[0], stat(h)[0], stat(s)[2], stat(h)[2]))
            a += pd.Timedelta(days=30)
        r = np.array(rows)
        print(f'  {label} 구간 {len(r)}개: 전략 수익 > 보유 수익 {np.mean(r[:, 0] > r[:, 1]):.0%}'
              f' · 수익 중앙값 {np.median(r[:, 0]):.0f}% / {np.median(r[:, 1]):.0f}%'
              f' · 낙폭 중앙값 {np.median(r[:, 2]):.0f}% / {np.median(r[:, 3]):.0f}%'
              f' · 전략 낙폭이 더 얕은 구간 {np.mean(r[:, 2] > r[:, 3]):.0%}')
    s, h, _, _ = b.run(b.w['채택 3신호'], PICK_START, PICK_END)
    print(f'  2026-08 문서가 든 6년({PICK_START} ~ {PICK_END}): 전략 {stat(s)[0]:.0f}% / 보유 {stat(h)[0]:.0f}%'
          f' · 낙폭 {stat(s)[2]:.0f}% / {stat(h)[2]:.0f}%')


def q3(b):
    print('\n[3] 덜 들고 있어서인가, 드나든 시점 덕인가')
    for label, a, z in (('전체', b.first, b.last), ('문서의 6년', PICK_START, PICK_END),
                        ('최근 3년', b.last - pd.Timedelta(days=1095), b.last)):
        s, h, expo, _ = b.run(b.w['채택 3신호'], a, z)
        f = b.fixed(expo, a, z)
        print(f'  {label} ({ts(a):%Y-%m-%d} ~ {ts(z):%Y-%m-%d})')
        print(HEAD)
        line('채택 3신호', s, f'평균 노출 {expo:.0%}')
        line(f'노출 {expo:.0%} 고정', f, '매달 비중만 다시 맞춤')
        line('단순 보유', h)
    s, h, expo, _ = b.run(b.w['채택 3신호'], b.first, b.last)
    rs, rh = s.pct_change().dropna(), h.pct_change().dropna()
    beta, t = hac_t(rs.values, rh.values)
    print(f'  보유 수익률로 설명되지 않는 부분(알파): 연 {beta[0] * 365 * 100:.1f}%p, t = {t:.2f}'
          f'  (보유에 대한 민감도 {beta[1]:.2f})')
    rf = b.fixed(expo, b.first, b.last).pct_change().dropna()
    d = (rs - rf).dropna()
    m, t = hac_t(d.values)
    print(f'  노출 고정 대비 일수익 차이: 연 {m[0] * 365 * 100:.1f}%p, t = {t:.2f}   (|t| 가 2 를 넘어야 우연이 아니라고 본다)')
    rows, a = [], b.first
    while a + pd.Timedelta(days=1095) <= b.last:
        z = a + pd.Timedelta(days=1095)
        s, h, expo, _ = b.run(b.w['채택 3신호'], a, z)
        rows.append(stat(s)[0] > stat(b.fixed(expo, a, z))[0])
        a += pd.Timedelta(days=30)
    print(f'  3년 구간 {len(rows)}개 중 전략 수익이 노출 고정보다 높은 구간 {np.mean(rows):.0%}')


def q4(b):
    print('\n[4] 흔한 추세 필터 하나와 비교')
    for label, a, z in (('전체', b.first, b.last), ('문서의 6년', PICK_START, PICK_END),
                        ('최근 3년', b.last - pd.Timedelta(days=1095), b.last)):
        print(f'  {label} ({ts(a):%Y-%m-%d} ~ {ts(z):%Y-%m-%d})')
        print(HEAD)
        for name, w in b.w.items():
            s, h, expo, n = b.run(w, a, z)
            line(name, s, f'노출 {expo:.0%} · 조정 연 {n:.1f}회')
        line('단순 보유', h)


def q5(b):
    print('\n[5] 가중치를 바꾸면 (세 신호 가중치를 0.1 단위로, 각 0.1 이상, 합 1 → 36가지. 하한 0.5 고정)')
    grid = [(i / 10, j / 10, (10 - i - j) / 10) for i in range(1, 9) for j in range(1, 10 - i)]
    mine = (config.W_FAST, config.W_ST, config.W_SLOW)
    periods = (('고르기 전 구간', b.first, ts(PICK_START) - pd.Timedelta(days=1)),
               ('고른 구간(문서의 6년)', PICK_START, PICK_END),
               ('고른 뒤 구간', ts(PICK_END) + pd.Timedelta(days=1), b.last))
    near = [(0.50, 0.30, 0.20), (0.40, 0.40, 0.20)]   # 2026-08 문서가 "비슷하다"고 든 이웃 값
    for label, a, z in periods:
        res = {}
        for g in grid + [mine] + near:
            w = {x: blend(b.sig[x], *g) for x in ALLOC}
            s, h, _, _ = b.run(w, a, z)
            res[g] = (stat(s)[0], stat(s)[2])
        rets = sorted((res[g][0] for g in grid), reverse=True)
        mdds = sorted((res[g][1] for g in grid), reverse=True)
        r, m = res[mine]
        print(f'  {label} ({ts(a):%Y-%m-%d} ~ {ts(z):%Y-%m-%d}): 보유 {stat(h)[0]:.0f}% / {stat(h)[2]:.0f}%')
        print(f'    채택값 수익 {r:.0f}% — 36가지 중 {sum(x > r for x in rets) + 1}위 (범위 {rets[-1]:.0f}% ~ {rets[0]:.0f}%, 중앙 {np.median(rets):.0f}%)')
        print(f'    채택값 낙폭 {m:.0f}% — 36가지 중 {sum(x > m for x in mdds) + 1}위 (범위 {mdds[-1]:.0f}% ~ {mdds[0]:.0f}%, 중앙 {np.median(mdds):.0f}%)')
        print('    이웃 값: ' + ' · '.join(f'{g[0]:.2f}/{g[1]:.2f}/{g[2]:.2f} → {res[g][0]:.0f}% / {res[g][1]:.0f}%' for g in near))
        best, worst = max(grid, key=lambda g: res[g][0]), min(grid, key=lambda g: res[g][0])
        print(f'    가장 높은 조합 {best[0]:.1f}/{best[1]:.1f}/{best[2]:.1f} · 가장 낮은 조합 {worst[0]:.1f}/{worst[1]:.1f}/{worst[2]:.1f}')


def q6(b):
    print(f'\n[6] 수수료와 체결 시점  ({b.first:%Y-%m-%d} ~ {b.last:%Y-%m-%d})')
    print(HEAD)
    for fee in (0.0, 0.0004, config.FEE_RATE):
        s, h, _, _ = b.run(b.w['채택 3신호'], b.first, b.last, fee=fee)
        line(f'수수료 {fee:.2%}', s)
    for label, kw in (('알림 직후(시가)', {'trade': 'open'}), ('하루 뒤 종가(기준)', {}),
                      ('이틀 뒤 종가', {'lag': 1})):
        s, h, _, _ = b.run(b.w['채택 3신호'], b.first, b.last, **kw)
        line(label, s)
    line('단순 보유', h)


def main():
    ap = argparse.ArgumentParser(description='채택 전략 재검토. 문서 숫자를 재현한다.')
    ap.add_argument('--end', required=True, help='평가 종료일 YYYY-MM-DD (UTC). 문서와 같은 날을 준다')
    ap.add_argument('--only', type=int, choices=range(1, 7), help='질문 하나만')
    args = ap.parse_args()
    b = Book(args.end)
    print(f'자산 배분 {" / ".join(f"{a} {w:.0%}" for a, w in ALLOC.items())} · 바이낸스 일봉'
          f' {b.df[next(iter(ALLOC))].index[0]:%Y-%m-%d} ~ {b.last:%Y-%m-%d}'
          f' · 평가는 {b.first:%Y-%m-%d} 부터')
    for i, q in enumerate((q1, q2, q3, q4, q5, q6), 1):
        if args.only in (None, i):
            q(b)
    return 0


if __name__ == '__main__':
    sys.exit(main())
