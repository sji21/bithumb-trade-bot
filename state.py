"""재시작을 견디는 중복 방지 상태.

두 종류의 상태를 다루며, 스키마가 다르므로 반드시 파일을 분리해서 저장한다.
예전에는 둘 다 last_notified.json 에 써서 서로를 덮어썼고, 그 결과 신호가 날
때마다 'State entry ... malformed' 경고와 함께 중복 방지가 무력화됐다.

    SEND_STATE_FILE   {'BTC': {'<candle_ts>': {'text': bool, 'photo': bool}}}
    SIGNAL_STATE_FILE {'BTC': <candle_ts>}   마지막으로 주문을 처리한 캔들

주문 게이트가 디스크에 있어야 하는 이유: 캔들 진행 중 프로세스가 죽으면
launchd(KeepAlive=true)가 즉시 되살리는데, 메모리 기록만으로는 같은 신호에
주문이 다시 나간다.
"""

import fcntl
import json
import logging
import os

import config

_KEEP_CANDLES = 20  # 캔들별 전송 기록 보관 개수


# ── 공통 입출력 ─────────────────────────────────────────────────────────────
def _read(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logging.warning('상태 파일 형식 이상, 초기화: %s', path)
            return default
        return data
    except Exception:
        logging.exception('상태 파일 읽기 실패: %s', path)
        return default


def _write_atomic(path, data):
    """임시 파일에 쓰고 락을 잡은 뒤 교체한다."""
    try:
        config.ensure_dirs()
        tmp = f'{path}.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        with open(f'{path}.lock', 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                os.replace(tmp, path)
            finally:
                try:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                except Exception:
                    pass
    except Exception:
        logging.exception('상태 파일 쓰기 실패: %s', path)


# ── 주문 게이트 ─────────────────────────────────────────────────────────────
def load_handled_candles():
    """{'BTC': ts, ...} 형태로 마지막 처리 캔들을 읽는다."""
    raw = _read(config.SIGNAL_STATE_FILE, {})
    out = {}
    for asset in config.ASSETS:
        value = raw.get(asset)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            out[asset] = int(value)
        elif isinstance(value, str) and value.isdigit():
            out[asset] = int(value)
    if out:
        logging.info('마지막 처리 캔들 복원: %s', out)
    return out


def mark_handled(handled, asset, candle_ts):
    """주문 처리를 기록하고 즉시 디스크에 반영한다."""
    handled[asset] = candle_ts
    _write_atomic(config.SIGNAL_STATE_FILE, handled)


# ── 전송 기록 ───────────────────────────────────────────────────────────────
def _send_entry(data, asset, candle_ts):
    bucket = data.get(asset)
    if not isinstance(bucket, dict):
        bucket = {}
    entry = bucket.get(str(candle_ts))
    if not isinstance(entry, dict):
        entry = {'text': False, 'photo': False}
    return bucket, entry


def was_sent(asset, candle_ts, kind):
    data = _read(config.SEND_STATE_FILE, {})
    _, entry = _send_entry(data, asset, candle_ts)
    return bool(entry.get(kind))


def mark_sent(asset, candle_ts, kind):
    data = _read(config.SEND_STATE_FILE, {})
    bucket, entry = _send_entry(data, asset, candle_ts)
    entry[kind] = True
    bucket[str(candle_ts)] = entry
    if len(bucket) > _KEEP_CANDLES:
        for key in sorted(bucket, key=lambda k: int(k) if k.isdigit() else 0)[:-_KEEP_CANDLES]:
            bucket.pop(key, None)
    data[asset] = bucket
    _write_atomic(config.SEND_STATE_FILE, data)
