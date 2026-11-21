#!/usr/bin/env python3
"""EMA 크로스 전략 백테스트.

봇이 실제로 쓰는 strategy 모듈을 그대로 불러 쓴다. 신호 로직을 여기서 다시
구현하면 실제 매매와 어긋난 결과를 보게 되므로 의도적으로 재사용한다.

주의(결과를 읽을 때 감안할 것):
  - 일봉 전략은 평가 구간보다 앞선 데이터로 지표를 미리 채운 뒤 평가한다.
    구간 안의 데이터만 쓰면 처음 200일은 MA200 이 없어 신호가 꺼진 것으로
    처리되고, 그 구간의 등락이 전략 성과에 섞인다 (2026-11 재검토에서 확인).
  - 평가 구간은 --start/--end 로 고정한다. 고정하지 않으면 실행한 날에 따라
    숫자가 달라져 문서와 대조할 수 없다.
  - 체결가를 캔들 종가로 가정한다. 일봉 신호는 전일 확정값이므로 실제 알림
    (마감 직후)보다 하루 늦은 체결이다. 4시간봉 EMA 는 신호 캔들의 종가다.
  - 신호는 바이낸스 USD 시세로 계산하고 주문은 빗썸 KRW 로 나가지만,
    여기서는 USD 단일 통화로 계산한다. 환율 변동은 반영되지 않는다.
  - 수수료는 양방향 각각 FEE_RATE(기본 0.25%)를 적용한다.
"""

import argparse
import sys
import time

import pandas as pd

import config
import strategy
from market import _exchange

INITIAL_CAPITAL = 10_000.0
HISTORY_START = '2017-08-01'      # 바이낸스 BTC/ETH 일봉이 시작되는 시점


def _ts(value):
    """날짜 문자열이나 Timestamp 를 UTC Timestamp 로."""
    t = pd.Timestamp(value)
    return t.tz_localize('UTC') if t.tzinfo is None else t.tz_convert('UTC')


def fetch_history(symbol, days, timeframe=None):
    """지정 기간의 캔들을 페이지네이션으로 모아온다."""
    timeframe = timeframe or config.TIMEFRAME
    since = int((pd.Timestamp.utcnow() - pd.Timedelta(days=days)).timestamp() * 1000)
    rows, seen = [], set()
    while True:
        batch = _exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
        if not batch:
            break
        fresh = [r for r in batch if r[0] not in seen]
        if not fresh:
            break
        rows.extend(fresh)
        seen.update(r[0] for r in fresh)
        since = fresh[-1][0] + 1
        if len(batch) < 1000:
            break
        time.sleep(_exchange.rateLimit / 1000)
    df = pd.DataFrame(rows, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
    df = df.drop_duplicates('ts').sort_values('ts')
    df.index = pd.to_datetime(df['ts'], unit='ms', utc=True)
    return df


def fetch_daily(symbol, end=None):
    """HISTORY_START 부터 end(포함)까지의 닫힌 일봉. data/ 에 캐시한다.

    end 를 주지 않으면 마지막으로 닫힌 캔들까지다. 같은 end 로 다시 실행하면
    같은 데이터가 나온다 — 문서 숫자를 재현하려면 end 를 고정해야 한다.
    """
    import pathlib
    cache_dir = pathlib.Path(__file__).resolve().parent / 'data'
    cache = cache_dir / f"{symbol.replace('/', '-')}-1d.csv"
    df = None
    if cache.is_file():
        df = pd.read_csv(cache)
    since = int(pd.Timestamp(HISTORY_START, tz='UTC').timestamp() * 1000)
    if df is not None and len(df):
        since = int(df['ts'].iloc[-1]) + 1
    rows = []
    while True:
        batch = _exchange.fetch_ohlcv(symbol, timeframe='1d', since=since, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        since = batch[-1][0] + 1
        if len(batch) < 1000:
            break
        time.sleep(_exchange.rateLimit / 1000)
    if rows:
        new = pd.DataFrame(rows, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df = new if df is None else pd.concat([df, new], ignore_index=True)
    df = df.drop_duplicates('ts').sort_values('ts').reset_index(drop=True)
    # 진행 중인 마지막 캔들은 버린다 (닫힌 뒤에 값이 바뀐다)
    day_ms = 24 * 3600 * 1000
    now_ms = int(pd.Timestamp.now(tz='UTC').timestamp() * 1000)
    df = df[df['ts'] + day_ms <= now_ms]
    cache_dir.mkdir(exist_ok=True)
    df.to_csv(cache, index=False)
    df.index = pd.to_datetime(df['ts'], unit='ms', utc=True)
    if end is not None:
        df = df.loc[:_ts(end)]
    return df


def simulate(df, ind, deploy='bot'):
    """신호대로 매매를 재현한다.

    deploy='bot'   현금의 50%만 투입 (봇의 실제 동작)
    deploy='full'  현금 전액 투입 (비교용)
    """
    cash, coin = INITIAL_CAPITAL, 0.0
    trades, equity = [], []
    entry_price = entry_cost = None

    for ts, price, sig in zip(df.index, ind.close, ind.signal):
        price = float(price)

        if sig == 'buy' and cash > 0:
            if deploy == 'full':
                budget = cash
            else:
                # 봇의 실제 규칙: 이미 의미 있는 수량을 들고 있으면 전액, 아니면 절반.
                budget = cash if (coin * price) >= config.HOLD_THRESHOLD_KRW else cash * 0.5
            spend = budget / (1 + config.FEE_RATE) * config.USABLE_RATIO
            if spend > 0:
                bought = spend / price
                fee = spend * config.FEE_RATE
                cash -= spend + fee
                coin += bought
                entry_price, entry_cost = price, spend + fee

        elif sig == 'sell' and coin > 0:
            volume = coin * config.USABLE_RATIO
            proceeds = volume * price
            fee = proceeds * config.FEE_RATE
            cash += proceeds - fee
            coin -= volume
            if entry_price is not None:
                pnl_pct = (price - entry_price) / entry_price * 100
                trades.append({
                    'exit': ts, 'entry_price': entry_price, 'exit_price': price,
                    'pnl_pct': pnl_pct,
                    'net': (proceeds - fee) - entry_cost if entry_cost else None,
                })
                entry_price = entry_cost = None

        equity.append(cash + coin * price)

    curve = pd.Series(equity, index=df.index)
    final = curve.iloc[-1]
    peak = curve.cummax()
    drawdown = (curve - peak) / peak * 100

    wins = [t for t in trades if t['pnl_pct'] > 0]

    return {
        'final': final,
        'return_pct': (final / INITIAL_CAPITAL - 1) * 100,
        'curve': curve,
        'max_dd': drawdown.min(),
        'trades': trades,
        'n_trades': len(trades),
        'win_rate': (len(wins) / len(trades) * 100) if trades else 0.0,
        'avg_win': (sum(t['pnl_pct'] for t in wins) / len(wins)) if wins else 0.0,
        'avg_loss': (sum(t['pnl_pct'] for t in trades if t['pnl_pct'] <= 0)
                     / max(1, len(trades) - len(wins))) if trades else 0.0,
    }


def buy_and_hold(df):
    first, last = float(df['close'].iloc[0]), float(df['close'].iloc[-1])
    units = INITIAL_CAPITAL / (1 + config.FEE_RATE) / first
    final = units * last * (1 - config.FEE_RATE)
    curve = units * df['close'].astype(float)
    peak = curve.cummax()
    return {
        'final': final,
        'return_pct': (final / INITIAL_CAPITAL - 1) * 100,
        'max_dd': ((curve - peak) / peak * 100).min(),
    }


def simulate_weights(close, weight, fee=None, band=None, trade_price=None):
    """목표 비중 시리즈대로 조정한 자산 곡선(시작값 1). (곡선, 조정 횟수, 낸 수수료).

    목표 비중이 직전에 맞춘 비중과 band 이상 벌어질 때만 그 차이만큼 조정한다 —
    봇의 리포트 규칙과 같다. trade_price 를 주면 그 가격으로 체결하고 평가는
    종가로 한다 (알림 직후 체결을 시가로 흉내 낼 때 쓴다).
    """
    fee = config.FEE_RATE if fee is None else fee
    band = config.REBAL_BAND if band is None else band
    close = close.astype(float)
    trade = close if trade_price is None else trade_price.astype(float)
    weight = weight.reindex(close.index).fillna(0.0).clip(0.0, 1.0)

    cash, coin, held_w = 1.0, 0.0, 0.0
    equity, n_rebal, fees_paid = [], 0, 0.0
    for price, px, target in zip(close, trade, weight):
        price, px, target = float(price), float(px), float(target)
        if abs(target - held_w) >= band:
            value = cash + coin * px
            delta = value * target - coin * px
            if abs(delta) >= 1e-12:
                fees_paid += abs(delta) * fee
                if delta > 0:
                    spend = min(delta, cash / (1 + fee))
                    coin += spend / px
                    cash -= spend * (1 + fee)
                else:
                    coin += delta / px             # delta < 0 → 매도
                    cash += -delta * (1 - fee)
                n_rebal += 1
            held_w = target
        equity.append(cash + coin * price)
    return pd.Series(equity, index=close.index), n_rebal, fees_paid


def simulate_daily(df, fee=None, start=None):
    """일봉 3신호 비중 전략. 채택 전략의 재현 엔트리.

    start 를 주면 df 전체로 지표를 계산하고 start 부터만 매매·평가한다.
    df 가 start 보다 required_bars() 이상 앞서 있어야 워밍업이 된다.
    start 가 없으면 df 첫 봉부터 평가한다 — 이때 처음 200봉은 신호가 온전하지
    않다는 점을 감안해야 한다.
    """
    ds = strategy.analyze_daily(df)
    close, weight = df['close'].astype(float), ds.weight
    if start is not None:
        start = _ts(start)
        close, weight = close.loc[start:], weight.loc[start:]
    curve, n_rebal, fees_paid = simulate_weights(close, weight, fee=fee)
    curve = curve * INITIAL_CAPITAL
    peak = curve.cummax()
    final = float(curve.iloc[-1])
    return {
        'final': final,
        'return_pct': (final / INITIAL_CAPITAL - 1) * 100,
        'max_dd': float(((curve - peak) / peak * 100).min()),
        'n_rebal': n_rebal,
        'exposure': float(weight.fillna(0.0).clip(0.0, 1.0).mean()),
        'fees_paid': fees_paid * INITIAL_CAPITAL,
        'curve': curve,
    }


def fee_drag(df, start=None):
    """수수료가 실제로 갉아먹은 몫. 같은 전략을 0% 로 돌려 차이를 본다.

    누적 수수료액을 초기자본으로 나누면 안 된다. 자산이 40배 불어난 구간에서
    낸 수수료를 초기자본 기준으로 환산하면 400% 같은 값이 나온다.
    """
    with_fee = simulate_daily(df, start=start)
    without = simulate_daily(df, fee=0.0, start=start)
    return without['return_pct'] - with_fee['return_pct']


def window(end=None, start=None, days=None):
    """평가 구간 (start, end). start 가 없으면 end 에서 days 만큼 거슬러 간다."""
    end = _ts(end) if end else None
    if start:
        return _ts(start), end
    return None, end   # start 는 데이터를 받은 뒤 days 로 정한다


def _resolve(df, start, end, days):
    end = df.index[-1] if end is None else min(end, df.index[-1])
    if start is None:
        start = end - pd.Timedelta(days=(days or 2190) - 1)
    first_ok = df.index[min(strategy.required_bars(), len(df) - 1)]
    return start, end, start < first_ok


def run_daily(asset, symbol, days=None, start=None, end=None):
    """채택 전략(일봉 3신호) 백테스트. 문서 숫자를 재현하는 엔트리."""
    start, end = window(end, start, days)
    df = fetch_daily(symbol, end)
    if df.empty or len(df) < strategy.required_bars():
        print(f'{asset}: 데이터 부족 ({len(df)}/{strategy.required_bars()}봉)')
        return None
    start, end, cold = _resolve(df, start, end, days)
    r = simulate_daily(df, start=start)
    bh = buy_and_hold(df.loc[start:])
    print(f'\n{"="*66}')
    print(f'{asset}  일봉 3신호  {start:%Y-%m-%d} ~ {end:%Y-%m-%d}  ({len(df.loc[start:])}봉)')
    print(f'수수료 편도 {config.FEE_RATE:.2%}  ·  리밸런싱 밴드 {config.REBAL_BAND:.0%}')
    if cold:
        print('※ 구간 앞쪽에 지표를 채울 데이터가 모자라다. 처음 200봉의 신호는 온전하지 않다.')
    print('='*66)
    print(f'{"":<22}{"수익률":>12}{"MDD":>10}{"거래":>7}{"노출":>8}')
    print('-'*66)
    print(f'{"일봉 3신호":<22}{r["return_pct"]:>11.1f}%{r["max_dd"]:>9.1f}%'
          f'{r["n_rebal"]:>7}{r["exposure"]*100:>7.0f}%')
    print(f'{"단순 보유":<22}{bh["return_pct"]:>11.1f}%{bh["max_dd"]:>9.1f}%{"-":>7}{"100":>7}%')
    print('-'*66)
    print(f'수수료 부담 {fee_drag(df, start):.1f}%p (0% 대비)  ·  '
          f'MDD 개선 {bh["max_dd"] - r["max_dd"]:+.1f}%p')
    return {'asset': asset, 'daily': r, 'bh': bh, 'df': df}


def run_portfolio(days=None, weights=None, fee=None, start=None, end=None):
    """BTC/ETH 배분 포트폴리오. docs 의 표를 재현하는 엔트리.

    자산별로 따로 운용한 곡선을 처음 배분 비율로 더한다 (자산 사이의 재배분은 없다).
    """
    weights = weights or {a: v['alloc'] for a, v in config.ASSETS.items() if v['alloc']}
    fee = config.FEE_RATE if fee is None else fee
    start, end = window(end, start, days)
    frames = {a: fetch_daily(config.ASSETS[a]['binance'], end) for a in weights}
    for asset, df in frames.items():
        if df.empty or len(df) < strategy.required_bars():
            print(f'{asset}: 데이터 부족'); return None
    first = frames[next(iter(frames))]
    start, end, cold = _resolve(first, start, end, days)

    curves, holds = {}, {}
    for asset, df in frames.items():
        curves[asset] = simulate_daily(df, fee=fee, start=start)['curve'] / INITIAL_CAPITAL
        close = df['close'].astype(float).loc[start:]
        holds[asset] = close / float(close.iloc[0])

    idx = None
    for c in curves.values():
        idx = c.index if idx is None else idx.intersection(c.index)
    strat = sum(curves[a].reindex(idx) * w for a, w in weights.items())
    hold = sum(holds[a].reindex(idx) * w for a, w in weights.items())

    def stat(e):
        pk = e.cummax()
        return (e.iloc[-1] - 1) * 100, float(((e - pk) / pk * 100).min())

    sr, sd = stat(strat)
    hr, hd = stat(hold)
    alloc_txt = ' / '.join(f'{a} {w:.0%}' for a, w in weights.items())
    print(f'\n{"="*66}')
    print(f'포트폴리오  {alloc_txt}  ·  {idx[0]:%Y-%m-%d} ~ {idx[-1]:%Y-%m-%d}')
    print(f'수수료 편도 {fee:.2%}  ·  리밸런싱 밴드 {config.REBAL_BAND:.0%}')
    if cold:
        print('※ 구간 앞쪽에 지표를 채울 데이터가 모자라다. 처음 200봉의 신호는 온전하지 않다.')
    print('='*66)
    print(f'{"":<22}{"수익률":>12}{"MDD":>10}')
    print('-'*66)
    print(f'{"일봉 3신호":<22}{sr:>11.1f}%{sd:>9.1f}%')
    print(f'{"단순 보유":<22}{hr:>11.1f}%{hd:>9.1f}%')
    print('-'*66)
    print(f'MDD 개선 {hd - sd:+.1f}%p')
    return {'strat': strat, 'hold': hold}


def run(asset, symbol, days):
    df = fetch_history(symbol, days)
    if df.empty or len(df) < config.EMA_SLOW + 2:
        print(f'{asset}: 데이터 부족')
        return None
    ind = strategy.analyze(df)
    bot = simulate(df, ind, 'bot')
    full = simulate(df, ind, 'full')
    bh = buy_and_hold(df)

    start, end = df.index[0], df.index[-1]
    print(f'\n{"="*66}')
    print(f'{asset}  {config.TIMEFRAME}  '
          f'{start:%Y-%m-%d} ~ {end:%Y-%m-%d}  ({len(df)}봉)')
    print(f'가격 {float(df["close"].iloc[0]):,.0f} → {float(df["close"].iloc[-1]):,.0f} USD '
          f'({(float(df["close"].iloc[-1])/float(df["close"].iloc[0])-1)*100:+.1f}%)')
    print('='*66)
    print(f'{"":<22}{"수익률":>12}{"MDD":>10}{"거래":>7}{"승률":>8}')
    print('-'*66)
    print(f'{"EMA 크로스 (봇 = 50%)":<22}{bot["return_pct"]:>11.1f}%{bot["max_dd"]:>9.1f}%'
          f'{bot["n_trades"]:>7}{bot["win_rate"]:>7.0f}%')
    print(f'{"EMA 크로스 (전액)":<22}{full["return_pct"]:>11.1f}%{full["max_dd"]:>9.1f}%'
          f'{full["n_trades"]:>7}{full["win_rate"]:>7.0f}%')
    print(f'{"단순 보유":<22}{bh["return_pct"]:>11.1f}%{bh["max_dd"]:>9.1f}%{"-":>7}{"-":>8}')
    print('-'*66)

    t = bot['trades']
    if t:
        fees = bot['n_trades'] * 2 * config.FEE_RATE * 100
        print(f'평균 수익 거래 {bot["avg_win"]:+.2f}%  |  평균 손실 거래 {bot["avg_loss"]:+.2f}%')
        print(f'왕복 수수료 누적 약 {fees:.1f}%p (거래 {bot["n_trades"]}회 × 0.5%)')
        worst = min(t, key=lambda x: x['pnl_pct'])
        best = max(t, key=lambda x: x['pnl_pct'])
        print(f'최고 {best["pnl_pct"]:+.2f}%  |  최악 {worst["pnl_pct"]:+.2f}%')
    return {'asset': asset, 'bot': bot, 'full': full, 'bh': bh, 'df': df}


def main():
    ap = argparse.ArgumentParser(
        description='기본은 채택 전략(일봉 3신호). --legacy 로 폐기된 EMA 크로스도 볼 수 있다.')
    ap.add_argument('--days', type=int, default=2190,
                    help='--start 가 없을 때 --end 에서 거슬러 갈 일수')
    ap.add_argument('--start', help='평가 시작일 YYYY-MM-DD (UTC)')
    ap.add_argument('--end', help='평가 종료일 YYYY-MM-DD (UTC). 문서와 대조하려면 고정한다')
    ap.add_argument('--legacy', action='store_true',
                    help='폐기된 4시간봉 EMA 크로스 백테스트')
    ap.add_argument('--portfolio', action='store_true',
                    help='BTC/ETH 배분 포트폴리오 (docs 표 재현)')
    args = ap.parse_args()
    if args.portfolio:
        run_portfolio(args.days, start=args.start, end=args.end)
        return 0
    for asset, syms in config.ASSETS.items():
        if args.legacy:
            run(asset, syms['binance'], args.days)
        else:
            run_daily(asset, syms['binance'], args.days, start=args.start, end=args.end)
    return 0


if __name__ == '__main__':
    sys.exit(main())
