"""전략 계산의 경계 조건. 네트워크 없이 합성 데이터로만 돈다.

여기 있는 테스트는 전부 실제로 터졌던 버그에서 나왔다.
새 버그를 잡으려는 게 아니라, 고친 것이 다시 무너지지 않게 하려는 것이다.
"""
import unittest

import numpy as np
import pandas as pd

import config
import strategy


def frame(closes, tz='UTC'):
    """OHLCV 프레임. 고가·저가는 종가에서 ±1% 로 만든다."""
    c = pd.Series([float(x) for x in closes])
    idx = pd.date_range('2024-01-01', periods=len(c), freq='D', tz=tz)
    c.index = idx
    return pd.DataFrame(
        {'open': c, 'high': c * 1.01, 'low': c * 0.99, 'close': c, 'vol': 1.0},
        index=idx,
    )


def walk(n, seed=0, drift=0.001, vol=0.02):
    rng = np.random.RandomState(seed)
    return 100 * np.cumprod(1 + rng.normal(drift, vol, n))


class RsiBoundaries(unittest.TestCase):
    """손실이 0 인 구간에서 pd.NA 를 반환해 daily_state 가 죽었다."""

    def test_no_loss_is_100_not_na(self):
        r = strategy._rsi(pd.Series(np.linspace(100, 200, 60)), 14)
        self.assertFalse(pd.isna(r.iloc[-1]))
        self.assertAlmostEqual(r.iloc[-1], 100.0, places=6)

    def test_no_gain_is_zero(self):
        r = strategy._rsi(pd.Series(np.linspace(200, 100, 60)), 14)
        self.assertAlmostEqual(r.iloc[-1], 0.0, places=6)

    def test_flat_is_neutral(self):
        r = strategy._rsi(pd.Series(np.full(60, 100.0)), 14)
        self.assertAlmostEqual(r.iloc[-1], 50.0, places=6)

    def test_dtype_is_float_not_object(self):
        # object dtype 이면 float() 변환에서 터진다
        for arr in (np.linspace(100, 200, 60), walk(60)):
            self.assertEqual(strategy._rsi(pd.Series(arr), 14).dtype, np.float64)

    def test_normal_series_stays_in_range(self):
        r = strategy._rsi(pd.Series(walk(300, seed=3)), 14).dropna()
        self.assertTrue(((r >= 0) & (r <= 100)).all())


class Warmup(unittest.TestCase):
    """봉이 모자라면 MA200 이 조용히 False 가 되어 점수가 낮게 나왔다."""

    def test_required_bars_covers_longest_ma(self):
        self.assertGreater(strategy.required_bars(), config.DAILY_SLOW)

    def test_flags_insufficient(self):
        st = strategy.daily_state(strategy.analyze_daily(frame(walk(100))))
        self.assertTrue(st['insufficient'])
        self.assertEqual(st['bars'], 100)

    def test_accepts_sufficient(self):
        n = strategy.required_bars() + 50
        st = strategy.daily_state(strategy.analyze_daily(frame(walk(n))))
        self.assertFalse(st['insufficient'])

    def test_state_never_returns_nan(self):
        # 길이를 바꿔가며 float 변환이 터지지 않는지 본다
        for n in (2, 30, 100, strategy.required_bars(), 400):
            st = strategy.daily_state(strategy.analyze_daily(frame(walk(n, seed=n))))
            for key in ('score', 'weight', 'rsi', 'close'):
                self.assertFalse(pd.isna(st[key]), f'{key} is NaN at n={n}')

    def test_uptrend_scores_full_only_after_warmup(self):
        rise = np.linspace(100, 400, strategy.required_bars() + 60)
        st = strategy.daily_state(strategy.analyze_daily(frame(rise)))
        self.assertFalse(st['insufficient'])
        self.assertAlmostEqual(st['score'], 1.0, places=6)


class ScoreAndWeight(unittest.TestCase):

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(config.W_FAST + config.W_ST + config.W_SLOW, 1.0, places=9)

    def test_score_below_floor_means_zero_weight(self):
        ds = strategy.analyze_daily(frame(walk(400, seed=7)))
        low = ds.score < config.SCORE_FLOOR - 1e-9
        self.assertTrue((ds.weight[low].fillna(0) == 0).all())

    def test_weight_equals_score_above_floor(self):
        ds = strategy.analyze_daily(frame(walk(400, seed=7)))
        high = ds.score >= config.SCORE_FLOOR
        self.assertTrue(np.allclose(ds.weight[high], ds.score[high]))

    def test_weight_never_exceeds_one(self):
        ds = strategy.analyze_daily(frame(walk(400, seed=11)))
        self.assertLessEqual(float(ds.weight.max()), 1.0)


class NoLookahead(unittest.TestCase):
    """당일 종가로 판단하면 미래를 참조하게 된다."""

    def test_signals_are_shifted(self):
        df = frame(walk(300, seed=5))
        ds = strategy.analyze_daily(df)
        raw_fast = (df['close'].rolling(config.DAILY_FAST).mean()
                    > df['close'].rolling(config.DAILY_MID).mean())
        # sig_fast[i] 는 raw_fast[i-1] 과 같아야 한다
        self.assertTrue(ds.sig_fast.iloc[1:].eq(raw_fast.shift(1).iloc[1:]).all())

    def test_appending_a_bar_does_not_change_past_weights(self):
        base = walk(320, seed=9)
        a = strategy.analyze_daily(frame(base[:-1])).weight
        b = strategy.analyze_daily(frame(base)).weight
        self.assertTrue(np.allclose(a.fillna(0), b.iloc[:-1].fillna(0)))


class Supertrend(unittest.TestCase):

    def test_returns_bool_series(self):
        up, line = strategy.supertrend(frame(walk(200, seed=2)))
        self.assertEqual(len(up), 200)
        self.assertTrue(set(up.unique()) <= {True, False})

    def test_follows_a_sustained_uptrend(self):
        up, _ = strategy.supertrend(frame(np.linspace(100, 300, 200)))
        self.assertTrue(bool(up.iloc[-1]))

    def test_follows_a_sustained_downtrend(self):
        up, _ = strategy.supertrend(frame(np.linspace(300, 100, 200)))
        self.assertFalse(bool(up.iloc[-1]))


if __name__ == '__main__':
    unittest.main()
