#!/usr/bin/env python3
"""
backend/tools/smoke_test.py

Simple smoke test that exercises the FastAPI backend streaming and RKLLM board.

Usage:
  python backend/tools/smoke_test.py

It will call these endpoints (defaults to localhost:8000):
  - GET /api/llm/status
  - GET /api/llm/autostart_log
  - POST /api/llm/warmup
  - POST /api/chat_local
  - POST /api/chat/stream  (streams and prints chunks)
  - direct RKLLM HTTP test at http://<LOCAL_BOARD_IP>:<LOCAL_BOARD_PORT>/v1/chat/completions

This file is intended for manual runs on your workstation to gather diagnostics.
"""

import os
import sys
import time
import json
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENV_PATH = os.path.join(ROOT, 'backend', '.env')

def read_env(path):
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, 'r', encoding='utf-8') as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith('#'):
                continue
            if '=' in ln:
                k, v = ln.split('=', 1)
                v = v.strip().strip('"').strip("'")
                env[k.strip()] = v
    return env


def pretty(obj):
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False)
    except Exception:
        return str(obj)


def main():
    env = read_env(ENV_PATH)
    backend_url = os.getenv('BACKEND_URL', 'http://localhost:8000')

    print('== Smoke test: backend status ==')
    try:
        r = requests.get(f"{backend_url}/api/llm/status", timeout=5)
        print('status:', r.status_code)
        print(pretty(r.json()))
    except Exception as e:
        print('status request failed:', e)

    print('\n== autostart log ==')
    try:
        r = requests.get(f"{backend_url}/api/llm/autostart_log", timeout=5)
        print('log:', r.status_code)
        print(r.text)
    except Exception as e:
        print('autostart_log failed:', e)

    print('\n== warmup endpoint ==')
    print('warmup endpoint removed from API; skipping warmup test')

    print('\n== chat_local (fallback) ==')
    try:
        payload = {'text': 'Hello from smoke test'}
        r = requests.post(f"{backend_url}/api/chat_local", json=payload, timeout=30)
        print('chat_local:', r.status_code)
        try:
            print(pretty(r.json()))
        except Exception:
            print(r.text)
    except Exception as e:
        print('chat_local failed:', e)

    print('\n== streaming /api/chat/stream (prints chunks as received) ==')
    try:
        payload = {'message': 'What is the temperature in the living room?'}
        with requests.post(f"{backend_url}/api/chat/stream", json=payload, stream=True, timeout=60) as r:
            print('stream status:', r.status_code)
            if r.status_code != 200:
                print('body:', r.text)
            else:
                start = time.time()
                try:
                    for line in r.iter_lines(decode_unicode=True):
                        if line:
                            print('>>', line)
                        # stop if done event seen
                        if 'event: done' in (line or '') or '[DONE]' in (line or ''):
                            break
                        # timeout protection
                        if time.time() - start > 40:
                            print('stream read timeout')
                            break
                except requests.exceptions.ChunkedEncodingError as e:
                    print('stream read error:', e)
    except Exception as e:
        print('streaming request failed:', e)

    # Direct RKLLM board HTTP test
    board_ip = os.getenv('LOCAL_BOARD_IP') or env.get('LOCAL_BOARD_IP')
    board_port = os.getenv('LOCAL_BOARD_PORT') or env.get('LOCAL_BOARD_PORT', '8080')
    model = os.getenv('RKLLM_MODEL_NAME') or os.getenv('RKLLM_MODEL_ID') or env.get('RKLLM_MODEL_ID') or ''
    if model:
        model = os.path.splitext(model)[0]

    print('\n== direct RKLLM board HTTP test ==')
    if not board_ip:
        print('LOCAL_BOARD_IP not found in env or backend/.env; skipping direct board test')
        return

    board_url = f"http://{board_ip}:{board_port}".rstrip('/')
    print('Board URL:', board_url)
    endpoint = f"{board_url}/v1/chat/completions"
    body = {
        'model': model or 'Qwen2.5-3B-Instruct',
        'messages': [{'role': 'user', 'content': 'hi'}],
        'max_tokens': 16,
        'stream': False
    }
    try:
        r = requests.post(endpoint, json=body, timeout=20)
        print('board response status:', r.status_code)
        try:
            print(pretty(r.json()))
        except Exception:
            print(r.text)
    except Exception as e:
        print('direct board test failed:', e)


if __name__ == '__main__':
    main()
