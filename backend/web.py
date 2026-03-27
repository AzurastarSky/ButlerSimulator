# backend/web.py
from flask import Flask, send_from_directory, request, jsonify, Response, stream_with_context
import base64
import re
from pathlib import Path
import time
import os
import json
import requests
import concurrent.futures
import copy
import queue
import threading
import uuid

def sse_format(event: str, data: dict) -> str:
    """Format a Server-Sent Event (SSE) message with JSON data."""
    try:
        payload = json.dumps(data)
    except Exception:
        payload = json.dumps({'error': 'failed to serialize event data'})
    return f"event: {event}\ndata: {payload}\n\n"

app = Flask(__name__, static_folder="../frontend", static_url_path="")
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# ---------------- In-memory State ----------------
# Rooms: lights only (blinds removed)
STATE = {
    # Downstairs
    "living room": {"light": "off"},
    "dining room": {"light": "off"},
    "kitchen":     {"light": "off"},
    # Upstairs
    "bathroom": {"light": "off"},
    "bedroom":  {"light": "off"},
    "office":   {"light": "off"},
}

# House thermostat (single, house-level)
HOUSE = {
    "target": 20.0,   # °C
    "mode": "heat",   # "heat" | "off"
}
# Duplicate per-model state (local vs cloud) so we can simulate two independent houses
STATE_LOCAL = copy.deepcopy(STATE)
STATE_CLOUD = copy.deepcopy(STATE)
HOUSE_LOCAL = copy.deepcopy(HOUSE)
HOUSE_CLOUD = copy.deepcopy(HOUSE)
AMBIENT = 18.0               # °C when heating is off, drift target
HEAT_RATE_C_PER_SEC = 0.02   # how fast temperature approaches target when heating
COOL_RATE_C_PER_SEC = 0.01   # how fast temperature moves toward ambient when off
LAST_UPDATE = time.time()
LAST_UPDATE_LOCAL = time.time()
LAST_UPDATE_CLOUD = time.time()

# Simple pubsub for state change events (SSE)
_state_subscribers = []
_state_sub_lock = threading.Lock()

def _current_state_snapshot():
    return {
        "house": HOUSE,
        "local": {"house": HOUSE_LOCAL, "rooms": STATE_LOCAL},
        "cloud": {"house": HOUSE_CLOUD, "rooms": STATE_CLOUD},
    }

def publish_state_event():
    payload = json.dumps(_current_state_snapshot())
    with _state_sub_lock:
        subs = list(_state_subscribers)
    for q in subs:
        try:
            q.put_nowait(payload)
        except Exception:
            pass


# Background loop to update temperatures and publish periodic state events
_state_publisher_started = False

def _state_publisher_loop(poll_interval: float = 1.0):
    while True:
        try:
            # advance simulated temps
            try:
                _update_house_temp_local()
            except Exception:
                pass
            try:
                _update_house_temp_cloud()
            except Exception:
                pass
            try:
                _update_house_temp()
            except Exception:
                pass
            # publish snapshot so clients see changing temps
            try:
                publish_state_event()
            except Exception:
                pass
        except Exception:
            pass
        time.sleep(poll_interval)

def ensure_state_publisher():
    global _state_publisher_started
    if _state_publisher_started:
        return
    _state_publisher_started = True
    t = threading.Thread(target=_state_publisher_loop, args=(1.0,), daemon=True)
    t.start()


# Start background state publisher immediately
try:
    ensure_state_publisher()
except Exception:
    pass

VALID_LIGHT_ACTIONS  = {"turn_on", "turn_off", "toggle"}
VALID_THERMO_ACTIONS = {"increase", "decrease", "set_value", "turn_on", "turn_off"}

DOWNSTAIRS = {"living room", "dining room", "kitchen"}
UPSTAIRS   = {"bathroom", "bedroom", "office"}

try:
    from .helpers import ROOM_SYNONYMS, DEVICE_SYNONYMS, norm_room, norm_device, VALID_ROOMS, VALID_DEVICES, clamp
except Exception:
    try:
        from helpers import ROOM_SYNONYMS, DEVICE_SYNONYMS, norm_room, norm_device, VALID_ROOMS, VALID_DEVICES, clamp
    except Exception:
        ROOM_SYNONYMS = {}
        DEVICE_SYNONYMS = {}
        def norm_room(v: str) -> str: return (v or "").strip().lower()
        def norm_device(v: str) -> str: return (v or "").strip().lower()
        VALID_ROOMS = set()
        VALID_DEVICES = set()
        def clamp(v: float, lo: float, hi: float) -> float: return max(lo, min(hi, v))

# OpenAI config (optional)
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_KEY = os.environ.get("OPENAI_API_KEY")

# Reuse local CLI helper where convenient
try:
    from . import llm_toolcall_test as local_llm
except Exception:
    try:
        import llm_toolcall_test as local_llm
    except Exception:
        local_llm = None

# Import parsing/helper utilities even if local LLM instance isn't available
try:
    from . import llm_toolcall_test as llm_helper
except Exception:
    try:
        import llm_toolcall_test as llm_helper
    except Exception:
        llm_helper = None

# Optional local STT provider (SenseVoice)
try:
    from .providers import local_sensevoice_stt as stt_provider
except Exception:
    try:
        from providers import local_sensevoice_stt as stt_provider
    except Exception as e:
        stt_provider = None
        print(f"[web] local STT provider not available: {e}")

# Optional local TTS provider (Paroli)
try:
    from .providers import local_paroli_tts as local_tts_provider
except Exception:
    try:
        from providers import local_paroli_tts as local_tts_provider
    except Exception as e:
        local_tts_provider = None
        print(f"[web] local Paroli TTS provider not available: {e}")

# Optional cloud TTS provider (OpenAI tts-1)
try:
    from .providers import openai_tts as cloud_tts_provider
except Exception:
    try:
        from providers import openai_tts as cloud_tts_provider
    except Exception as e:
        cloud_tts_provider = None
        print(f"[web] cloud TTS provider not available: {e}")

# Directory to persist TTS audio files
TTS_OUTPUT_DIR = Path(__file__).resolve().parent / "tts_output"
_TTS_JOB_LOCK = threading.Lock()
# JOBS: job_id -> {text, local: bytes|None, cloud: bytes|None, status}
JOBS = {}

def _ensure_tts_output_dir():
    try:
        TTS_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

def _save_job_audio(job_id: str, source: str, data: bytes) -> str:
    """Save bytes to disk under TTS_OUTPUT_DIR and return relative path."""
    _ensure_tts_output_dir()
    fname = f"{job_id}_{source}.mp3"
    outp = TTS_OUTPUT_DIR / fname
    try:
        with open(outp, "wb") as f:
            f.write(data)
    except Exception:
        pass
    return str(outp)

def _save_chunk_audio(job_id: str, chunk_idx: int, source: str, data: bytes) -> str:
    _ensure_tts_output_dir()
    fname = f"{job_id}_chunk{chunk_idx}_{source}.mp3"
    outp = TTS_OUTPUT_DIR / fname
    try:
        with open(outp, 'wb') as f:
            f.write(data)
    except Exception:
        pass
    return str(outp)


def _clear_persisted_chunks():
    """Delete persisted chunk files from disk and clear 'chunks' lists in JOBS.

    This is called when a new TTS streaming request starts so that chunk
    artifacts from previous sessions don't accumulate indefinitely. It only
    removes per-sentence chunk files (files with '_chunk' in their name) and
    clears the corresponding 'chunks' metadata in the in-memory JOBS store.
    """
    try:
        # Determine active job ids to avoid deleting chunks for ongoing jobs
        with _TTS_JOB_LOCK:
            active_jobs = set(JOBS.keys())

        # Delete files on disk matching chunk naming pattern only if their
        # job_id (prefix before first '_') is not in active_jobs.
        if TTS_OUTPUT_DIR.exists():
            for p in TTS_OUTPUT_DIR.iterdir():
                try:
                    if '_chunk' not in p.name:
                        continue
                    # Expect filenames like '<job_id>_chunk{idx}_{source}.mp3'
                    parts = p.name.split('_')
                    if not parts:
                        continue
                    job_id_in_name = parts[0]
                    # If the job_id is not currently tracked in JOBS, it's safe to delete
                    if job_id_in_name and (job_id_in_name not in active_jobs):
                        p.unlink()
                except Exception:
                    pass

        # Clear 'chunks' metadata for jobs that no longer exist in JOBS (defensive)
        with _TTS_JOB_LOCK:
            for jid, meta in list(JOBS.items()):
                if not isinstance(meta, dict):
                    continue
                # Keep 'chunks' for active jobs; remove stale empty entries
                if 'chunks' in meta and not meta.get('chunks'):
                    try:
                        meta.pop('chunks', None)
                        JOBS[jid] = meta
                    except Exception:
                        pass
    except Exception:
        pass


def _delete_job_files(job_id: str) -> dict:
    """Delete all persisted files (chunks and final) for a specific job_id.

    Returns a summary dict containing counts of deleted files and any errors encountered.
    """
    summary = {'deleted': [], 'errors': []}
    try:
        if not job_id:
            summary['errors'].append('no job_id')
            return summary

        # Delete files on disk that start with the job_id prefix
        if TTS_OUTPUT_DIR.exists():
            for p in TTS_OUTPUT_DIR.iterdir():
                try:
                    if p.name.startswith(job_id + '_'):
                        p.unlink()
                        summary['deleted'].append(str(p))
                except Exception as e:
                    summary['errors'].append(f"{p}: {e}")

        # Remove job entry from JOBS under lock
        with _TTS_JOB_LOCK:
            if job_id in JOBS:
                try:
                    JOBS.pop(job_id, None)
                except Exception as e:
                    summary['errors'].append(f"jobs_pop_error: {e}")
    except Exception as e:
        summary['errors'].append(str(e))
    return summary


def _purge_old_job_files(except_job_id: str = None) -> dict:
    """Delete persisted audio files for all jobs except `except_job_id`.

    Returns a summary with lists of deleted files and errors. Also removes
    corresponding JOBS entries for deleted job ids.
    """
    summary = {'deleted': [], 'errors': []}
    try:
        # Preserve only the explicit exception job id; delete all other jobs/files.
        preserved = set()
        if except_job_id:
            preserved.add(except_job_id)

        # snapshot existing jobs so we can remove their entries after deleting files
        with _TTS_JOB_LOCK:
            existing_jobs = list(JOBS.keys())

        # Delete files on disk that belong to jobs not in `preserved`.
        # Only delete files for jobs that are not currently pending (to avoid
        # removing a slow provider's output while another provider already
        # completed). We consider a job "in-flight" if its 'status' dict
        # contains any provider marked as 'pending'.
        #
        # Snapshot JOBS statuses for decision-making.
        job_status_snapshot = {}
        with _TTS_JOB_LOCK:
            for jid, meta in JOBS.items():
                try:
                    job_status_snapshot[jid] = dict(meta.get('status', {}))
                except Exception:
                    job_status_snapshot[jid] = {}

        if TTS_OUTPUT_DIR.exists():
            for p in list(TTS_OUTPUT_DIR.iterdir()):
                try:
                    if '_' not in p.name:
                        continue
                    job_id_in_name = p.name.split('_', 1)[0]
                    if not job_id_in_name:
                        continue
                    if job_id_in_name in preserved:
                        continue
                    # If we have a status snapshot and the job shows any 'pending', skip deletion
                    sts = job_status_snapshot.get(job_id_in_name, {})
                    if any(v == 'pending' for v in sts.values()):
                        continue
                    # safe to delete
                    p.unlink()
                    summary['deleted'].append(str(p))
                except Exception as e:
                    summary['errors'].append(f"{p}: {e}")

        # Remove JOBS entries for previously existing jobs that are not preserved
        # and that are not in-flight (no 'pending' statuses). Keep any in-flight
        # jobs to avoid disrupting ongoing syntheses.
        with _TTS_JOB_LOCK:
            for jid in existing_jobs:
                if jid in preserved:
                    continue
                sts = job_status_snapshot.get(jid, {})
                if any(v == 'pending' for v in sts.values()):
                    # Skip removing in-flight job
                    continue
                try:
                    JOBS.pop(jid, None)
                except Exception as e:
                    summary['errors'].append(f"jobs_pop_{jid}: {e}")
    except Exception as e:
        summary['errors'].append(str(e))
    return summary

def _split_into_chunks(text: str, min_tokens: int = 5, max_chunks: int = 3):
    """Split text on sentence-ending punctuation but avoid creating too many chunks.

    Rules applied:
    - If text is short (<=32 tokens) return a single chunk.
    - If punctuation is sparse (<=1 sentence ender), return a single chunk.
    - Otherwise split on sentence-ending punctuation, merge very small pieces to meet min_tokens,
      and cap the number of chunks to `max_chunks` by merging tail chunks.
    """
    if not text:
        return []
    import re
    toks = text.strip().split()
    total_tokens = len(toks)
    # Short input -> single chunk (fast path)
    if total_tokens <= 120:
        return [text.strip()]

    # If very few punctuation markers, keep single chunk to avoid overhead
    sent_enders = re.findall(r'[\.\!\?;:]', text)
    if len(sent_enders) <= 1:
        return [text.strip()]

    # Split on sentence-ending punctuation
    parts = re.split(r'(?<=[\.\!\?;:])\s+', text.strip())
    # Merge small pieces to ensure min_tokens
    merged = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p.split()) >= min_tokens:
            merged.append(p)
        else:
            if merged:
                merged[-1] = (merged[-1] + ' ' + p).strip()
            else:
                merged.append(p)

    # Ensure all chunks meet min_tokens by merging forward
    i = 0
    while i < len(merged):
        if len(merged[i].split()) < min_tokens and i+1 < len(merged):
            merged[i] = (merged[i] + ' ' + merged[i+1]).strip()
            merged.pop(i+1)
            continue
        i += 1

    # Cap the number of chunks to max_chunks by merging from the end
    while len(merged) > max_chunks:
        # merge last into second-last
        merged[-2] = (merged[-2] + ' ' + merged[-1]).strip()
        merged.pop()

    return merged


_SENT_RE = re.compile(r'([\.\!\?;:]+)(\s+|$)')
def _extract_sentences(buf: str):
    """Extract complete sentences from text using sentence-ending punctuation.
    Returns (sentences, remainder)
    """
    sentences = []
    start = 0
    for m in _SENT_RE.finditer(buf):
        end = m.end()
        s = buf[start:end].strip()
        if s:
            sentences.append(s)
        start = end
    return sentences, buf[start:]

def _start_tts_background(job_id: str, text: str, voice: str = None):
    """Run local and cloud TTS in background and populate JOBS entry."""
    def work():
        def run_provider(src, provider):
            t0 = time.time()
            data = None
            print(f"[TTS BG] starting provider={src} for job={job_id} text_len={len(text)}")
            try:
                data = provider.synthesize_speech(text, voice=voice)
                print(f"[TTS BG] provider={src} completed synthesis for job={job_id} bytes={len(data) if data else 0}")
            except Exception as e:
                data = None
                try:
                    print(f"[TTS BG] provider={src} failed for job={job_id}: {repr(e)}")
                except Exception:
                    pass
            t1 = time.time()
            dur_ms = int((t1 - t0) * 1000)
            with _TTS_JOB_LOCK:
                job = JOBS.get(job_id) or {}
                job.setdefault('text', text)
                job.setdefault('created_at', time.time())
                job.setdefault('status', {})
                job.setdefault('timings', {})
                job['timings'][src] = {'start_ms': int(t0*1000), 'end_ms': int(t1*1000), 'duration_ms': dur_ms}
                if data:
                    job[src] = data
                    try:
                        path = _save_job_audio(job_id, src, data)
                        job[f"{src}_path"] = path
                        # Also store the raw bytes for diagnostics
                        try:
                            job[f"{src}_bytes"] = data
                        except Exception:
                            pass
                        try:
                            size = os.path.getsize(path) if os.path.exists(path) else None
                            print(f"[TTS BG] saved job={job_id} src={src} path={path} size={size}")
                        except Exception:
                            pass
                    except Exception:
                        try:
                            print(f"[TTS BG] failed to save job={job_id} src={src}")
                        except Exception:
                            pass
                    job['status'][src] = 'done'
                else:
                    job[src] = None
                    job['status'][src] = 'failed'
                JOBS[job_id] = job

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            futures = {}
            if local_tts_provider:
                futures[ex.submit(run_provider, 'local', local_tts_provider)] = 'local'
            if cloud_tts_provider:
                futures[ex.submit(run_provider, 'cloud', cloud_tts_provider)] = 'cloud'
            for fut in concurrent.futures.as_completed(futures):
                _ = futures.get(fut)

    t = threading.Thread(target=work, daemon=True)
    t.start()

 # Normalizers and clamp are provided by `helpers.py` import above.

def _update_house_temp():
    # No longer tracking a simulated 'current' temperature.
    # Keep the timestamp so periodic publisher remains functional.
    global LAST_UPDATE
    LAST_UPDATE = time.time()


def _update_house_temp_local():
    # No longer tracking a simulated 'current' temperature for local store.
    global LAST_UPDATE_LOCAL
    LAST_UPDATE_LOCAL = time.time()


def _update_house_temp_cloud():
    # No longer tracking a simulated 'current' temperature for cloud store.
    global LAST_UPDATE_CLOUD
    LAST_UPDATE_CLOUD = time.time()

# ---------------- Static routes ----------------
@app.get("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")

@app.get("/styles.css")
def styles_css():
    return send_from_directory(FRONTEND_DIR, "styles.css")

@app.get("/app.js")
def app_js():
    return send_from_directory(FRONTEND_DIR, "app.js")

# ---------------- API ----------------
@app.get("/api/state")
def get_state():
    # Update the appropriate house temps when returning state so UI sees drifting temps
    which = (request.args.get('which') or '').lower()
    if which == 'local':
        _update_house_temp_local()
    elif which == 'cloud':
        _update_house_temp_cloud()
    else:
        _update_house_temp()
    # Return structure with house + rooms. Query param 'which' selects local/cloud/default
    which = (request.args.get('which') or '').lower()
    if which == 'local':
        return jsonify({"house": HOUSE_LOCAL, "rooms": STATE_LOCAL})
    if which == 'cloud':
        return jsonify({"house": HOUSE_CLOUD, "rooms": STATE_CLOUD})
    return jsonify({"house": HOUSE, "rooms": STATE})

@app.post("/api/device")
def device():
    data = request.get_json(force=True) or {}
    room = norm_room(data.get("room", ""))
    device = norm_device(data.get("device", ""))
    action = (data.get("action", "") or "").strip().lower()
    value  = data.get("value", None)

    # ------- Thermostat (house-level) -------
    if device == "thermostat":
        if action not in VALID_THERMO_ACTIONS:
            return jsonify({"ok": False, "error": f"Unknown thermostat action: {action}"}), 400

        step = 1.0
        if value is not None:
            try:
                step = float(value)
            except:
                pass

        if action == "increase":
            HOUSE["target"] = clamp(HOUSE["target"] + step, 10.0, 28.0)
        elif action == "decrease":
            HOUSE["target"] = clamp(HOUSE["target"] - step, 10.0, 28.0)
        elif action == "set_value":
            try:
                num = float(value)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "set_value requires numeric 'value'"}), 400
            HOUSE["target"] = clamp(num, 10.0, 28.0)
        elif action == "turn_on":
            HOUSE["mode"] = "heat"
        elif action == "turn_off":
            HOUSE["mode"] = "off"

        # Mirror thermostat change to local/cloud houses
        HOUSE_LOCAL["target"] = HOUSE["target"]
        HOUSE_CLOUD["target"] = HOUSE["target"]
        HOUSE_LOCAL["mode"] = HOUSE["mode"]
        HOUSE_CLOUD["mode"] = HOUSE["mode"]

        _update_house_temp()
        # notify listeners
        try:
            publish_state_event()
        except Exception:
            pass
        return jsonify({"ok": True, "device": "thermostat", "action": action, "house": HOUSE})

    # ------- Lights (room or scoped) -------
    if device not in {"light"}:
        return jsonify({"ok": False, "error": f"Unsupported device: {device}"}), 400

    # Allow helper autocorrections (maps brightness/intents and increase/decrease for lights)
    try:
        if 'llm_helper' in globals() and llm_helper and hasattr(llm_helper, 'try_autocorrect'):
            try:
                corrected = llm_helper.try_autocorrect(data, None)
                # update local variables from corrected map
                device = norm_device(corrected.get('device', device))
                action = (corrected.get('action', action) or '').strip().lower()
                value = corrected.get('value', value)
            except Exception:
                pass
        else:
            # fallback: map light increase/decrease -> turn_on/turn_off
            if device == 'light' and action in {'increase', 'decrease'}:
                action = 'turn_on' if action == 'increase' else 'turn_off'
    except Exception:
        pass

    valid_actions = VALID_LIGHT_ACTIONS
    if action not in valid_actions:
        return jsonify({"ok": False, "error": f"Unknown action: {action}"}), 400

    # Scope operations
    if room in {"all", "upstairs", "downstairs"}:
        targets = list(STATE.keys()) if room == "all" else \
                  [r for r in STATE if (r in UPSTAIRS if room == "upstairs" else r in DOWNSTAIRS)]
        applied, skipped = [], []
        for r in targets:
            if device not in STATE[r]:
                skipped.append(r)
                continue
            cur = STATE[r][device]
            new_state = ("on" if action == "turn_on"
                         else "off" if action == "turn_off"
                         else ("off" if cur == "on" else "on"))
            STATE[r][device] = new_state
            # Mirror into per-model stores if they have the room/device
            try:
                if device in STATE_LOCAL.get(r, {}):
                    STATE_LOCAL[r][device] = new_state
            except Exception:
                pass
            try:
                if device in STATE_CLOUD.get(r, {}):
                    STATE_CLOUD[r][device] = new_state
            except Exception:
                pass
            # publish state change so UI updates immediately
            try:
                publish_state_event()
            except Exception:
                pass
            applied.append({"room": r, "new_state": new_state})

        return jsonify({
            "ok": True, "bulk": True, "scope": room,
            "device": device, "action": action,
            "applied": applied, "skipped": skipped
        })

    # Single room
    if room not in STATE or device not in STATE[room]:
        return jsonify({"ok": False, "error": f"Unsupported room/device. Known rooms: {list(STATE.keys())}"}), 400

    cur = STATE[room][device]
    new_state = ("on" if action == "turn_on"
                 else "off" if action == "turn_off"
                 else ("off" if cur == "on" else "on"))
    STATE[room][device] = new_state
    # Mirror single-room change to local/cloud if present
    try:
        if device in STATE_LOCAL.get(room, {}):
            STATE_LOCAL[room][device] = new_state
    except Exception:
        pass
    try:
        if device in STATE_CLOUD.get(room, {}):
            STATE_CLOUD[room][device] = new_state
    except Exception:
        pass
    return jsonify({"ok": True, "room": room, "device": device, "action": action, "new_state": new_state})


@app.route('/api/stt', methods=['POST'])
def api_stt():
    """Accept an audio file upload (multipart form 'audio') or raw bytes and return transcription."""
    audio_bytes = None
    if 'audio' in request.files:
        f = request.files['audio']
        audio_bytes = f.read()
    else:
        audio_bytes = request.get_data()

    if not audio_bytes:
        return jsonify({'ok': False, 'error': 'no audio provided'}), 400

    if stt_provider is None:
        return jsonify({'ok': False, 'error': 'local STT provider not configured'}), 500

    language = request.args.get('lang', 'en')
    # Check the remote HTTP health endpoint. Do NOT attempt to start it here; user will manage it manually.
    try:
        healthy = False
        try:
            healthy = stt_provider.check_sensevoice_health()
        except Exception:
            healthy = False

        if not healthy:
            return jsonify({'ok': False, 'error': 'SenseVoice STT HTTP not reachable; please start the server manually and retry'}), 502

        # Now call transcribe and normalize timing values to integer ms
        res = stt_provider.transcribe_audio(audio_bytes, language=language)
        def _int(v):
            try:
                return int(round(float(v)))
            except Exception:
                return None

        timings = {
            'inference_ms': _int(res.get('inference_ms')),
            'upload_ms': _int(res.get('upload_ms')),
            'convert_ms': _int(res.get('convert_ms')),
            'total_ms': _int(res.get('total_ms')),
        }
        return jsonify({'ok': True, 'transcript': res.get('text'), 'timings': timings, 'raw': res})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500



@app.route('/api/stt/cloud', methods=['POST'])
def api_stt_cloud():
    """Send audio to OpenAI's transcription endpoint and return the transcript.
    Accepts multipart form 'audio' or raw bytes.
    """
    if not OPENAI_KEY:
        return jsonify({'ok': False, 'error': 'OPENAI_API_KEY not set'}), 500

    audio_bytes = None
    if 'audio' in request.files:
        audio_bytes = request.files['audio'].read()
        filename = request.files['audio'].filename or 'audio.webm'
    else:
        audio_bytes = request.get_data() or b''
        filename = 'audio.webm'

    if not audio_bytes:
        return jsonify({'ok': False, 'error': 'no audio provided'}), 400

    language = request.args.get('lang', 'en')

    # Post to OpenAI Audio Transcriptions
    try:
        # measure upload + inference times
        t_start = time.time()
        files = {
            'file': (filename, audio_bytes, 'application/octet-stream')
        }
        data = {'model': 'whisper-1', 'language': language}
        headers = {'Authorization': f'Bearer {OPENAI_KEY}'}

        t_upload_start = time.time()
        resp = requests.post('https://api.openai.com/v1/audio/transcriptions', headers=headers, files=files, data=data, timeout=60)
        t_upload_end = time.time()

        if resp.status_code != 200:
            return jsonify({'ok': False, 'error': f'OpenAI transcription failed: {resp.status_code} {resp.text[:500]}'}), 500

        t_infer_end = time.time()
        j = resp.json()
        text = j.get('text') or j.get('transcript') or ''

        upload_ms = int((t_upload_end - t_upload_start) * 1000)
        inference_ms = int((t_infer_end - t_upload_end) * 1000)
        total_ms = int((t_infer_end - t_start) * 1000)

        return jsonify({'ok': True, 'transcript': text, 'timings': {'upload_ms': upload_ms, 'inference_ms': inference_ms, 'total_ms': total_ms}, 'raw': j})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500







# Removed unused STT admin/debug endpoints to reduce clutter.


@app.route('/api/tts', methods=['POST'])
def api_tts_start():
    """Start a TTS race job. Returns job id and URLs for streaming / files.

    Request JSON: {"text": "...", "voice": "sage"}
    """
    data = request.get_json(force=True) or {}
    text = (data.get('text') or '')
    if not text:
        return jsonify({'ok': False, 'error': 'no text provided'}), 400
    voice = data.get('voice')
    job_id = uuid.uuid4().hex
    # create a single job and start background synth for full text
    with _TTS_JOB_LOCK:
        JOBS[job_id] = {
            'text': text,
            'created_at': time.time(),
            'status': {
                'local': 'pending' if local_tts_provider else 'disabled',
                'cloud': 'pending' if cloud_tts_provider else 'disabled'
            },
            'timings': {}
        }

    try:
        # Purge persisted files from previous jobs, but keep the new job id safe.
        _purge_old_job_files(except_job_id=job_id)
        _start_tts_background(job_id, text, voice=voice)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    stream_url = f"/api/tts/stream?job_id={job_id}"
    local_file = f"/api/tts/file/{job_id}?source=local"
    cloud_file = f"/api/tts/file/{job_id}?source=cloud"
    return jsonify({'ok': True, 'job_id': job_id, 'stream_url': stream_url, 'files': {'local': local_file, 'cloud': cloud_file}})


@app.route('/api/tts/stream', methods=['GET'])
def api_tts_stream():
    """Block until first audio (local or cloud) is available for the given job_id, then return audio bytes.

    Query: ?job_id=...&timeout=30
    """
    job_id = request.args.get('job_id')
    if not job_id:
        return jsonify({'ok': False, 'error': 'job_id required'}), 400
    timeout = float(request.args.get('timeout') or 30.0)
    start = time.time()

    # Poll until one of local/cloud is available or timeout
    while True:
        with _TTS_JOB_LOCK:
            job = JOBS.get(job_id)
            if not job:
                return jsonify({'ok': False, 'error': 'unknown job_id'}), 404
            local_b = job.get('local')
            cloud_b = job.get('cloud')

        # prefer whichever arrived first in time (we don't record arrival time, so return first non-None)
        if local_b:
            resp = Response(local_b, mimetype='audio/mpeg')
            resp.headers['X-TTS-Job'] = job_id
            resp.headers['X-TTS-Source'] = 'local'
            return resp
        if cloud_b:
            resp = Response(cloud_b, mimetype='audio/mpeg')
            resp.headers['X-TTS-Job'] = job_id
            resp.headers['X-TTS-Source'] = 'cloud'
            return resp

        if (time.time() - start) >= timeout:
            return jsonify({'ok': False, 'error': 'timeout waiting for audio'}), 504
        time.sleep(0.2)


@app.route('/api/tts/file/<job_id>', methods=['GET'])
def api_tts_file(job_id: str):
    """Serve saved audio file for job_id and source (local/cloud)."""
    source = (request.args.get('source') or 'local').lower()
    if source not in {'local', 'cloud'}:
        return jsonify({'ok': False, 'error': 'source must be local or cloud'}), 400

    with _TTS_JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({'ok': False, 'error': 'unknown job_id'}), 404
        path = job.get(f"{source}_path")
        data = job.get(source)

    # If persisted file exists on disk, prefer it
    try:
        if path and os.path.exists(path):
            with open(path, 'rb') as f:
                b = f.read()
            return Response(b, mimetype='audio/mpeg')
    except Exception:
        pass

    if data:
        return Response(data, mimetype='audio/mpeg')

    return jsonify({'ok': False, 'error': 'audio not available yet'}), 404


@app.route('/api/tts/status', methods=['GET'])
def api_tts_status():
    """Return job metadata and timings for a given job_id (no raw audio bytes)."""
    job_id = request.args.get('job_id')
    if not job_id:
        return jsonify({'ok': False, 'error': 'job_id required'}), 400
    with _TTS_JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({'ok': False, 'error': 'unknown job_id'}), 404
        out = {
            'job_id': job_id,
            'text': job.get('text'),
            'created_at': job.get('created_at'),
            'status': job.get('status', {}),
            'timings': job.get('timings', {}),
            'local_path': job.get('local_path'),
            'cloud_path': job.get('cloud_path')
        }
    return jsonify({'ok': True, 'job': out})


# Removed debug TTS jobs endpoint (unused by frontend)


@app.route('/api/tts/stream_sentences', methods=['POST'])
def api_tts_stream_sentences():
    """Stream TTS per-sentence for non-tool responses.

    Request JSON:
      - text: string
      - source: 'local'|'cloud'|'race' (default: 'local')
      - voice: voice name

    Emits SSE events:
      - sentence: {index, audio_data (base64), mime_type, text, tts_ms}
      - final_audio: {url}
      - tts_metrics: {ttfa_ms, tts_total_ms, sentences}
      - app_error/done
    """
    try:
        body = request.get_json(force=True) or {}
    except Exception:
        body = {}

    text = (body.get('text') or '').strip()
    source = (body.get('source') or 'local').lower()
    voice = body.get('voice') or 'alloy'

    if not text:
        def bad():
            yield sse_format('app_error', {'message': 'Text is empty'})
            yield sse_format('done', {})
        return Response(stream_with_context(bad()), mimetype='text/event-stream')

    headers = {
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no',
    }

    def gen():
        # Generate a run id for this streaming request and purge previous job
        # files while preserving any files that match this new run id.
        run_id = uuid.uuid4().hex
        _purge_old_job_files(except_job_id=run_id)
        parts = []
        ttfa_ms = None
        t_start = time.perf_counter()

        # Extract sentences by punctuation
        sentences, remainder = _extract_sentences(text)
        if remainder.strip():
            sentences.append(remainder.strip())

        if not sentences:
            yield sse_format('app_error', {'message': 'No sentences to synthesize'})
            yield sse_format('done', {})
            return

        for idx, sentence in enumerate(sentences):
            if not sentence.strip():
                continue
            a0 = time.perf_counter()
            audio_bytes = None

            # Choose provider
            try:
                if source == 'local':
                    if not local_tts_provider:
                        raise RuntimeError('local TTS provider not configured')
                    audio_bytes = local_tts_provider.synthesize_speech(sentence, voice)
                elif source == 'cloud':
                    if not cloud_tts_provider:
                        raise RuntimeError('cloud TTS provider not configured')
                    audio_bytes = cloud_tts_provider.synthesize_speech(sentence, voice)
                else:
                    # race: choose whichever returns first (local vs cloud)
                    from concurrent.futures import ThreadPoolExecutor, as_completed

                    def call_local():
                        if not local_tts_provider:
                            raise RuntimeError('local TTS provider not configured')
                        return local_tts_provider.synthesize_speech(sentence, voice)

                    def call_cloud():
                        if not cloud_tts_provider:
                            raise RuntimeError('cloud TTS provider not configured')
                        return cloud_tts_provider.synthesize_speech(sentence, voice)

                    with ThreadPoolExecutor(max_workers=2) as ex:
                        futures = {ex.submit(call_local): 'local', ex.submit(call_cloud): 'cloud'}
                        for f in as_completed(futures):
                            try:
                                audio_bytes = f.result()
                                break
                            except Exception:
                                continue

                a1 = time.perf_counter()
            except Exception as e:
                yield sse_format('app_error', {'message': f'TTS failed for sentence {idx}: {repr(e)}'})
                continue

            if not audio_bytes:
                yield sse_format('app_error', {'message': f'TTS returned empty audio for sentence {idx}'})
                continue

            parts.append(bytes(audio_bytes))
            # persist each chunk to disk so we can reliably reconstruct the full
            # audio later and provide per-chunk artifacts for debugging/inspection
            try:
                chunk_path = _save_chunk_audio(run_id, idx, source, audio_bytes)
                with _TTS_JOB_LOCK:
                    job = JOBS.get(run_id) or {}
                    job.setdefault('chunks', []).append(chunk_path)
                    JOBS[run_id] = job
                try:
                    print(f"[TTS STREAM] saved chunk run_id={run_id} idx={idx} source={source} path={chunk_path} size={os.path.getsize(chunk_path) if os.path.exists(chunk_path) else 'unknown'}")
                except Exception:
                    pass
            except Exception:
                # non-fatal; continue streaming (chunk save failed)
                try:
                    print(f"[TTS STREAM] run_id={run_id} source={source} sentence_idx={idx} chunk_save_failed")
                except Exception:
                    pass

            if ttfa_ms is None:
                ttfa_ms = int((a1 - t_start) * 1000.0)

            audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')

            yield sse_format('sentence', {
                'index': idx,
                'audio_data': audio_b64,
                'mime_type': 'audio/mpeg',
                'text': sentence,
                'tts_ms': (a1 - a0) * 1000.0
            })

        # Save full audio
        full = b''.join(parts)
        try:
            job_meta = {'text': text, 'created_at': time.time()}
            with _TTS_JOB_LOCK:
                JOBS[run_id] = job_meta

            save_path = _save_job_audio(run_id, source, full)
            with _TTS_JOB_LOCK:
                # Save under both the legacy key (source) and a bytes-specific key so
                # `api_tts_file` and other consumers can find the final audio regardless
                # of which key they check.
                JOBS[run_id][f"{source}_path"] = save_path
                JOBS[run_id][f"{source}_bytes"] = full
                JOBS[run_id][source] = full
                JOBS[run_id]['status'] = {source: 'done'}
            try:
                print(f"[TTS STREAM] saved final run_id={run_id} source={source} path={save_path} size={os.path.getsize(save_path) if os.path.exists(save_path) else len(full)}")
            except Exception:
                pass
        except Exception:
            yield sse_format('app_error', {'message': 'Failed to persist final audio'})

        t_end = time.perf_counter()
        tts_total_ms = int((t_end - t_start) * 1000.0)

        # final audio URL (use existing file-serving endpoint)
        yield sse_format('final_audio', {'url': f'/api/tts/file/{run_id}?source={source}'})

        # Also emit the full audio bytes as base64 over SSE so clients can obtain the
        # authoritative final audio even when the server is busy handling the streaming
        # connection (Flask dev server may be single-threaded). This prevents a race
        # where clients fetch the file URL before the OS has flushed the file to disk.
        try:
            audio_b64_full = base64.b64encode(full).decode('utf-8')
            yield sse_format('final_audio_bytes', {
                'audio_data': audio_b64_full,
                'mime_type': 'audio/mpeg'
            })
        except Exception:
            # non-fatal; continue
            pass

        yield sse_format('tts_metrics', {
            'ttfa_ms': ttfa_ms,
            'tts_total_ms': tts_total_ms,
            'sentences': len(sentences)
        })

        yield sse_format('done', {})

    return Response(stream_with_context(gen()), mimetype='text/event-stream', headers=headers)


@app.route('/api/tts/summarize', methods=['POST'])
def api_tts_summarize():
    """Return a short, spoken-English summary for TTS.

    Accepts JSON: {"text": "assistant content...", "applied": <applied object>, "prefer": "cloud"|"local"}
    Tries preferred LLM (cloud if OPENAI_KEY, else local), falls back if unavailable.
    """
    data = request.get_json(force=True) or {}
    text = data.get('text', '')
    applied = data.get('applied')
    prefer = (data.get('prefer') or '').lower()

    # Defensive sanitization
    def _sanitize(s: str) -> str:
        if not s:
            return ''
        try:
            out = str(s).strip()
            # If the assistant returned a JSON-like wrapper, try to parse and extract useful text
            try:
                j = json.loads(out)
                if isinstance(j, dict):
                    # prefer `reply` or `content` or `text` keys if present
                    for k in ('reply', 'content', 'text', 'message'):
                        if k in j and j.get(k):
                            return str(j.get(k)).strip()
                    # If the JSON only contains a tool invocation / metadata (e.g. {"tool":"tell_story","story_length":100}),
                    # treat it as no user-facing content so we don't send it to TTS.
                    tool_like_keys = {'tool', 'action', 'command', 'tool_name', 'tool_call'}
                    if any((k.lower() in tool_like_keys or k.lower().endswith('_length') or k.lower().endswith('_size')) for k in j.keys()):
                        return ''
                    # fallback: join any string-valued properties
                    vals = [str(v).strip() for v in j.values() if isinstance(v, str) and v.strip()]
                    if vals:
                        return ' '.join(vals).strip()
            except Exception:
                # not JSON — continue with regex cleaning
                pass

            # remove common tokens that leak tooling/debug info (cover quoted keys too)
            import re
            out = re.sub(r'"?tool"?\s*:\s*null,?', '', out, flags=re.IGNORECASE)
            out = re.sub(r'"?tool"?\s*:\s*"[^"]*"\s*,?', '', out, flags=re.IGNORECASE)
            out = re.sub(r'reply\s*-\s*', '', out, flags=re.IGNORECASE)
            out = re.sub(r'reply\s*:\s*', '', out, flags=re.IGNORECASE)
            # strip lines that start with tool: or similar labels
            out = '\n'.join([ln for ln in out.split('\n') if not re.match(r'^\s*"?tool"?\s*:', ln, flags=re.IGNORECASE)])
            # collapse whitespace
            out = ' '.join(out.split())
            return out.strip()
        except Exception:
            return ''

    safe_text = _sanitize(text)
    safe_applied = applied
    try:
        # ensure applied can be stringified
        applied_str = json.dumps(safe_applied, default=str)
    except Exception:
        applied_str = str(safe_applied or '')

    # Build system and user messages for the summarizer
    system_msg = (
        "You are a concise assistant that rewrites an assistant's output into a single natural, spoken-English sentence suitable for playback by a TTS system. "
        "Do NOT include any JSON, code, or debug tokens such as 'tool:null', 'tool:', 'reply', or internal markers. "
        "Do not mention internal implementation details. Keep the summary friendly, human, and under 20 words. Respond with plain text only."
    )

    user_prompt = data.get('user_prompt') or ''
    user_msg = f"Original user request: {user_prompt}\n\nAssistant content: {safe_text}\n\nApplied result (JSON): {applied_str}\n\nProduce a single short sentence as described above."

    summary = ''

    # Try cloud first if preferred or available
    def try_cloud():
        if not OPENAI_KEY:
            raise RuntimeError('no OPENAI_API_KEY')
        headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
        payload = {"model": "gpt-5.2", "messages": [{"role": "system", "content": system_msg}, {"role": "user", "content": user_msg}], "temperature": 0.2}
        resp = requests.post(OPENAI_URL, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        j = resp.json()
        try:
            return j.get('choices', [])[0].get('message', {}).get('content', '') or ''
        except Exception:
            return ''

    def try_local():
        if not local_llm:
            raise RuntimeError('no local LLM')
        # Pass system role + user content to local LLM if supported
        msgs = [{"role": "system", "content": system_msg}, {"role": "user", "content": user_msg}]
        resp = local_llm.post_chat(msgs)
        try:
            if hasattr(local_llm, 'get_message_content'):
                return local_llm.get_message_content(resp) or ''
        except Exception:
            pass
        # Best-effort fallback
        try:
            return str(resp)
        except Exception:
            return ''

    attempts = []
    if prefer == 'cloud':
        attempts = [try_cloud, try_local]
    elif prefer == 'local':
        attempts = [try_local, try_cloud]
    else:
        attempts = [try_cloud, try_local] if OPENAI_KEY else [try_local, try_cloud]

    last_exc = None
    for fn in attempts:
        try:
            out = fn()
            out = _sanitize(out)
            if out:
                summary = out
                break
        except Exception as e:
            last_exc = e
            continue

    if not summary:
        summary = 'Okay — I performed the requested action.'

    return jsonify({'ok': True, 'summary': summary})


# Removed TTS clear_job admin endpoint (unused by frontend)



def apply_toolcall(js: dict, target: str = 'local', last_user_text: str = None) -> dict:
    """Apply a parsed assistant toolcall (manage_device) to either the local or cloud in-memory state.

    `target` should be 'local' or 'cloud'. Returns a result dict similar to /api/device.
    """
    if not isinstance(js, dict):
        return {"ok": False, "error": "invalid payload"}

    tool = js.get("tool")
    if tool not in {"manage_device", "query_state"}:
        return {"ok": False, "error": "unsupported tool"}

    # Support multiple-room specifications: 'rooms' list or comma/and-separated 'room' string
    raw_room = js.get("room", "") or ""
    rooms_field = js.get("rooms", None)
    # If the model returned a list in 'room' (or returned rooms as list), treat as multi-room
    if isinstance(raw_room, list) and not rooms_field:
        rooms_field = raw_room
        room = ""
    else:
        # safe normalization: only call norm_room for string inputs
        room = norm_room(raw_room) if not isinstance(raw_room, list) else ""
    device = norm_device(js.get("device", ""))
    action = (js.get("action", "") or "").strip().lower()
    value = js.get("value", None)

    # Select target stores
    rooms_store = STATE_LOCAL if target == 'local' else STATE_CLOUD
    house_store = HOUSE_LOCAL if target == 'local' else HOUSE_CLOUD

    # Handle lightweight query_state toolcalls which request room/device state
    if tool == 'query_state':
        # Accept either 'room' (string) or 'rooms' (list). If rooms contains 'all', return all rooms.
        raw_room = js.get('room', '') or ''
        rooms_field = js.get('rooms', None)
        if isinstance(raw_room, list) and not rooms_field:
            rooms_field = raw_room
            room = ''
        else:
            room = norm_room(raw_room) if not isinstance(raw_room, list) else ''

        # If device is thermostat, return house-level state
        device = norm_device(js.get('device', ''))
        if device == 'thermostat':
            return {'ok': True, 'query': {'device': 'thermostat', 'target_store': target}, 'result': {'thermostat': dict(house_store)}}

        # Build list of targets for room-level queries
        if isinstance(rooms_field, list) and len(rooms_field) > 0:
            normed = [norm_room(str(r)) for r in rooms_field]
            if any(r == 'all' for r in normed):
                targets = list(rooms_store.keys())
            else:
                targets = normed
        else:
            if room in { 'all', 'upstairs', 'downstairs' }:
                targets = list(rooms_store.keys()) if room == 'all' else ([r for r in rooms_store if (r in UPSTAIRS if room == 'upstairs' else r in DOWNSTAIRS)])
            else:
                targets = [room]

        # If device not specified, return whole-room states
        result = {'ok': True, 'query': {'targets': targets, 'device': device, 'target_store': target}}
        data = {}
        for r in targets:
            if not r or r not in rooms_store:
                data[r] = None
                continue
            if device:
                data[r] = rooms_store[r].get(device)
            else:
                data[r] = dict(rooms_store[r])
        result['result'] = data
        return result

    # Blinds have been removed from the model; treat only lights here

    # Thermostat applied to house_store
    if device == "thermostat":
        if action not in VALID_THERMO_ACTIONS:
            return {"ok": False, "error": f"Unknown thermostat action: {action}"}

        # If increase/decrease without explicit value, try to infer from user text
        if action in {"increase", "decrease"} and (value is None):
            try:
                if local_llm and hasattr(local_llm, 'infer_thermo_step') and last_user_text:
                    inferred = local_llm.infer_thermo_step(last_user_text)
                    if inferred is not None:
                        value = inferred
            except Exception:
                pass

        step = 1.0
        if value is not None:
            try:
                step = float(value)
            except:
                pass

        if action == "increase":
            house_store["target"] = clamp(house_store["target"] + step, 10.0, 28.0)
        elif action == "decrease":
            house_store["target"] = clamp(house_store["target"] - step, 10.0, 28.0)
        elif action == "set_value":
            try:
                num = float(value)
            except (TypeError, ValueError):
                return {"ok": False, "error": "set_value requires numeric 'value'"}
            house_store["target"] = clamp(num, 10.0, 28.0)
        elif action == "turn_on":
            house_store["mode"] = "heat"
        elif action == "turn_off":
            house_store["mode"] = "off"

        # trigger house update for that target (timestamp maintenance)
        if target == 'local':
            _update_house_temp_local()
        elif target == 'cloud':
            _update_house_temp_cloud()
        else:
            _update_house_temp()

        # publish state change so SSE clients see updated target/mode
        try:
            publish_state_event()
        except Exception:
            pass

        return {"ok": True, "device": "thermostat", "action": action, "house": house_store}

    # Lights
    if device not in {"light"}:
        return {"ok": False, "error": f"Unsupported device for target: {device}"}

    # Multi-room: if 'rooms' list provided, apply to those rooms.
    # Treat a list containing a single 'all' (or any item normalized to 'all')
    # as the special scope 'all' rather than attempting to apply to a room
    # literally named 'all'. This handles model outputs like {"rooms":["all"]}.
    if isinstance(rooms_field, list) and len(rooms_field) > 0:
        normed = [norm_room(str(r)) for r in rooms_field]
        if any(r == 'all' for r in normed):
            targets = list(rooms_store.keys())
        else:
            targets = normed
    else:
        # If room is a comma/and-separated list, split it
        if isinstance(raw_room, str) and ("," in raw_room or " and " in raw_room):
            parts = [norm_room(p) for p in __import__('re').split(r",| and ", raw_room) if p.strip()]
            targets = parts
        else:
            # Keep existing scope handling for 'all', 'upstairs', 'downstairs'
            if room in {"all", "upstairs", "downstairs"}:
                targets = list(rooms_store.keys()) if room == "all" else \
                          [r for r in rooms_store if (r in UPSTAIRS if room == "upstairs" else r in DOWNSTAIRS)]
            else:
                targets = [room]

    # Apply action to each target room where possible
    applied, skipped = [], []
    for r in targets:
        if not r or r not in rooms_store or device not in rooms_store[r]:
            skipped.append(r)
            continue
        cur = rooms_store[r][device]
        new_state = ("on" if action == "turn_on"
                     else "off" if action == "turn_off"
                     else ("off" if cur == "on" else "on"))
        rooms_store[r][device] = new_state
        applied.append({"room": r, "new_state": new_state})

    # If multiple targets were specified, return bulk result
    if len(applied) > 1 or (len(skipped) > 0 and len(applied) > 0):
        return {"ok": True, "bulk": True, "scope": rooms_field or room, "device": device, "action": action, "applied": applied, "skipped": skipped}

    # Single room
    if room not in rooms_store or device not in rooms_store[room]:
        return {"ok": False, "error": f"Unsupported room/device. Known rooms: {list(rooms_store.keys())}"}

    cur = rooms_store[room][device]
    new_state = ("on" if action == "turn_on"
                 else "off" if action == "turn_off"
                 else ("off" if cur == "on" else "on"))
    rooms_store[room][device] = new_state
    # publish state change for SSE clients
    try:
        publish_state_event()
    except Exception:
        pass
    return {"ok": True, "room": room, "device": device, "action": action, "new_state": new_state}


def post_chat_openai(history: list) -> dict:
    """Send chat to OpenAI Chat Completions API and return the JSON response.

    Requires OPENAI_KEY environment variable. Returns dict or raises.
    """
    if not OPENAI_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")

    # Ensure the cloud model receives the same system prompt guidance as the local helper
    sys_prompt = None
    if llm_helper and hasattr(llm_helper, 'SYSTEM_PROMPT'):
        sys_prompt = llm_helper.SYSTEM_PROMPT
    elif local_llm and hasattr(local_llm, 'SYSTEM_PROMPT'):
        sys_prompt = local_llm.SYSTEM_PROMPT
    else:
        sys_prompt = "You are Butler. Output JSON only as specified by the assistant's device-control schema."

    messages = [{"role": "system", "content": sys_prompt}]
    # include recent history if helper exposes MAX_HISTORY
    try:
        max_hist = llm_helper.MAX_HISTORY if llm_helper and hasattr(llm_helper, 'MAX_HISTORY') else (local_llm.MAX_HISTORY if local_llm and hasattr(local_llm, 'MAX_HISTORY') else None)
        if max_hist and isinstance(max_hist, int):
            messages += history[-max_hist:]
        else:
            messages += history
    except Exception:
        messages += history

    payload = {"model": "gpt-5.2", "messages": messages, "temperature": 0}
    headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    resp = requests.post(OPENAI_URL, json=payload, headers=headers, timeout=(5, 60))
    resp.raise_for_status()
    return resp.json()


@app.post('/api/chat/dual')
# Removed chat dual endpoint (unused by frontend)


@app.post('/api/chat/local')
def api_chat_local():
    """Send a prompt/history to the local LLM only and return its response."""
    if local_llm is None:
        return jsonify({'ok': False, 'error': 'no local LLM available'}), 500
    data = request.get_json(force=True) or {}
    history = data.get('history') or data.get('messages') or []
    user = data.get('user')
    if user and not history:
        history = [{"role": "user", "content": user}]
    try:
        t0 = time.time()
        resp = local_llm.post_chat(history)
        t1 = time.time()
        content = local_llm.get_message_content(resp) if local_llm else ''
        parsed = None
        applied = None
        try:
            parsed = local_llm.extract_json(content) if local_llm else None
        except Exception:
            parsed = None
        if parsed:
            last_text = history[-1]['content'] if history else (user or '')
            applied = apply_toolcall(parsed, target='local', last_user_text=last_text)
        ms = int((t1 - t0) * 1000)
        return jsonify({'ok': True, 'resp': resp, 'content': content, 'parsed': parsed, 'applied': applied, 'ms': ms})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.post('/api/chat/cloud')
def api_chat_cloud():
    """Send a prompt/history to the cloud (OpenAI) LLM only and return its response."""
    if not OPENAI_KEY:
        return jsonify({'ok': False, 'error': 'OPENAI_API_KEY not set'}), 500
    data = request.get_json(force=True) or {}
    history = data.get('history') or data.get('messages') or []
    user = data.get('user')
    if user and not history:
        history = [{"role": "user", "content": user}]
    try:
        t0 = time.time()
        resp = post_chat_openai(history)
        t1 = time.time()
        # extract content and parse using helper utilities
        content = ''
        try:
            if llm_helper:
                content = llm_helper.get_message_content(resp)
            else:
                content = resp.get('choices', [])[0].get('message', {}).get('content', '')
        except Exception:
            content = ''
        parsed = None
        applied = None
        try:
            parsed = llm_helper.extract_json(content) if llm_helper else None
        except Exception:
            parsed = None
        if parsed:
            last_text = history[-1]['content'] if history else (user or '')
            applied = apply_toolcall(parsed, target='cloud', last_user_text=last_text)
        ms = int((t1 - t0) * 1000)
        return jsonify({'ok': True, 'resp': resp, 'content': content, 'parsed': parsed, 'applied': applied, 'ms': ms})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.get('/api/chat/stream')
def api_chat_stream():
    """SSE endpoint that races local and cloud and streams each model's arrival as an event.

    Usage: GET /api/chat/stream?user=... or &history=... (history not implemented in querystring)
    """
    user = request.args.get('user', '')
    history = [{"role": "user", "content": user}] if user else []

    def generate():
        start = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            futures = {}
            if local_llm:
                futures[ex.submit(local_llm.post_chat, history)] = 'local'
            if OPENAI_KEY:
                futures[ex.submit(post_chat_openai, history)] = 'cloud'

            for fut in concurrent.futures.as_completed(futures):
                who = futures[fut]
                t = int((time.time() - start) * 1000)
                try:
                    resp = fut.result()
                except Exception as e:
                    payload = {"model": who, "ok": False, "error": str(e), "ms": t}
                    yield f"event: model\ndata: {json.dumps(payload)}\n\n"
                    continue

                # get content and parse via helper
                try:
                    if who == 'local' and local_llm:
                        content = local_llm.get_message_content(resp)
                    elif llm_helper:
                        content = llm_helper.get_message_content(resp)
                    else:
                        content = resp.get('choices', [])[0].get('message', {}).get('content', '')
                except Exception:
                    content = ''

                parsed = None
                applied = None
                try:
                    parsed = llm_helper.extract_json(content) if llm_helper else None
                except Exception:
                    parsed = None

                if parsed:
                    last_text = history[-1]['content'] if history else (user or '')
                    applied = apply_toolcall(parsed, target=who, last_user_text=last_text)

                payload = {"model": who, "ok": True, "content": content, "parsed": parsed, "applied": applied, "ms": t}
                yield f"event: model\ndata: {json.dumps(payload, default=str)}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@app.get('/api/state/stream')
def api_state_stream():
    """SSE endpoint that streams state snapshots whenever the server state changes."""
    def gen(q: queue.Queue):
        # send initial snapshot
        try:
            init = json.dumps(_current_state_snapshot())
            yield f"event: state\ndata: {init}\n\n"
        except Exception:
            pass

        while True:
            try:
                payload = q.get(timeout=30)
            except Exception:
                # send a ping comment to keep connection alive
                try:
                    yield ": ping\n\n"
                except Exception:
                    break
                continue
            try:
                yield f"event: state\ndata: {payload}\n\n"
            except GeneratorExit:
                break

    q = queue.Queue()
    with _state_sub_lock:
        _state_subscribers.append(q)

    def stream_and_cleanup():
        try:
            return gen(q)
        finally:
            with _state_sub_lock:
                try:
                    _state_subscribers.remove(q)
                except Exception:
                    pass

    return Response(stream_with_context(stream_and_cleanup()), mimetype='text/event-stream')

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)