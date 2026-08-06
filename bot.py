#!/usr/bin/env python3
"""EMA 크로스 트레이딩 봇.

신호는 바이낸스(USDT) 캔들로 계산하고, 주문은 빗썸(KRW) Open API 2.0 으로 낸다.
닫힌 캔들만 사용하며, 한 캔들당 최대 한 번만 주문한다.

실행 모드는 .env 로 제어한다:
    ENABLE_LIVE_TRADING=false     주문을 만들지 않음
    ENABLE_LIVE_SIMULATION=true   라이브 경로를 그대로 밟되 실주문은 보내지 않음
"""

import fcntl
import logging
import os
import sys
import time
from datetime import datetime, timezone

import bithumb
import charting
import config
import market
import notify
import report
import state
import strategy

_lock_handle = None


def acquire_lock():
    """중복 실행 방지. 이미 실행 중이면 False."""
    global _lock_handle
    try:
        _lock_handle = open(config.LOCK_FILE, 'w')
        fcntl.flock(_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except Exception:
        return False


def _order_for_buy(asset, accounts, last_krw):
    """매수 수량 결정. (spend_krw, 사유) — spend 가 None 이면 주문하지 않는다."""
    krw = bithumb.available(accounts, 'KRW')
    coin = bithumb.available(accounts, asset)
    coin_krw = coin * last_krw if last_krw else 0.0

    # 이미 의미 있는 수량을 들고 있으면 남은 현금을 모두 태우고,
    # 그렇지 않으면 절반만 써서 분할 진입한다.
    budget = krw if coin_krw >= config.HOLD_THRESHOLD_KRW else krw * 0.5
    spend = int(budget / (1 + config.FEE_RATE) * config.USABLE_RATIO)
    if spend < config.MIN_ORDER_KRW:
        return None, f'매수 예정액 {spend:,} KRW < 최소 {config.MIN_ORDER_KRW:,} KRW'
    return spend, f'{spend:,} KRW'


def _order_for_sell(asset, accounts, last_krw):
    """매도 수량 결정. (volume, 사유) — volume 이 None 이면 주문하지 않는다."""
    coin = bithumb.available(accounts, asset)
    volume = coin * config.USABLE_RATIO
    proceeds = volume * last_krw if last_krw else 0.0
    if volume <= 0:
        return None, '보유 수량 없음'
    if proceeds < config.MIN_ORDER_KRW:
        return None, f'매도 예정액 {proceeds:,.0f} KRW < 최소 {config.MIN_ORDER_KRW:,} KRW'
    return volume, f'{volume:.8f} ≈ {proceeds:,.0f} KRW'


def execute_signal(asset, symbol, signal, accounts, last_krw, candle_ts):
    """신호에 따라 주문을 낸다.

    (settled, message) 를 반환한다. settled 가 False 면 일시적 실패라
    이 캔들을 처리 완료로 기록하면 안 된다 — 기록해 버리면 잔고 조회 실패나
    네트워크 오류 한 번에 그 신호를 영영 건너뛴다.
    """
    if accounts is None:
        logging.warning('%s: 잔고 조회 실패로 %s 주문 생략', asset, signal.upper())
        return False, f'⚠️ *{asset} {signal.upper()} 생략* — 잔고 조회 실패'

    if signal == 'buy':
        spend, detail = _order_for_buy(asset, accounts, last_krw)
        if spend is None:
            logging.info('%s: %s', asset, detail)
            return True, None          # 낼 주문이 없는 것은 정상 종료
        result = bithumb.place_market_order(symbol, 'bid', krw=spend)
    else:
        volume, detail = _order_for_sell(asset, accounts, last_krw)
        if volume is None:
            logging.info('%s: %s', asset, detail)
            return True, None
        result = bithumb.place_market_order(symbol, 'ask', volume=volume)

    if result is None:
        if not config.LIVE_TRADING:
            return True, None          # 애초에 주문을 낼 생각이 없었다
        return False, report.build_order_result(asset, signal, None, detail)

    status = result.get('status_code')
    ok = bool(result.get('mock')) or (isinstance(status, int) and status < 400)
    return ok, report.build_order_result(asset, signal, result, detail)


def process_asset(asset, symbols, handled, seen):
    """자산 하나에 대해 한 캔들을 처리한다."""
    df = market.fetch_ohlcv(symbols['binance'])
    if df is None:
        return
    df = market.drop_unclosed(df)
    if df is None or len(df) < 2:
        logging.info('%s: 닫힌 캔들이 부족해 건너뜁니다', asset)
        return

    candle_ts = int(df.index[-1].timestamp())
    if seen.get(asset) == candle_ts:
        return  # 같은 캔들을 폴링마다 다시 처리하지 않는다

    ind = strategy.analyze(df)
    signal = strategy.latest_signal(ind)

    ticker = bithumb.fetch_ticker(symbols['bithumb'])
    last_krw = ticker['last'] if ticker else None
    accounts = bithumb.fetch_accounts()

    logging.info('%s 캔들 %s 처리: signal=%s KRW=%s',
                 asset, candle_ts, signal or 'none', last_krw)

    # ── 차트 ────────────────────────────────────────────────────────────
    signals = strategy.signal_history(df, ind)
    if config.SEND_ON_CANDLE_CLOSE or signal:
        if not state.was_sent(asset, candle_ts, 'photo'):
            path = os.path.join(config.CHART_DIR, f'{asset}_{config.TIMEFRAME}.png')
            if charting.render(asset, df, ind, signals, path):
                caption = report.build_caption(asset, ind, signal, last_krw)
                if notify.send_photo(path, caption):
                    state.mark_sent(asset, candle_ts, 'photo')

    # ── 리포트 ──────────────────────────────────────────────────────────
    if not state.was_sent(asset, candle_ts, 'text'):
        text = report.build_status(
            asset, df, ind, signal, last_krw, accounts,
            prev_signal=strategy.last_signal_before(ind),
        )
        if notify.send_text(text):
            state.mark_sent(asset, candle_ts, 'text')

    # ── 주문 ────────────────────────────────────────────────────────────
    if signal and not config.LEGACY_EMA_EXECUTION:
        # 폐기된 전략이다. 알림만 보내고 주문은 내지 않는다.
        logging.info('%s: %s 신호 — 레거시 EMA 실행이 꺼져 있어 주문 생략',
                     asset, signal.upper())
    elif signal:
        # 재시작 방어: 캔들 진행 중 프로세스가 죽으면 launchd 가 되살리는데,
        # 메모리 기록만으로는 같은 신호에 주문이 다시 나간다.
        if handled.get(asset) == candle_ts:
            logging.info('%s: 캔들 %s 의 %s 신호는 이미 처리됨 — 주문 생략',
                         asset, candle_ts, signal.upper())
        else:
            settled, message = execute_signal(asset, symbols['bithumb'], signal,
                                              accounts, last_krw, candle_ts)
            if settled:
                state.mark_handled(handled, asset, candle_ts)
            else:
                logging.warning('%s: %s 주문 미확정 — 다음 주기에 재시도',
                                asset, signal.upper())
            if message:
                notify.send_text(message)

    seen[asset] = candle_ts


def daily_report(sent_dates):
    """일봉 마감(UTC 00:00 = KST 09:00) 직후 3신호 전략 리포트를 보낸다.

    4시간봉 알림과 별개로 동작한다. 4시간봉은 시세 확인용이고,
    실제 판단은 이 일봉 리포트를 따른다.
    """
    now = datetime.now(timezone.utc)
    if now.hour == 0 and now.minute < config.DAILY_REPORT_DELAY_MIN:
        return                                  # 거래소 데이터가 정착할 시간을 준다
    today = now.date().isoformat()
    if sent_dates.get('daily') == today:
        return

    rows, charts = [], []
    for asset, meta in config.ASSETS.items():
        df = market.fetch_ohlcv(meta['binance'], timeframe='1d',
                                limit=config.DAILY_HISTORY_DAYS)
        if df is None:
            logging.warning('%s 일봉 조회 실패 — 리포트 보류', asset)
            return
        df = market.drop_unclosed(df, hours=24)
        if df is None or len(df) < config.DAILY_SLOW + 5:
            logging.warning('%s 일봉이 부족합니다 (%s개)', asset, 0 if df is None else len(df))
            return
        ds = strategy.analyze_daily(df)
        st = strategy.daily_state(ds)
        ticker = bithumb.fetch_ticker(meta['bithumb'])
        rows.append((asset, meta['alloc'], st, ticker['last'] if ticker else None))

        path = os.path.join(config.CHART_DIR, f'{asset}_daily.png')
        if charting.render_daily(asset, df, ds, path, alloc=meta['alloc']):
            charts.append((asset, path, st, meta['alloc']))

    accounts = bithumb.fetch_accounts()
    if not notify.send_text(report.build_daily(rows, accounts)):
        logging.warning('일봉 리포트 전송 실패 — 다음 폴링에서 재시도')
        return
    for asset, path, st, alloc in charts:
        tag = '관찰용' if not alloc else f'전체 {alloc*st["weight"]*100:.0f}%'
        notify.send_photo(path, f'{asset} 일봉 · 비중 {st["weight"]*100:.0f}% · {tag}')

    sent_dates['daily'] = today
    logging.info('일봉 리포트 전송 완료 (%s)', today)


def main():
    if not acquire_lock():
        print('이미 실행 중입니다. 종료합니다.', file=sys.stderr)
        return 0

    config.ensure_dirs()
    logging.info('트레이딩 봇 시작 — 모드: %s, 폴링 %ss, 대상 %s',
                 config.mode_label(), config.POLL_INTERVAL_SEC, ', '.join(config.ASSETS))

    handled = state.load_handled_candles()
    seen, sent_dates = {}, state.load_daily_sent()

    while True:
        try:
            for asset, symbols in config.ASSETS.items():
                if not symbols.get('alloc'):
                    continue          # 배분 0 인 자산은 4시간봉 매매 대상이 아니다
                try:
                    process_asset(asset, symbols, handled, seen)
                except Exception:
                    logging.exception('%s 처리 중 오류', asset)
            try:
                daily_report(sent_dates)
            except Exception:
                logging.exception('일봉 리포트 오류')
            time.sleep(config.POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            logging.info('사용자 중단. 종료합니다.')
            return 0
        except Exception:
            logging.exception('메인 루프 오류')
            time.sleep(5)


if __name__ == '__main__':
    sys.exit(main())
