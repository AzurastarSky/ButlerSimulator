#!/usr/bin/env python3
"""
Quick diagnostic script to check if rkllm3-server is running and reachable.

Run this to diagnose connection issues.
"""

import os
import sys
import requests
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
ENV_PATH = ROOT / 'backend' / '.env'

def read_env(path):
    env = {}
    if not path.exists():
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

def main():
    env = read_env(ENV_PATH)
    board_ip = os.getenv('LOCAL_BOARD_IP') or env.get('LOCAL_BOARD_IP', '192.168.0.222')
    board_port = os.getenv('LOCAL_BOARD_PORT') or env.get('LOCAL_BOARD_PORT', '8080')
    model_name = os.getenv('RKLLM_MODEL_NAME') or env.get('RKLLM_MODEL_NAME', '')
    
    print(f"Checking rkllm3-server at {board_ip}:{board_port}")
    print(f"Expected model: {model_name or '(not configured)'}")
    print()
    
    # Check /v1/models endpoint
    models_url = f"http://{board_ip}:{board_port}/v1/models"
    print(f"Testing: {models_url}")
    try:
        resp = requests.get(models_url, timeout=5)
        print(f"✓ Status: {resp.status_code}")
        if resp.status_code == 200:
            print(f"✓ Response: {resp.text[:200]}")
        else:
            print(f"✗ Error response: {resp.text}")
    except requests.exceptions.ConnectionError as e:
        print(f"✗ Connection refused - server is not running or not reachable")
        print(f"  Error: {e}")
        print()
        print("Possible causes:")
        print("  1. rkllm3-server process is not started on the board")
        print("  2. Server crashed during startup (check /tmp/rkllm_server.log on board)")
        print("  3. Wrong IP or port in backend/.env")
        print("  4. Firewall blocking the connection")
        return 1
    except Exception as e:
        print(f"✗ Error: {e}")
        return 1
    
    print()
    
    # Try a simple chat completion
    if model_name:
        chat_url = f"http://{board_ip}:{board_port}/v1/chat/completions"
        print(f"Testing chat completion: {chat_url}")
        try:
            resp = requests.post(chat_url, json={
                'model': model_name,
                'messages': [{'role': 'user', 'content': 'hi'}],
                'max_tokens': 5,
                'stream': False
            }, timeout=30)
            print(f"✓ Status: {resp.status_code}")
            if resp.status_code == 200:
                print(f"✓ Chat works! Response: {resp.text[:200]}")
            else:
                print(f"✗ Error: {resp.text}")
        except Exception as e:
            print(f"✗ Chat request failed: {e}")
            return 1
    
    print()
    print("✓ Server is reachable and working!")
    return 0

if __name__ == '__main__':
    sys.exit(main())
