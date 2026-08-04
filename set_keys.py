#!/usr/bin/env python3
"""빗썸 API 2.0 키 교체 + 즉시 검증 스크립트.

터미널에서 직접 실행하세요:
    python3 set_keys.py

키는 화면에 표시되지 않고, 셸 히스토리에도 남지 않습니다.
"""

import os
import re
import sys
import time
import uuid
import shutil
import getpass
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE, '.env')


def public_ip():
    try:
        return requests.get('https://api.ipify.org', timeout=10).text.strip()
    except Exception:
        return None


def verify(access_key, secret_key):
    """새 키로 /v1/accounts 를 호출해 상태를 진단한다."""
    import jwt as _jwt
    payload = {
        'access_key': access_key,
        'nonce': str(uuid.uuid4()),
        'timestamp': int(time.time() * 1000),
    }
    token = _jwt.encode(payload, secret_key, algorithm='HS256')
    if isinstance(token, bytes):
        token = token.decode()
    try:
        r = requests.get('https://api.bithumb.com/v1/accounts',
                         headers={'Authorization': f'Bearer {token}'}, timeout=10)
    except Exception as e:
        return False, f'네트워크 오류: {e}'

    try:
        data = r.json()
    except Exception:
        return False, f'응답 파싱 실패 (status={r.status_code}): {r.text[:200]}'

    if isinstance(data, list):
        krw = next((it for it in data if isinstance(it, dict) and it.get('currency') == 'KRW'), None)
        coins = [it.get('currency') for it in data
                 if isinstance(it, dict) and it.get('currency') != 'KRW']
        msg = '인증 성공. 잔고 조회 정상.'
        if krw:
            bal = float(krw.get('balance') or 0)
            msg += f'\n   KRW 잔고: {bal:,.0f}원'
        if coins:
            msg += f'\n   보유 코인: {", ".join(coins)}'
        return True, msg

    name = ''
    if isinstance(data, dict) and isinstance(data.get('error'), dict):
        name = data['error'].get('name', '')
        message = data['error'].get('message', '')
    else:
        message = str(data)[:200]

    hints = {
        'NotAllowIP': (
            f'이 PC의 IP가 화이트리스트에 없습니다.\n'
            f'   빗썸 API 관리에서 다음 IP를 등록하세요: {public_ip() or "(조회 실패)"}'
        ),
        'invalid_access_key': 'access key가 존재하지 않습니다. 값을 다시 확인하세요.',
        'jwt_verification': 'secret key가 맞지 않습니다. 값을 다시 확인하세요.',
    }
    hint = hints.get(name, message)
    return False, f'[{name or "오류"}] status={r.status_code}\n   {hint}'


def update_env(access_key, secret_key):
    if not os.path.exists(ENV_PATH):
        print(f'!! .env 를 찾을 수 없습니다: {ENV_PATH}')
        sys.exit(1)

    backup = f'{ENV_PATH}.bak.{time.strftime("%Y%m%d_%H%M%S")}'
    shutil.copy2(ENV_PATH, backup)

    with open(ENV_PATH) as f:
        lines = f.read().splitlines()

    updates = {'BITHUMB_API_KEY': access_key, 'BITHUMB_API_SECRET': secret_key}
    seen = set()
    out = []
    for line in lines:
        m = re.match(r'^\s*([A-Z_]+)\s*=', line)
        if m and m.group(1) in updates:
            key = m.group(1)
            out.append(f'{key}={updates[key]}')
            seen.add(key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in seen:
            out.append(f'{key}={val}')

    with open(ENV_PATH, 'w') as f:
        f.write('\n'.join(out) + '\n')
    os.chmod(ENV_PATH, 0o600)
    return backup


def main():
    print('=== 빗썸 API 2.0 키 교체 ===')
    print(f'현재 이 PC의 공인 IP: {public_ip() or "(조회 실패)"}')
    print('입력값은 화면에 표시되지 않습니다.\n')

    access_key = getpass.getpass('Access Key : ').strip()
    secret_key = getpass.getpass('Secret Key : ').strip()

    if not access_key or not secret_key:
        print('!! 값이 비어 있어 중단합니다.')
        sys.exit(1)

    print('\n[1/2] 새 키로 인증 확인 중...')
    ok, msg = verify(access_key, secret_key)
    print(f'   {msg}')

    if not ok:
        ans = input('\n검증에 실패했습니다. 그래도 .env 에 저장할까요? [y/N] ').strip().lower()
        if ans != 'y':
            print('중단했습니다. .env 는 변경되지 않았습니다.')
            sys.exit(1)

    print('\n[2/2] .env 갱신 중...')
    backup = update_env(access_key, secret_key)
    print(f'   저장 완료. 백업: {os.path.basename(backup)}')

    print('\n남은 단계:')
    print('   봇 재시작 → launchctl kickstart -k gui/$(id -u)/ai.tradebot.bot')


if __name__ == '__main__':
    main()
