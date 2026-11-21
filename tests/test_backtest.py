"""백테스트와 재검토 스크립트의 계산 규칙. 네트워크 없이 합성 데이터로만 돈다.

2026-11 재검토에서 나온 문제를 고정한다 — 평가 구간의 데이터만으로 지표를
계산하면 처음 200봉은 신호가 꺼진 것으로 처리되고, 그 구간의 등락이 성과에 섞인다.
"""
import unittest

import numpy as np
import pandas as pd

import backtest
import evaluate


def frame(closes):
    c = pd.Series([float(x) for x in closes])
    idx = pd.date_range('2022-01-01', periods=len(c), freq='D', tz='UTC')
    c.index = idx
    return pd.DataFrame(
        {'ts': idx.astype('int64') // 10**6, 'open': c, 'high': c * 1.01,
         'low': c * 0.99, 'close': c, 'vol': 1.0}, index=idx)


class SimulateWeights(unittest.TestCase):
    def test_full_weight_without_fee_tracks_price(self):
        close = frame(np.linspace(100, 200, 50))['close']
        curve, n, fees = backtest.simulate_weights(close, pd.Series(1.0, index=close.index), fee=0.0)
        self.assertAlmostEqual(curve.iloc[-1], 2.0, places=9)
        self.assertEqual((n, fees), (1, 0.0))

    def test_zero_weight_stays_in_cash(self):
        close = frame(np.linspace(100, 50, 30))['close']
        curve, n, _ = backtest.simulate_weights(close, pd.Series(0.0, index=close.index))
        self.assertEqual((curve.iloc[-1], n), (1.0, 0))

    def test_changes_inside_band_do_not_trade(self):
        close = frame([100] * 10)['close']
        weight = pd.Series([0.6, 0.65, 0.55, 0.62, 0.6, 0.69, 0.6, 0.6, 0.6, 0.6], index=close.index)
        _, n, _ = backtest.simulate_weights(close, weight, fee=0.0, band=0.10)
        self.assertEqual(n, 1)          # 처음 0 → 0.6 한 번뿐

    def test_fee_reduces_the_result(self):
        close = frame(np.linspace(100, 200, 50))['close']
        weight = pd.Series(1.0, index=close.index)
        free, _, _ = backtest.simulate_weights(close, weight, fee=0.0)
        paid, _, fees = backtest.simulate_weights(close, weight, fee=0.0025)
        self.assertLess(paid.iloc[-1], free.iloc[-1])
        self.assertGreater(fees, 0)

    def test_trade_price_is_used_for_fills_and_close_for_marking(self):
        df = frame([100, 110, 121])
        df['open'] = [100, 100, 110]     # 시가는 전일 종가
        w = pd.Series([0.0, 1.0, 1.0], index=df.index)
        at_close, _, _ = backtest.simulate_weights(df['close'], w, fee=0.0)
        at_open, _, _ = backtest.simulate_weights(df['close'], w, fee=0.0, trade_price=df['open'])
        self.assertAlmostEqual(at_close.iloc[-1], 1.1, places=9)    # 둘째 날 종가 110 에 사서 121
        self.assertAlmostEqual(at_open.iloc[-1], 1.21, places=9)    # 둘째 날 시가 100 에 사서 121


class WarmUp(unittest.TestCase):
    def test_indicators_are_warmed_by_data_before_the_window(self):
        # 꾸준히 오르는 시세에서는 세 신호가 모두 켜져 있어야 한다
        df = frame(100 * 1.003 ** np.arange(700))
        start = df.index[400]
        warm = backtest.simulate_daily(df, fee=0.0, start=start)
        cold = backtest.simulate_daily(df.loc[start:], fee=0.0)
        hold = float(df['close'].iloc[-1] / df['close'].loc[start])
        self.assertAlmostEqual(warm['exposure'], 1.0, places=6)
        self.assertAlmostEqual(warm['final'] / backtest.INITIAL_CAPITAL, hold, places=6)
        # 구간 안의 데이터만 쓰면 지표가 찰 때까지 비중이 낮아 같은 시세에서도 뒤처진다
        self.assertLess(cold['exposure'], 0.95)
        self.assertLess(cold['final'], warm['final'])


class Review(unittest.TestCase):
    def test_const_mix_edges(self):
        close = frame(np.linspace(100, 300, 90))['close']
        self.assertEqual(evaluate.const_mix(close, 0.0, 0.0025).iloc[-1], 1.0)
        self.assertAlmostEqual(evaluate.const_mix(close, 1.0, 0.0).iloc[-1], 3.0, places=9)

    def test_const_mix_holds_the_weight_each_month(self):
        close = frame(100 * 1.01 ** np.arange(120))['close']
        half = evaluate.const_mix(close, 0.5, 0.0).iloc[-1]
        self.assertTrue(1.0 < half < float(close.iloc[-1] / close.iloc[0]))

    def test_hac_t_recovers_intercept_and_slope(self):
        rng = np.random.RandomState(3)
        x = rng.normal(0, 1, 2000)
        y = 0.5 + 2.0 * x + rng.normal(0, 0.1, 2000)
        beta, t = evaluate.hac_t(y, x)
        self.assertAlmostEqual(beta[0], 0.5, places=1)
        self.assertAlmostEqual(beta[1], 2.0, places=1)
        self.assertGreater(t, 50)

    def test_hac_t_of_pure_noise_is_small(self):
        rng = np.random.RandomState(4)
        _, t = evaluate.hac_t(rng.normal(0, 1, 3000))
        self.assertLess(abs(t), 3)

    def test_stat_reports_drawdown(self):
        curve = pd.Series([1.0, 2.0, 1.0, 1.5], index=pd.date_range('2024-01-01', periods=4, freq='D', tz='UTC'))
        total, _, mdd, _ = evaluate.stat(curve)
        self.assertAlmostEqual(total, 50.0)
        self.assertAlmostEqual(mdd, -50.0)


if __name__ == '__main__':
    unittest.main()
