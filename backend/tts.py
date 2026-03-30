import os
import time
import threading
import base64
import uuid
from pathlib import Path
import concurrent.futures

# Providers and JOB store
try:
    from .providers import local_paroli_tts as local_tts_provider
except Exception:
    try:
        from providers import local_paroli_tts as local_tts_provider
    except Exception:
        local_tts_provider = None

try:
    from .providers import openai_tts as cloud_tts_provider
except Exception:
    try:
        from providers import openai_tts as cloud_tts_provider
    except Exception:
        cloud_tts_provider = None

TTS_OUTPUT_DIR = Path(__file__).resolve().parent / "tts_output"
_TTS_JOB_LOCK = threading.Lock()
JOBS = {}


def _ensure_tts_output_dir():
    try:
        TTS_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _save_job_audio(job_id: str, source: str, data: bytes) -> str:
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


def _purge_old_job_files(except_job_id: str = None) -> dict:
    summary = {'deleted': [], 'errors': []}
    try:
        preserved = set()
        if except_job_id:
            preserved.add(except_job_id)

        with _TTS_JOB_LOCK:
            existing_jobs = list(JOBS.keys())

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
                    sts = job_status_snapshot.get(job_id_in_name, {})
                    if any(v == 'pending' for v in sts.values()):
                        continue
                    p.unlink()
                    summary['deleted'].append(str(p))
                except Exception as e:
                    summary['errors'].append(f"{p}: {e}")

        with _TTS_JOB_LOCK:
            for jid in existing_jobs:
                if jid in preserved:
                    continue
                sts = job_status_snapshot.get(jid, {})
                if any(v == 'pending' for v in sts.values()):
                    continue
                try:
                    JOBS.pop(jid, None)
                except Exception as e:
                    summary['errors'].append(f"jobs_pop_{jid}: {e}")
    except Exception as e:
        summary['errors'].append(str(e))
    return summary


_SENT_RE_chars = ".!?;:"

def _extract_sentences(buf: str):
    import re
    sentences = []
    start = 0
    SENT_RE = re.compile(r'([\.%s]+)(\s+|$)' % re.escape(_SENT_RE_chars))
    for m in SENT_RE.finditer(buf):
        end = m.end()
        s = buf[start:end].strip()
        if s:
            sentences.append(s)
        start = end
    return sentences, buf[start:]


def _start_tts_background(job_id: str, text: str, voice: str = None):
    def work():
        def run_provider(src, provider):
            t0 = time.time()
            data = None
            try:
                data = provider.synthesize_speech(text, voice=voice)
            except Exception:
                data = None
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
                        try:
                            job[f"{src}_bytes"] = data
                        except Exception:
                            pass
                    except Exception:
                        pass
                    job['status'][src] = 'done'
                else:
                    job[src] = None
                    job['status'][src] = 'failed'
                JOBS[job_id] = job

        t = threading.Thread(target=lambda: _background_exec(run_provider), daemon=True)
        t.start()

    def _background_exec(run_provider_fn):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            futures = {}
            if local_tts_provider:
                futures[ex.submit(run_provider_fn, 'local', local_tts_provider)] = 'local'
            if cloud_tts_provider:
                futures[ex.submit(run_provider_fn, 'cloud', cloud_tts_provider)] = 'cloud'
            for fut in concurrent.futures.as_completed(futures):
                _ = futures.get(fut)

    work()
