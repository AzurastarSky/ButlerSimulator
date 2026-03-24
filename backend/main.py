from fastapi import FastAPI, HTTPException, Request
import asyncio
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import os, threading, time, logging
from dotenv import load_dotenv
from openai import OpenAI
from fastapi.middleware.cors import CORSMiddleware

# Load backend/.env (local LLM config) before importing providers that read env
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=ENV_PATH, override=True)

from backend.providers.model_manager import get_manager
from backend.services.local_llm_service import LocalLLMService

app = FastAPI()
# Enable CORS for local testing (allow all origins temporarily)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
manager = get_manager()
local_llm = LocalLLMService()

# store state in-memory for now (simple)
LLM_PID = None

class LLMStartRequest(BaseModel):
    # keep minimal: supply the command from client for now
    command: str

class ChatLocalRequest(BaseModel):
    text: str


class ChatRequest(BaseModel):
    message: str
    provider: str = "cloud"  # "cloud" or "local"
    model: str | None = None


class LLMSwitchRequest(BaseModel):
    # Full shell command to run on the remote board to start the rkllm server
    command: str
    # Optional model id to set locally for the OpenAI-compatible client
    model_id: str | None = None


class ModelStartRequest(BaseModel):
    model_name: str

@app.post("/api/llm/start")
def llm_start(req: LLMStartRequest):
    global LLM_PID
    try:
        # Kick off start in a background thread so the API call returns quickly
        def _start_cmd():
            try:
                manager.cleanup_all()
            except Exception:
                pass
            try:
                pid_str = manager._exec_background_command(req.command)
                pid = int(pid_str) if pid_str and pid_str.isdigit() else None
                # store pid in memory (best-effort)
                global LLM_PID
                LLM_PID = pid
            except Exception as e:
                logging.error(f"Background start command failed: {e}")

        threading.Thread(target=_start_cmd, daemon=True).start()
        return {"ok": True, "starting": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/llm/start_model")
def llm_start_model(req: ModelStartRequest):
    """Start a named model from the predefined MODEL_CONFIGS on the remote board."""
    global LLM_PID
    try:
        # Start model asynchronously via manager
        manager.start_model_async(req.model_name)
        # Update local client to target the model name (used by OpenAI-compatible API)
        try:
            local_llm.model_id = req.model_name
        except Exception:
            pass
        return {"ok": True, "starting": True, "model": req.model_name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/api/llm/cleanup")
def llm_cleanup():
    manager.cleanup_all()
    return {"ok": True}

@app.post("/api/llm/stop")
def llm_stop():
    global LLM_PID
    if not LLM_PID:
        return {"ok": True, "note": "no known pid"}
    try:
        manager.stop_current_model()
        LLM_PID = None
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/api/chat_local")
def chat_local(req: ChatLocalRequest):
    try:
        # Stream-only UI: provide a simple non-stream fallback if needed
        answer = local_llm.chat(req.text)
        return {"answer": answer, "model": local_llm.model_id, "base_url": local_llm.base_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Note: non-streaming `/api/chat` endpoint removed — UI is stream-only.
# Keep `chat_local` as a minimal fallback and use `/api/chat/stream` for streaming responses.


@app.post("/api/chat/stream")
async def chat_stream(request: Request):
    """Stream chat responses from the local provider as server-sent events.

    This endpoint expects a JSON body: { "message": "..." }
    It returns `text/event-stream` where each `data:` line is a partial
    token chunk from the model.
    """
    # Read the body in the async context first
    try:
        payload = await request.json()
        message = payload.get('message')
        if not message:
            return StreamingResponse(("data: [ERROR] missing message\n\n" for _ in [1]), media_type='text/event-stream')
    except Exception:
        return StreamingResponse(("data: [ERROR] invalid body\n\n" for _ in [1]), media_type='text/event-stream')

    # Ensure local client is configured with a model id
    if not getattr(local_llm, 'model_id', None):
        return StreamingResponse(("data: [ERROR] RKLLM_MODEL_ID not configured on server; set backend/.env and restart\n\n" for _ in [1]), media_type='text/event-stream')

    # Quick check if server is reachable before attempting to stream
    try:
        import requests
        board_ip = os.getenv("LOCAL_BOARD_IP", "192.168.0.222")
        board_port = os.getenv("LOCAL_BOARD_PORT", "8080")
        check_url = f"http://{board_ip}:{board_port}/v1/models"
        requests.get(check_url, timeout=2)
    except Exception as e:
        err_msg = f"RKLLM server not reachable at {board_ip}:{board_port} - is the model started? Error: {e}"
        logging.error(err_msg)
        return StreamingResponse((f"data: [ERROR] {err_msg}\n\n" for _ in [1]), media_type='text/event-stream')

    # Define an async generator for proper FastAPI streaming
    async def event_generator():
        try:
            for chunk in local_llm.stream_chat(message):
                safe = chunk.replace('\n', '\\n')
                logging.info(f"SSE emit chunk: {safe[:50]}...")
                yield f"data: {safe}\n\n"
                await asyncio.sleep(0)  # Allow other async tasks to run
        except Exception as e:
            logging.error(f"Stream error: {e}")
            yield f"data: [STREAM_ERROR] {str(e)}\n\n"
        yield "event: done\ndata: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type='text/event-stream')


@app.get("/api/stream/test")
def stream_test():
    """Simple SSE test endpoint that emits a few test chunks.

    Use this to confirm the backend streaming path works independently
    of the RKLLM provider.
    """
    async def gen():
        for i in range(1, 6):
            yield f"data: test chunk {i}\n\n"
            await asyncio.sleep(0.5)
        yield "event: done\ndata: [DONE]\n\n"

    return StreamingResponse(gen(), media_type='text/event-stream')


# `/api/llm/warmup` endpoint removed — warmup behavior disabled per user request.


def _background_start_llm(cmd: str, timeout: int = 120):
    """Start RKLLM server via SSH in background.

    This is run in a daemon thread to avoid blocking FastAPI startup.
    """
    global LLM_PID
    try:
        logging.info("Cleaning up existing RKLLM servers before start...")
        manager.cleanup_all()
    except Exception as e:
        logging.warning(f"Cleanup before start failed: {e}")

    try:
        # Start background command via manager
        pid_str = manager._exec_background_command(cmd)
        pid = int(pid_str) if pid_str and pid_str.isdigit() else None
        LLM_PID = pid
        logging.info(f"Started RKLLM server with PID {pid} (raw:{pid_str})")

        # Ensure local_llm knows which model name to query for streaming.
        env_model_name = os.getenv("RKLLM_MODEL_NAME", "").strip()
        env_model_id = os.getenv("RKLLM_MODEL_ID", "").strip()
        if env_model_name:
            local_llm.model_id = env_model_name
        elif env_model_id:
            local_llm.model_id = os.path.splitext(env_model_id)[0]
        # write autostart log
        try:
            with open(os.path.join(os.path.dirname(__file__), "auto_start.log"), "a", encoding="utf-8") as f:
                f.write(f"STARTED PID={pid} CMD={cmd}\n")
        except Exception:
            pass
    except Exception as e:
        logging.error(f"Failed to start RKLLM server: {e}")
        try:
            with open(os.path.join(os.path.dirname(__file__), "auto_start.log"), "a", encoding="utf-8") as f:
                f.write(f"START_FAILED: {e}\n")
        except Exception:
            pass
        return

@app.on_event("startup")
def maybe_autostart_local_llm():
    """On startup, optionally start the local RKLLM server if configured.
    
    If RKLLM_MODEL_NAME is set, the model will be started and warmed up
    automatically with a simple 'Hello' prompt so it's ready when users connect.
    """
    try:
        # AUTO_START_LOCAL_LLM controls whether we attempt to start via a raw start command.
        # If a model name is configured via RKLLM_MODEL_NAME we will start that model
        # and warm it with a 'Hello' prompt so it's fully loaded and ready before
        # users connect to the webpage.
        auto = os.getenv("AUTO_START_LOCAL_LLM", "false").lower() in ("1", "true", "yes")
        start_cmd = os.getenv("RKLLM_START_CMD", "").strip()

        # If a raw start command is configured and autostart is enabled, run it.
        if auto and start_cmd:
            logging.info("AUTO_START_LOCAL_LLM enabled; starting RKLLM in background (custom cmd)")
            try:
                with open(os.path.join(os.path.dirname(__file__), "auto_start.log"), "a", encoding="utf-8") as f:
                    f.write(f"AUTOSTART requested: {start_cmd}\n")
            except Exception:
                pass
            threading.Thread(target=_background_start_llm, args=(start_cmd,), daemon=True).start()

        # If a specific model name is configured, attempt to start it via
        # the manager so the model is available before clients connect.
        model_name = os.getenv("RKLLM_MODEL_NAME", "").strip()
        if model_name:
            logging.info(f"Preloading configured model on startup: {model_name}")
            try:
                manager.start_model_async(model_name)
                try:
                    local_llm.model_id = model_name
                except Exception:
                    pass
                try:
                    with open(os.path.join(os.path.dirname(__file__), "auto_start.log"), "a", encoding="utf-8") as f:
                        f.write(f"AUTOSTART model requested: {model_name}\n")
                except Exception:
                    pass
                
                # Warm up the model with a simple "Hello" prompt so it's ready when users connect
                def _warmup_after_start():
                    # Poll for server to be ready before warmup (max 60 seconds)
                    import requests
                    board_ip = os.getenv("LOCAL_BOARD_IP", "192.168.0.222")
                    board_port = os.getenv("LOCAL_BOARD_PORT", "8080")
                    check_url = f"http://{board_ip}:{board_port}/v1/models"
                    
                    logging.info("Waiting for rkllm3-server to be ready before warmup...")
                    for attempt in range(60):
                        time.sleep(1)
                        try:
                            resp = requests.get(check_url, timeout=2)
                            if resp.status_code == 200:
                                logging.info(f"✓ Server is ready after {attempt+1} seconds")
                                break
                        except Exception:
                            if attempt % 5 == 0:
                                logging.info(f"Still waiting for server... ({attempt+1}s)")
                    else:
                        logging.warning("Server did not become ready in 60s, skipping warmup")
                        return
                    
                    try:
                        if local_llm.warmup("Hello"):
                            logging.info("✓ Model warmed up and ready for use")
                        else:
                            logging.warning("Warmup returned False - check logs")
                    except Exception as e:
                        logging.error(f"Startup warmup failed: {e}")
                
                threading.Thread(target=_warmup_after_start, daemon=True).start()
            except Exception as e:
                logging.error(f"Failed to preload model {model_name} on startup: {e}")
        else:
            logging.info("No RKLLM_MODEL_NAME configured; skipping model preload")
    except Exception as e:
        logging.error(f"Error in startup autostart handler: {e}")


@app.get("/api/llm/autostart_log")
def get_autostart_log():
    """Return the autostart log contents for debugging startup attempts."""
    p = os.path.join(os.path.dirname(__file__), "auto_start.log")
    try:
        if not os.path.exists(p):
            return {"ok": True, "log": "(no autostart log)"}
        with open(p, "r", encoding="utf-8") as f:
            return {"ok": True, "log": f.read()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.on_event("shutdown")
def shutdown_cleanup():
    """Cleanup RKLLM servers and close SSH connections on shutdown."""
    logging.info("Shutdown: cleaning up RKLLM servers and closing connections")
    try:
        try:
            manager.cleanup_all()
        except Exception as e:
            logging.warning(f"manager.cleanup_all() failed: {e}")
        # Reset PID tracking
        global LLM_PID
        LLM_PID = None
    except Exception as e:
        logging.error(f"Error during shutdown cleanup: {e}")


@app.post("/api/llm/switch")
def llm_switch(req: LLMSwitchRequest):
    """Switch RKLLM server by running the provided start command (remote SSH).

    This will attempt to clean up existing RKLLM servers, start the new one,
    optionally update the `local_llm.model_id`, and warm up the model.
    """
    global LLM_PID
    try:
        # Stop existing servers
        manager.cleanup_all()

        # If a model name was provided, start via manager; otherwise start the raw command
        if req.model_id:
            manager.start_model_async(req.model_id)
            try:
                local_llm.model_id = req.model_id
            except Exception:
                pass
            return {"ok": True, "starting_model": req.model_id}

        pid_str = manager._exec_background_command(req.command)
        pid = int(pid_str) if pid_str and pid_str.isdigit() else None
        LLM_PID = pid
        return {"ok": True, "pid": pid}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/llm/status")
def llm_status():
    global LLM_PID
    current = manager.get_current_model()
    
    # Check if server is actually reachable
    server_reachable = False
    try:
        import requests
        board_ip = os.getenv("LOCAL_BOARD_IP", "192.168.0.222")
        board_port = os.getenv("LOCAL_BOARD_PORT", "8080")
        check_url = f"http://{board_ip}:{board_port}/v1/models"
        resp = requests.get(check_url, timeout=2)
        server_reachable = (resp.status_code == 200)
    except Exception as e:
        server_reachable = False
    
    if not current:
        # Try to fetch some logs for debugging
        try:
            out, _, _ = manager._exec_command("tail -20 /tmp/rkllm_server.log", timeout=3)
        except Exception:
            out = "(no remote log available)"
        return {
            "running": False, 
            "pid": None, 
            "model": None, 
            "server_reachable": server_reachable,
            "log_tail": out, 
            "last_error": manager.last_error
        }

    return {
        "running": True, 
        "pid": manager.server_pid, 
        "model": current, 
        "server_reachable": server_reachable,
        "last_error": manager.last_error
    }

