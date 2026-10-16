"""Bounded vision inference and a real image/JSON readiness check."""
import base64
import json
import os
import secrets
import struct
import sys
import time
import zlib

REQUEST_TIMEOUT = 60
ATTEMPTS = 3


class ProviderUnavailable(RuntimeError):
    def __init__(self, message, fatal=False):
        super().__init__(message)
        self.fatal = fatal


def permanent_failure(error):
    message = str(error).lower()
    return getattr(error, 'status_code', None) in (400, 401, 403, 404, 422) or any(
        part in message for part in ('does not accept image', 'non-text content parts',
        'all_accounts_inactive', 'all upstream accounts are inactive', 'no active accounts'))


def infer(client, model, messages, parse):
    for attempt in range(ATTEMPTS):
        try:
            response = client.chat.completions.create(
                model=model, max_tokens=600, messages=messages, timeout=REQUEST_TIMEOUT)
            return parse(response.choices[0].message.content)
        except Exception as error:
            fatal = permanent_failure(error)
            status = getattr(error, 'status_code', None)
            category = 'invalid model output' if isinstance(error, (ValueError, IndexError, AttributeError)) else type(error).__name__
            detail = f'{category}' + (f' (HTTP {status})' if status else '')
            if fatal or attempt == ATTEMPTS - 1:
                message = str(error).lower()
                if 'does not accept image' in message or 'non-text content parts' in message:
                    detail += ': the selected provider route rejects images'
                elif any(part in message for part in ('all_accounts_inactive', 'all upstream accounts are inactive', 'no active accounts')):
                    detail += ': no active provider accounts are connected'
                elif 'valid api key' in message or status in (401, 403):
                    detail += ': provider authentication failed'
                hint = 'Check OmniRoute vision routes and connected provider accounts.' if fatal else 'Inference failed after bounded retries.'
                raise ProviderUnavailable(f'{detail}. {hint}', fatal=fatal) from error
            print(f'mail-writer {detail}; retry {attempt + 1}/{ATTEMPTS} in {2 ** attempt}s', file=sys.stderr, flush=True)
            time.sleep(2 ** attempt)


def preflight(client, model):
    # A generated color image requires actual vision, not merely a listed model name.
    color, rgb = secrets.choice([('red', (255, 0, 0)), ('green', (0, 255, 0)), ('blue', (0, 0, 255))])
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 32, 32, 8, 2, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress((b'\x00' + bytes(rgb) * 32) * 32)) + chunk(b'IEND', b'')
    messages = [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'Identify the dominant color of this image. Return only a JSON object with exactly one key, color, whose value is red, green, or blue. No markdown.'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(png).decode()}}
    ]}]
    def parse(raw):
        value = json.loads(raw)
        if value != {'color': color}:
            raise ValueError('Vision probe returned an incorrect color or JSON shape')
        return value
    infer(client, model, messages, parse)
    print('Vision and JSON preflight passed.', flush=True)


def configured_client():
    from openai import OpenAI
    base = os.environ.get('MAIL_WRITER_BASE_URL') or os.environ.get('TRIAGE_BASE_URL')
    model = os.environ.get('MAIL_WRITER_MODEL') or os.environ.get('TRIAGE_MODEL')
    key = os.environ.get('MAIL_WRITER_API_KEY') or os.environ.get('TRIAGE_API_KEY')
    if not (base and key and model):
        raise ValueError('Mail writer endpoint, model and API key must be configured')
    return OpenAI(base_url=base, api_key=key, timeout=REQUEST_TIMEOUT, max_retries=0), model
