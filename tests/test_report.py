"""리밸런싱 밴드와 리포트 문구.

밴드는 설정만 있고 구현이 없던 기간이 있었다(코드 리뷰 P0-2).
'조치 불필요' 와 '리밸런싱' 판정이 뒤집히면 사람이 잘못 집행한다.
"""
import unittest

import config
import report

BTC_KRW, ETH_KRW = 90_000_000.0, 2_660_000.0


def accounts(krw=0.0, btc=0.0, eth=0.0):
    return [
        {'currency': 'KRW', 'balance': str(krw), 'locked': '0'},
        {'currency': 'BTC', 'balance': str(btc), 'locked': '0'},
        {'currency': 'ETH', 'balance': str(eth), 'locked': '0'},
    ]


def state(weight, insufficient=False, prev=None):
    return {
        'fast': True, 'st': True, 'slow': False,
        'score': weight, 'weight': weight,
        'prev_weight': weight if prev is None else prev,
        'rsi': 54.0, 'close': BTC_KRW,
        'bars': 100 if insufficient else 480,
        'bars_needed': 201, 'insufficient': insufficient,
    }


def rows(w_btc=0.80, w_eth=0.80, **kw):
    return [('BTC', 0.70, state(w_btc, **kw), BTC_KRW),
            ('ETH', 0.30, state(w_eth, **kw), ETH_KRW)]


class RebalanceBand(unittest.TestCase):

    def test_on_target_says_no_action(self):
        # 총 1,000만 · 목표 BTC 56% / ETH 24% 에 맞춘 잔고
        acc = accounts(krw=2_000_000, btc=5_600_000/BTC_KRW, eth=2_400_000/ETH_KRW)
        out = report.build_daily(rows(), acc)
        self.assertIn('조치 불필요', out)
        self.assertNotIn('리밸런싱', out)

    def test_large_gap_triggers_rebalance(self):
        acc = accounts(krw=8_200_000, btc=1_800_000/BTC_KRW, eth=0)
        out = report.build_daily(rows(), acc)
        self.assertIn('리밸런싱', out)
        self.assertIn('BTC 매수', out)

    def test_all_cash_asks_to_buy_both(self):
        out = report.build_daily(rows(), accounts(krw=10_000_000))
        self.assertIn('BTC 매수', out)
        self.assertIn('ETH 매수', out)

    def test_gap_just_inside_band_is_ignored(self):
        # 목표 56%, 보유 약 48% → 격차 8%p < 밴드 10%
        total = 10_000_000
        btc_val = total * 0.48
        acc = accounts(krw=total - btc_val - 2_400_000,
                       btc=btc_val/BTC_KRW, eth=2_400_000/ETH_KRW)
        out = report.build_daily(rows(), acc)
        self.assertIn('밴드 내', out)
        self.assertNotIn('BTC 매수', out)

    def test_zero_weight_asks_to_sell(self):
        acc = accounts(krw=1_000_000, btc=9_000_000/BTC_KRW)
        out = report.build_daily(rows(w_btc=0.0, w_eth=0.0), acc)
        self.assertIn('BTC 매도', out)

    def test_band_value_is_shown(self):
        out = report.build_daily(rows(), accounts(krw=10_000_000))
        self.assertIn(f'{config.REBAL_BAND:.0%}', out)


class WithoutAccounts(unittest.TestCase):
    """잔고 조회가 실패해도 리포트는 나가야 한다."""

    def test_renders_without_balances(self):
        out = report.build_daily(rows())
        self.assertIn('목표 총 노출', out)
        self.assertNotIn('현재 보유', out)

    def test_weight_change_is_reported_when_balances_missing(self):
        out = report.build_daily(rows(prev=0.55))
        self.assertIn('비중 변경', out)


class InsufficientData(unittest.TestCase):
    """워밍업이 모자라면 점수를 그대로 믿으면 안 된다."""

    def test_warns_on_short_history(self):
        out = report.build_daily(rows(insufficient=True), accounts(krw=10_000_000))
        self.assertIn('데이터 부족', out)
        self.assertIn('100/201', out)

    def test_no_warning_when_sufficient(self):
        out = report.build_daily(rows(), accounts(krw=10_000_000))
        self.assertNotIn('데이터 부족', out)


class Exposure(unittest.TestCase):

    def test_total_target_is_alloc_weighted(self):
        # BTC 0.70×0.80 + ETH 0.30×0.80 = 0.80
        self.assertIn('`80%`', report.build_daily(rows()))

    def test_observation_only_asset_excluded_from_total(self):
        r = rows() + [('SOL', 0.0, state(1.0), 100_000.0)]
        out = report.build_daily(r)
        self.assertIn('관찰용', out)
        self.assertIn('`80%`', out)     # SOL 이 총 노출을 올리지 않는다


if __name__ == '__main__':
    unittest.main()
