#!/usr/bin/env python3
"""EMA 크로스 전략 백테스트.

봇이 실제로 쓰는 strategy 모듈을 그대로 불러 쓴다. 신호 로직을 여기서 다시
구현하면 실제 매매와 어긋난 결과를 보게 되므로 의도적으로 재사용한다.

주의(결과를 읽을 때 감안할 것):
  - 체결가를 캔들 종가로 가정한다. 실제 봇은 마감 직후 시장가로 내므로
    슬리피지만큼 낙관적이다.
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
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=180)
    args = ap.parse_args()
    for asset, syms in config.ASSETS.items():
        run(asset, syms['binance'], args.days)
    return 0


if __name__ == '__main__':
    sys.exit(main())
