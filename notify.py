"""텔레그램 전송."""

import logging

import requests

import config

_API = 'https://api.telegram.org/bot{token}/{method}'


def _configured():
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        return True
    logging.warning('Telegram 토큰/챗아이디 미설정. 전송 생략.')
    return False


def send_text(text):
    """메시지 전송. 성공하면 True."""
    if not _configured():
        return False
    url = _API.format(token=config.TELEGRAM_BOT_TOKEN, method='sendMessage')
    try:
        r = requests.post(
            url,
            json={'chat_id': config.TELEGRAM_CHAT_ID, 'text': text,
                  'parse_mode': 'Markdown', 'disable_web_page_preview': True},
            timeout=10,
        )
        if r.status_code != 200:
            # Markdown 파싱 실패는 400 으로 돌아온다. 원문이라도 전달되게 재시도한다.
            logging.warning('Telegram sendMessage %s: %s', r.status_code, r.text[:200])
            r = requests.post(
                url,
                json={'chat_id': config.TELEGRAM_CHAT_ID, 'text': text},
                timeout=10,
            )
        r.raise_for_status()
        return True
    except Exception as e:
        logging.exception('Telegram 전송 실패: %s', e)
        return False


def send_photo(path, caption=None):
    """사진 전송. 성공하면 True."""
    if not _configured():
        return False
    url = _API.format(token=config.TELEGRAM_BOT_TOKEN, method='sendPhoto')
    try:
        with open(path, 'rb') as f:
            data = {'chat_id': config.TELEGRAM_CHAT_ID}
            if caption:
                data['caption'] = caption
            r = requests.post(url, data=data, files={'photo': f}, timeout=30)
        if r.status_code != 200:
            logging.warning('Telegram sendPhoto %s: %s', r.status_code, r.text[:200])
        r.raise_for_status()
        return True
    except Exception as e:
        logging.exception('Telegram 사진 전송 실패: %s', e)
        return False
