import os
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI
import logging
import requests

# Explicitly load backend/.env (since your .env is in backend)
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(dotenv_path=str(ENV_PATH), override=True)

class LocalLLMService:
    def __init__(self):
        ip = os.getenv("LOCAL_BOARD_IP", "192.168.0.222")
        port = os.getenv("LOCAL_BOARD_PORT", "8080")
        raw_model = os.getenv("RKLLM_MODEL_ID", "").strip()

        # Normalize model id: accept either a model name (e.g. Qwen2.5-3B-Instruct)
        # or a model file (e.g. Qwen2.5-3B-Instruct.rknn). If a file is provided,
        # strip the extension so the OpenAI-compatible API is addressed by model name.
        if raw_model:
            self.model_id = os.path.splitext(raw_model)[0]
        else:
            # Don't raise at import time if RKLLM_MODEL_ID is not set; allow
            # the server to start and let endpoints handle missing model id.
            self.model_id = None

        self.base_url = f"http://{ip}:{port}/v1/"
        # Create the OpenAI-compatible client regardless of model_id so health
        # checks and warmups can still attempt connections to the board.
        self.client = OpenAI(
            base_url=self.base_url,
            api_key="sk-no-key-required",
            timeout=60.0
        )

    def health_check(self) -> dict:
        """Check if local LLM server is reachable and the configured model responds.

        Returns a dict: {"ok": bool, "detail": str}
        """
        try:
            # Quick ping using the OpenAI-compatible models list if available
            # Some local servers expose /v1/models; fall back to a short chat if not
            try:
                resp = self.client.models.list()
                models = [m.id for m in getattr(resp, "data", [])]
                return {"ok": True, "detail": f"models:{models}"}
            except Exception:
                # Try a minimal chat to verify the model responds
                if not self.model_id:
                    return {"ok": False, "detail": "RKLLM_MODEL_ID not configured"}
                resp = self.client.chat.completions.create(
                    model=self.model_id,
                    messages=[{"role": "system", "content": "healthcheck"},
                              {"role": "user", "content": "ping"}],
                    max_tokens=1,
                    temperature=0.0,
                    stream=False,
                    timeout=10.0,
                )
                return {"ok": True, "detail": "chat_ok"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    # Warmup helper removed: explicit warmup requests are disabled.

    def chat(self, user_text: str, system_text: str = "You are a helpful home butler.") -> str:
        resp = self.client.chat.completions.create(
            model=self.model_id,
            messages=[
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
            max_tokens=200,
            temperature=0.2,
            stream=False,
        )
        return resp.choices[0].message.content or ""

    def stream_chat(self, user_text: str, system_text: str = "You are a helpful home butler."):
        """Stream incremental token deltas from the local OpenAI-compatible LLM.

        Yields unicode text chunks (partial content) as they arrive.
        """
        if not self.model_id:
            raise RuntimeError("RKLLM_MODEL_ID not set")
        
        try:
            logging.info(f"stream_chat: creating streaming request for model={self.model_id}")
            resp = self.client.chat.completions.create(
                model=self.model_id,
                messages=[
                    {"role": "system", "content": system_text},
                    {"role": "user", "content": user_text},
                ],
                max_tokens=200,
                temperature=0.2,
                stream=True,
            )

            chunk_count = 0
            for event in resp:
                if hasattr(event, 'choices') and event.choices:
                    for choice in event.choices:
                        if hasattr(choice, 'delta') and hasattr(choice.delta, 'content'):
                            content = choice.delta.content
                            if content:
                                chunk_count += 1
                                if chunk_count <= 3:  # Log first few chunks
                                    logging.info(f"stream_chat: yielding chunk #{chunk_count}: {repr(content)[:50]}")
                                yield content
            
            logging.info(f"stream_chat: completed, yielded {chunk_count} chunks total")
        except Exception as e:
            logging.error(f"stream_chat error: {e}", exc_info=True)
            yield f"[STREAM_ERROR] {e}"

    def warmup(self, warmup_text: str = "Hello"):
        """Send a simple warmup prompt to preload the model into memory.
        
        This should be called after starting the model to ensure it's ready.
        Returns True if successful, False otherwise.
        """
        if not self.model_id:
            logging.warning("warmup: RKLLM_MODEL_ID not set, skipping")
            return False
        
        # First check if the server is even reachable
        try:
            import requests
            health_url = self.base_url.replace('/v1/', '/health')
            requests.get(health_url, timeout=2)
        except Exception:
            logging.warning(f"Warmup skipped: server at {self.base_url} not reachable yet")
            return False
        
        try:
            logging.info(f"Warming up model {self.model_id} with prompt: {warmup_text}")
            resp = self.client.chat.completions.create(
                model=self.model_id,
                messages=[{"role": "user", "content": warmup_text}],
                max_tokens=10,
                temperature=0.1,
                stream=False,
                timeout=30.0,
            )
            result = resp.choices[0].message.content or ""
            logging.info(f"✓ Warmup complete, got response: {result[:50]}...")
            return True
        except Exception as e:
            logging.error(f"Warmup failed: {e}")
            return False