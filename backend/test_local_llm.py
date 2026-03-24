"""
Quick tester for local RKLLM-based LLM via OpenAI-compatible API.
Run this after your RKLLM server is up and `backend/.env` contains `RKLLM_MODEL_ID`.
"""

import asyncio
from backend.services.local_llm_service import LocalLLMService


def main():
    print("Local LLM test script")
    try:
        svc = LocalLLMService()
    except Exception as e:
        print("Failed to initialize LocalLLMService:", e)
        return

    print("Running health_check()...")
    h = svc.health_check()
    print(h)

    if not h.get("ok"):
        print("Health check failed; warmup removed, inspect logs and try again.")
    else:
        print("Health ok; warmup removed, ready to use.")


if __name__ == "__main__":
    main()
