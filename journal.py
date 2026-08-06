#!/usr/bin/env python3
"""시뮬레이션 관찰 일지. 오늘의 신호를 계산해 CSV 한 줄로 남긴다.

프로토콜은 docs/rehearsal.md. 기록이 6개월 이어지려면 손이 덜 가야 해서,
계산되는 값은 전부 자동으로 채우고 사람은 집행 여부만 적는다.

  python3 journal.py                     오늘 신호 기록
  python3 journal.py --note "..."        메모를 붙여서
  python3 journal.py --show              최근 기록 보기
  python3 journal.py --summary           규칙 위반·집행률 집계
"""
import argparse
import csv
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import market
import strategy

PATH = os.path.join(config.BASE_DIR, 'journal.csv')
FIELDS = [
    'date', 'asset', 'score', 'target_pct', 'current_pct', 'gap_pct',
    'band', 'instructed_krw', 'executed', 'executed_krw', 'fill_price',
    'fee_krw', 'violation', 'note',
]


def today_rows():
    """자산별 오늘 상태와 빗썸 KRW 시세.

    신호는 바이낸스 USD 봉으로 계산하지만 비중은 KRW 평가액으로 따져야 한다.
    두 단위를 섞으면 격차가 환율만큼 어긋난다 — 백테스트에서 이미 한 번
    겪은 실수다(docs/code-review-2026-08-06.md 9번).
    """
    import bithumb
    out = []
    for asset, spec in config.ASSETS.items():
        if not spec['alloc']:
            continue                      # 관찰용 종목은 일지에 넣지 않는다
        df = market.fetch_ohlcv(spec['binance'], '1d', config.DAILY_HISTORY_DAYS)
        if df is None:
            print(f'⚠️  {asset}: 일봉 조회 실패 — 건너뜀', file=sys.stderr)
            continue
        df = market.drop_unclosed(df, hours=24)   # 진행 중인 봉은 쓰지 않는다
        st = strategy.daily_state(strategy.analyze_daily(df))
        if st['insufficient']:
            print(f"⚠️  {asset}: 봉 부족 ({st['bars']}/{st['bars_needed']}) — "
                  f"장기 신호가 빠진 점수다", file=sys.stderr)
        ticker = bithumb.fetch_ticker(spec['bithumb'])
        out.append((asset, spec['alloc'], st, ticker['last'] if ticker else None))
    return out


def current_weights(accounts, rows):
    """보유 평가액(KRW) 기준 현재 비중. 잔고나 시세가 없으면 빈 dict."""
    if accounts is None or any(krw is None for *_, krw in rows):
        return {}
    import bithumb
    total = bithumb.available(accounts, 'KRW')
    vals = {}
    for asset, _, _, krw in rows:
        v = bithumb.available(accounts, asset) * krw
        vals[asset] = v
        total += v
    return {a: v / total for a, v in vals.items()} if total else {}


def record(rows, accounts, note='', quiet=False):
    """계산된 상태를 CSV 한 줄로 남긴다. 봇의 일봉 리포트에서도 부른다.

    rows: [(asset, alloc, daily_state, last_krw), ...] — 배분 0 인 종목은 뺀 것.
    같은 날짜가 이미 있으면 아무것도 하지 않는다(폴링이 여러 번 돌아도 안전).
    """
    date = datetime.now(ZoneInfo(config.KST)).strftime('%Y-%m-%d')
    if os.path.exists(PATH):
        with open(PATH, encoding='utf-8') as f:
            if any(r['date'] == date for r in csv.DictReader(f)):
                return False

    cur = current_weights(accounts, rows)
    total_krw = None
    if cur:
        import bithumb
        total_krw = bithumb.available(accounts, 'KRW') + sum(
            bithumb.available(accounts, a) * krw for a, _, _, krw in rows)

    is_new_file = not os.path.exists(PATH)
    breached = False
    with open(PATH, 'a', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new_file:
            w.writeheader()
        for asset, alloc, st, krw in rows:
            tgt = alloc * st['weight']
            c = cur.get(asset)
            gap = (tgt - c) if c is not None else None
            inband = (abs(gap) < config.REBAL_BAND) if gap is not None else None
            breached = breached or inband is False
            w.writerow({
                'date': date, 'asset': asset,
                'score': f"{st['score']:.2f}",
                'target_pct': f'{tgt*100:.1f}',
                'current_pct': '' if c is None else f'{c*100:.1f}',
                'gap_pct': '' if gap is None else f'{gap*100:+.1f}',
                'band': '' if inband is None else ('in' if inband else 'OUT'),
                # 밴드를 벗어났을 때만 지시 금액을 채운다. 나머지는 사람이 적는다.
                'instructed_krw': ('' if (inband is not False or not total_krw)
                                   else f'{gap*total_krw:+.0f}'),
                'executed': '', 'executed_krw': '',
                'fill_price': '' if krw is None else f'{krw:.0f}',
                'fee_krw': '', 'violation': '', 'note': note,
            })
            if quiet:
                continue
            mark = {None: '?', True: '·', False: '⚠️'}[inband]
            line = f'{mark} {asset}  점수 {st["score"]:.2f}  목표 {tgt*100:.0f}%'
            if c is not None:
                line += f'  현재 {c*100:.0f}%  격차 {gap*100:+.0f}%p'
                if inband is False and total_krw:
                    line += f'  → {gap*total_krw:+,.0f} KRW'
            print(line)

    if quiet:
        return True
    print(f'\n{PATH}')
    if breached:
        print('밴드를 벗어났다. 집행하기 전에 이 줄부터 채울 것 '
              '(docs/rehearsal.md — 먼저 기록하고 나중에 집행한다)')
    elif not cur:
        print('잔고를 못 읽어 현재 비중이 비어 있다. --no-accounts 였거나 조회 실패다')
    return True


def append(note='', use_accounts=True):
    """CLI 진입점. 시세와 잔고를 직접 받아 기록한다."""
    accounts = None
    if use_accounts:
        try:
            import bithumb
            accounts = bithumb.fetch_accounts()
        except Exception as e:                      # noqa: BLE001
            print(f'잔고 조회 실패 ({e}) — 현재 비중은 비워 둔다', file=sys.stderr)

    rows = today_rows()
    if not rows:
        print('기록할 종목이 없다', file=sys.stderr)
        return
    if not record(rows, accounts, note):
        print('오늘 기록이 이미 있다')


def show(n=20):
    if not os.path.exists(PATH):
        print('아직 기록이 없다'); return
    with open(PATH, encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    print(f'{"날짜":<12}{"종목":<6}{"점수":>6}{"목표":>7}{"현재":>7}{"격차":>8}'
          f'{"밴드":>6}  {"집행":<6}{"위반":<8}메모')
    print('-' * 88)
    for r in rows[-n:]:
        print(f'{r["date"]:<12}{r["asset"]:<6}{r["score"]:>6}{r["target_pct"]:>7}'
              f'{r["current_pct"]:>7}{r["gap_pct"]:>8}{r["band"]:>6}  '
              f'{r["executed"]:<6}{r["violation"]:<8}{r["note"][:28]}')


def summary():
    if not os.path.exists(PATH):
        print('아직 기록이 없다'); return
    with open(PATH, encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print('아직 기록이 없다'); return
    days = len({r['date'] for r in rows})
    out = [r for r in rows if r['band'] == 'OUT']
    done = [r for r in out if r['executed'].strip().lower() in ('y', 'yes', 'o', '집행')]
    viol = [r for r in rows if r['violation'].strip()]
    first, last = rows[0]['date'], rows[-1]['date']

    print(f'관찰 {first} ~ {last}  ({days}일)')
    print()
    print(f'  밴드 이탈       {len(out)}회')
    print(f'  집행            {len(done)}회'
          + (f'  ({len(done)/len(out)*100:.0f}%)' if out else ''))
    print(f'  규칙 위반       {len(viol)}건')
    if viol:
        for r in viol:
            print(f'    · {r["date"]} {r["asset"]}  {r["violation"]}  {r["note"][:40]}')

    # 판정은 기간과 위반을 함께 본다. 위반 0건이어도 기간이 짧으면 판정하지 않는다.
    print()
    if viol:
        print(f'  ❌ 라이브 보류 — 규칙 위반 {len(viol)}건')
        if len(viol) >= 3:
            print('     3건 이상이다. 전략이 아니라 이 방식이 나와 맞지 않는 것일 수 '
                  '있다 (docs/rehearsal.md — 그만두는 조건)')
    elif days < 180:
        print(f'  ⏳ 관찰 중 — {days}/180일, 남은 {180-days}일')
        print('     위반 0건이지만 아직 판정하지 않는다. 하락 구간을 한 번은 '
              '통과해야 한다')
    else:
        print('  ✅ 위반 0건으로 6개월 완주 — docs/rehearsal.md 의 나머지 조건을 '
              '직접 확인할 것')
        print('     (하락 -15% 구간 통과 / 월간 대조 불일치 0건 / 리포트 미수신 3일 이하)')


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--note', default='', help='메모')
    ap.add_argument('--show', action='store_true', help='최근 기록')
    ap.add_argument('--summary', action='store_true', help='집계')
    ap.add_argument('--no-accounts', action='store_true', help='잔고 조회 생략')
    a = ap.parse_args()
    if a.show:
        show()
    elif a.summary:
        summary()
    else:
        append(a.note, use_accounts=not a.no_accounts)
    return 0


if __name__ == '__main__':
    sys.exit(main())
