"""빗썸 Open API 2.0 클라이언트.

구 1.0(/public, /info, HMAC-SHA512 + Api-Sign 헤더)은 종료 예정이라 쓰지 않는다.
2.0 은 업비트 계열 규격으로, 인증은 JWT(HS256)이고 마켓 코드는 'KRW-BTC' 형식이다.

시장가 주문 규격이 매수/매도에서 다르다는 점이 중요하다:
    매수(bid) : ord_type='price',  price  = 총 KRW 금액
    매도(ask) : ord_type='market', volume = 코인 수량
매도에 ord_type='price' 를 보내면 400 으로 거부된다.
"""

import hashlib
import json
import logging
import os
import time
import uuid
from urllib.parse import urlencode

import jwt
import requests

import config
import notify

API_BASE = 'https://api.bithumb.com'

# 같은 사유의 인증 실패를 반복 통보하지 않기 위한 최근 발송 시각.
_last_auth_alert = {}


# ── 마켓 코드 ───────────────────────────────────────────────────────────────
def to_market(symbol):
    """'BTC/KRW' -> 'KRW-BTC'. 이미 2.0 형식이면 그대로 반환."""
    if '-' in symbol:
        return symbol
    base, quote = symbol.split('/')
    return f'{quote}-{base}'


def asset_of(symbol):
    """'BTC/KRW' 또는 'KRW-BTC' -> 'BTC'."""
    return to_market(symbol).split('-')[1]


# ── 인증 ────────────────────────────────────────────────────────────────────
def _auth_header(query=None):
    """JWT 토큰을 담은 Authorization 헤더. query 가 있으면 query_hash 를 포함한다."""
    payload = {
        'access_key': config.BITHUMB_API_KEY,
        'nonce': str(uuid.uuid4()),
        'timestamp': int(time.time() * 1000),
    }
    if query:
        qs = urlencode(list(query.items())).encode('utf-8')
        payload['query_hash'] = hashlib.sha512(qs).hexdigest()
        payload['query_hash_alg'] = 'SHA512'
    token = jwt.encode(payload, config.BITHUMB_API_SECRET, algorithm='HS256')
    if isinstance(token, bytes):
        token = token.decode()
    return {'Authorization': f'Bearer {token}'}


def _has_credentials(action):
    if config.BITHUMB_API_KEY and config.BITHUMB_API_SECRET:
        return True
    logging.warning('Bithumb API 키 미설정. %s 생략.', action)
    return False


def current_public_ip():
    try:
        return requests.get('https://api.ipify.org', timeout=5).text.strip()
    except Exception:
        return None


def _alert_auth_failure(name, message):
    """인증 실패를 텔레그램으로 알린다.

    IP 화이트리스트에서 막히면 봇은 계속 돌지만 주문만 조용히 실패한다.
    실제로 이 상태를 몇 달간 눈치채지 못한 적이 있어 사유를 즉시 통보한다.
    """
    now = time.time()
    if now - _last_auth_alert.get(name, 0) < config.AUTH_ALERT_INTERVAL_SEC:
        return
    _last_auth_alert[name] = now
    if name == 'NotAllowIP':
        text = (
            '⚠️ *빗썸 API 차단 — 등록되지 않은 IP*\n\n'
            f'현재 IP: `{current_public_ip() or "조회 실패"}`\n\n'
            '빗썸 API 관리에서 위 IP를 등록해야 주문이 재개됩니다.\n'
            '그때까지 신호는 계산되지만 매수/매도는 실행되지 않습니다.'
        )
    else:
        text = f'⚠️ *빗썸 인증 실패*: `{name}`\n{message}'
    notify.send_text(text)


def _unwrap(response, what):
    """빗썸 응답을 파싱한다. 실패 시 None.

    오류는 HTTP 상태코드와 함께 {'error': {'name':..., 'message':...}} 로 온다.
    이를 리스트로 가정하고 순회하면 dict 의 키(문자열)를 돌게 되어
    'str' object has no attribute 'get' 로 죽는다.
    """
    try:
        data = response.json()
    except ValueError:
        logging.error('%s: JSON 아님 (status=%s) %s', what, response.status_code, response.text[:200])
        return None
    if isinstance(data, dict) and 'error' in data:
        err = data['error'] if isinstance(data['error'], dict) else {'message': str(data['error'])}
        name = err.get('name') or 'unknown'
        logging.error('%s 거부 (status=%s, name=%s): %s',
                      what, response.status_code, name, err.get('message', ''))
        _alert_auth_failure(name, err.get('message', ''))
        return None
    return data


# ── 시세 (인증 불필요) ──────────────────────────────────────────────────────
def fetch_ticker(symbol):
    """현재가 조회. {'last': float, 'info': {...}} 또는 None."""
    market = to_market(symbol)
    try:
        r = requests.get(f'{API_BASE}/v1/ticker', params={'markets': market}, timeout=10)
    except Exception as e:
        logging.exception('Bithumb ticker 요청 실패 (%s): %s', market, e)
        return None
    data = _unwrap(r, f'Bithumb ticker({market})')
    if not isinstance(data, list) or not data:
        if data is not None:
            logging.error('Bithumb ticker 응답 형식 예상 밖 (%s): %r', market, data)
        return None
    row = data[0]
    return {'last': float(row['trade_price']), 'info': row}


# ── 잔고 ────────────────────────────────────────────────────────────────────
def fetch_accounts():
    """계좌 목록(dict 리스트) 또는 None."""
    if not _has_credentials('잔고 조회'):
        return None
    try:
        r = requests.get(f'{API_BASE}/v1/accounts', headers=_auth_header(), timeout=10)
    except Exception as e:
        logging.exception('Bithumb 잔고 조회 실패: %s', e)
        return None
    data = _unwrap(r, 'Bithumb 잔고 조회')
    if not isinstance(data, list):
        if data is not None:
            logging.error('Bithumb 잔고 응답 형식 예상 밖: %r', data)
        return None
    return [it for it in data if isinstance(it, dict)]


def available(accounts, currency):
    """특정 통화의 주문 가능 수량(= balance - locked)."""
    for it in accounts or []:
        if it.get('currency') == currency:
            return float(it.get('balance') or 0) - float(it.get('locked') or 0)
    return 0.0


def balance_of(accounts, currency):
    """특정 통화의 총 보유량(locked 포함)."""
    for it in accounts or []:
        if it.get('currency') == currency:
            return float(it.get('balance') or 0)
    return 0.0


# ── 주문 ────────────────────────────────────────────────────────────────────
def _save_receipt(prefix, market, payload):
    try:
        config.ensure_dirs()
        path = os.path.join(config.ORDER_DIR, f'{prefix}_{market}_{int(time.time())}.json')
        with open(path, 'w') as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        return path
    except Exception:
        logging.exception('주문 기록 저장 실패')
        return None


def place_market_order(symbol, side, krw=None, volume=None):
    """시장가 주문.

    side='bid' 면 krw(총 KRW 금액), side='ask' 면 volume(코인 수량)이 필요하다.
    반환: {'request':..., 'status_code':..., 'response':...} 또는 None.
    """
    market = to_market(symbol)

    if side == 'bid':
        if krw is None:
            logging.error('매수 주문에 krw 금액이 필요합니다: %s', market)
            return None
        body = {'market': market, 'side': 'bid', 'ord_type': 'price', 'price': str(int(krw))}
        desc = f'{int(krw):,}KRW'
    elif side == 'ask':
        if volume is None:
            logging.error('매도 주문에 volume(코인 수량)이 필요합니다: %s', market)
            return None
        body = {'market': market, 'side': 'ask', 'ord_type': 'market', 'volume': f'{volume:.8f}'}
        desc = f'{volume:.8f}'
    else:
        logging.error('알 수 없는 side: %r', side)
        return None

    if config.LIVE_SIMULATION:
        logging.info('SIM: %s %s %s — 실제 주문을 보내지 않음', market, side, desc)
        receipt = {'mock': True, 'request': body, 'timestamp': int(time.time())}
        _save_receipt('mock_order', market, receipt)
        return receipt

    if not config.LIVE_TRADING:
        logging.info('LIVE_TRADING 꺼짐: %s %s %s 주문 생략', market, side, desc)
        return None

    if not _has_credentials('주문'):
        return None

    try:
        r = requests.post(
            f'{API_BASE}/v1/orders',
            headers={**_auth_header(body), 'Content-Type': 'application/json'},
            data=json.dumps(body),
            timeout=10,
        )
    except Exception as e:
        logging.exception('Bithumb 주문 요청 실패: %s', e)
        return None

    out = {'request': body, 'status_code': r.status_code}
    try:
        out['response'] = r.json()
    except ValueError:
        out['response_text'] = r.text

    _save_receipt('live_order', market, out)
    if r.status_code >= 400:
        logging.error('주문 거부 %s %s %s status=%s %s',
                      market, side, desc, r.status_code, str(out.get('response'))[:200])
    else:
        logging.info('주문 접수 %s %s %s status=%s', market, side, desc, r.status_code)
    return out


def fetch_order(order_uuid):
    """주문 상세 조회. 체결 확인용."""
    if not _has_credentials('주문 조회'):
        return None
    query = {'uuid': order_uuid}
    try:
        r = requests.get(f'{API_BASE}/v1/order', headers=_auth_header(query),
                         params=query, timeout=10)
    except Exception as e:
        logging.exception('Bithumb 주문 조회 실패: %s', e)
        return None
    data = _unwrap(r, 'Bithumb 주문 조회')
    return data if isinstance(data, dict) else None
