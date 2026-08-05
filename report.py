"""텔레그램 리포트 문구 작성."""

from datetime import datetime, timedelta, timezone

import pandas as pd

import bithumb
import config

_SIGNAL_BADGE = {'buy': '🟢 BUY', 'sell': '🔴 SELL', None: '⚪️ 관망'}


# ── 포맷 헬퍼 ───────────────────────────────────────────────────────────────
def _num(value, digits=0):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 'N/A'
    return f'{value:,.{digits}f}'


def _pct(value, digits=2, sign=True):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 'N/A'
    fmt = f'{value:+.{digits}f}%' if sign else f'{value:.{digits}f}%'
    return fmt


def _rsi_label(rsi):
    if rsi is None or pd.isna(rsi):
        return ''
    if rsi >= 70:
        return ' 과매수'
    if rsi <= 30:
        return ' 과매도'
    if rsi >= 55:
        return ' 강세'
    if rsi <= 45:
        return ' 약세'
    return ' 중립'


def _last(series):
    if series is None or len(series) == 0:
        return None
    value = series.iloc[-1]
    return None if pd.isna(value) else float(value)


def _until(target):
    delta = target - datetime.now(timezone.utc)
    total = int(delta.total_seconds())
    if total <= 0:
        return '마감 직후'
    hours, minutes = divmod(total // 60, 60)
    return f'{hours}시간 {minutes}분' if hours else f'{minutes}분'


# ── 본문 ────────────────────────────────────────────────────────────────────
def build_status(asset, df, ind, signal, last_krw, accounts, prev_signal=None):
    """캔들 마감 리포트를 만든다."""
    close_usd = _last(ind.close)
    prev_close = _last(ind.prev_close)
    change_pct = ((close_usd - prev_close) / prev_close * 100) if (close_usd and prev_close) else None

    ema_fast = _last(ind.ema_fast)
    ema_slow = _last(ind.ema_slow)
    diff = _last(ind.diff)
    diff_pct = (diff / ema_slow * 100) if (diff is not None and ema_slow) else None

    rsi = _last(ind.rsi)
    atr = _last(ind.atr)
    atr_pct = (atr / close_usd * 100) if (atr and close_usd) else None

    volume = _last(ind.volume)
    avg_volume = _last(ind.avg_volume)
    vol_pct = ((volume - avg_volume) / avg_volume * 100) if (volume is not None and avg_volume) else None

    candle_start = pd.Timestamp(df.index[-1])
    if candle_start.tzinfo is None:
        candle_start = candle_start.tz_localize('UTC')
    candle_close = candle_start + pd.Timedelta(hours=config.TIMEFRAME_HOURS)
    close_kst = candle_close.tz_convert(config.KST)
    next_close = (candle_close + pd.Timedelta(hours=config.TIMEFRAME_HOURS)).to_pydatetime()

    lines = [
        f'{_SIGNAL_BADGE.get(signal, _SIGNAL_BADGE[None])} · *{asset}* {config.TIMEFRAME}',
        '',
        f'`{_num(last_krw)} KRW`  |  `{_num(close_usd)} USD`  {_pct(change_pct)}',
        '',
        '*지표*',
        f'• EMA{config.EMA_FAST} `{_num(ema_fast)}` / EMA{config.EMA_SLOW} `{_num(ema_slow)}`',
        f'• 이격 `{_num(diff)}` ({_pct(diff_pct)})',
        f'• RSI({config.RSI_PERIOD}) `{_num(rsi)}`{_rsi_label(rsi)}',
        f'• ATR({config.ATR_PERIOD}) `{_num(atr)}` (변동성 {_pct(atr_pct, sign=False)})',
        f'• 거래량 `{_num(volume, 2)}` (20봉평균 대비 {_pct(vol_pct)})',
    ]

    # 보유 현황 — 잔고 조회에 실패했으면 그 사실을 숨기지 않는다.
    lines += ['', '*보유*']
    if accounts is None:
        lines.append('• 잔고 조회 실패 (주문 실행 불가 상태)')
    else:
        coin = bithumb.available(accounts, asset)
        krw = bithumb.available(accounts, 'KRW')
        coin_krw = coin * last_krw if (coin and last_krw) else 0.0
        lines.append(f'• {asset} `{coin:.8f}` ≈ `{_num(coin_krw)} KRW`')
        lines.append(f'• KRW `{_num(krw)}`')
        lines.append(f'• 평가총액 ≈ `{_num(coin_krw + krw)} KRW`')
        if signal == 'buy':
            budget = krw if coin_krw >= config.HOLD_THRESHOLD_KRW else krw * 0.5
            spend = int(budget / (1 + config.FEE_RATE) * config.USABLE_RATIO)
            if spend < config.MIN_ORDER_KRW:
                lines.append(f'• ⚠️ 매수 예정액 `{_num(spend)}` < 최소 `{_num(config.MIN_ORDER_KRW)}` → 생략')
            else:
                lines.append(f'• 매수 예정 `{_num(spend)} KRW`')
        elif signal == 'sell':
            volume_to_sell = coin * config.USABLE_RATIO
            proceeds = volume_to_sell * last_krw if last_krw else 0
            if proceeds < config.MIN_ORDER_KRW:
                lines.append(f'• ⚠️ 매도 예정액 `{_num(proceeds)}` < 최소 `{_num(config.MIN_ORDER_KRW)}` → 생략')
            else:
                lines.append(f'• 매도 예정 `{volume_to_sell:.8f}` ≈ `{_num(proceeds)} KRW`')

    lines += [
        '',
        '*캔들*',
        f"• 마감 {close_kst.strftime('%m-%d %H:%M')} KST "
        f"({candle_close.strftime('%m-%d %H:%M')} UTC)",
        f'• 다음 마감까지 {_until(next_close)}',
    ]

    if prev_signal:
        prev_ts, prev_kind, prev_price = prev_signal
        prev_ts = pd.Timestamp(prev_ts)
        if prev_ts.tzinfo is None:
            prev_ts = prev_ts.tz_localize('UTC')
        move = ((close_usd - prev_price) / prev_price * 100) if (close_usd and prev_price) else None
        lines += [
            '',
            '*직전 신호*',
            f"• {prev_kind.upper()} {prev_ts.tz_convert(config.KST).strftime('%m-%d %H:%M')} KST "
            f'@ `{_num(prev_price)}` → 현재 {_pct(move)}',
        ]

    lines += ['', f'_모드: {config.mode_label()}_']
    return '\n'.join(lines)


def build_caption(asset, ind, signal, last_krw):
    """차트에 붙일 짧은 캡션."""
    badge = _SIGNAL_BADGE.get(signal, _SIGNAL_BADGE[None])
    return (
        f'{badge} · {asset} {config.TIMEFRAME}\n'
        f'EMA{config.EMA_FAST} {_num(_last(ind.ema_fast))} / '
        f'EMA{config.EMA_SLOW} {_num(_last(ind.ema_slow))} · '
        f'{_num(last_krw)} KRW'
    )


def build_daily(rows, accounts=None):
    """일봉 3신호 전략 리포트.

    rows: [(asset, alloc, state_dict, last_krw), ...]
    state_dict 는 strategy.daily_state() 결과.
    """
    now = datetime.now(timezone.utc).astimezone()
    lines = [f'📐 *일봉 전략 리포트*  ·  {now:%Y-%m-%d %H:%M}', '']

    total_target = 0.0
    changed = []
    for asset, alloc, st, last_krw in rows:
        mark = lambda b: '🟢' if b else '🔴'
        w = st['weight']
        pw = st['prev_weight']
        arrow = ''
        if pw is not None and abs(w - pw) > 1e-9:
            arrow = f'  ← {pw*100:.0f}% 에서 변경'
            changed.append(asset)
        if alloc:
            total_target += alloc * w
            head = f'*{asset}*  배분 {alloc:.0%}  ·  비중 `{w*100:.0f}%`  →  전체 `{alloc*w*100:.0f}%`{arrow}'
        else:
            head = f'*{asset}*  _관찰용_  ·  비중 `{w*100:.0f}%`{arrow}'
        lines.append(head)
        lines.append(
            f'   {mark(st["fast"])} 단기  {mark(st["st"])} 슈퍼트렌드  {mark(st["slow"])} 장기'
            f'   ·  점수 `{st["score"]:.2f}`  ·  RSI `{st["rsi"]:.0f}`'
        )
        if last_krw:
            lines.append(f'   현재가 `{last_krw:,.0f} KRW`')
        lines.append('')

    lines.append(f'*목표 총 노출* `{total_target*100:.0f}%`  ·  현금 `{(1-total_target)*100:.0f}%`')

    if accounts is not None:
        krw = bithumb.available(accounts, 'KRW')
        holdings, total = [], krw
        for asset, alloc, st, last_krw in rows:
            q = bithumb.available(accounts, asset)
            v = q * last_krw if (q and last_krw) else 0.0
            total += v
            if v > 0:
                holdings.append((asset, alloc, v))
        lines += ['', '*현재 보유*', f'   총 `{total:,.0f} KRW`  (현금 `{krw:,.0f}`)']
        for asset, alloc, v in holdings:
            cur_pct = v/total*100 if total else 0
            tgt = next((a*s['weight']*100 for as_, a, s, _ in rows if as_ == asset), 0)
            gap = (tgt - cur_pct)/100*total
            note = ''
            if alloc:
                note = (f'  → 목표 {tgt:.0f}%  ({gap:+,.0f} KRW)'
                        if abs(gap) >= config.MIN_ORDER_KRW else '  → 목표 근처')
            lines.append(f'   {asset} `{v:,.0f}` ({cur_pct:.0f}%){note}')

    if changed:
        lines += ['', f'⚠️ *조정 필요* — {", ".join(changed)} 비중이 바뀌었습니다']
    else:
        lines += ['', '_비중 변동 없음 — 조치 불필요_']

    lines += ['', f'_모드: {config.mode_label()} · 주문은 수동_']
    return '\n'.join(lines)


def build_order_result(asset, side, result, detail):
    """주문 실행 결과 메시지."""
    if result is None:
        return f'⚠️ *{asset} {side.upper()} 주문 실패* — {detail}'
    if result.get('mock'):
        return f'🧪 *{asset} {side.upper()} 모의 주문* — {detail}'
    status = result.get('status_code')
    body = result.get('response') or {}
    uuid_ = body.get('uuid', '') if isinstance(body, dict) else ''
    icon = '✅' if isinstance(status, int) and status < 400 else '❌'
    text = f'{icon} *{asset} {side.upper()} 주문* status `{status}`\n{detail}'
    if uuid_:
        text += f'\n`{uuid_}`'
    return text
