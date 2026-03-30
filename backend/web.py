from flask import Flask, send_from_directory, request, jsonify, Response, stream_with_context
from pathlib import Path
import os, time, base64, uuid, queue, threading, requests, concurrent.futures, json

# Prefer package-style relative imports, but allow running `python web.py`
try:
    from . import state, tts, llm_api
except Exception:
    import state, tts, llm_api

app = Flask(__name__, static_folder="../frontend", static_url_path="")
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# optional local STT provider
try:
    from .providers import local_sensevoice_stt as stt_provider
except Exception:
    try:
        from providers import local_sensevoice_stt as stt_provider
    except Exception:
        stt_provider = None


@app.get("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")

@app.get("/styles.css")
def styles_css():
    return send_from_directory(FRONTEND_DIR, "styles.css")

@app.get("/app.js")
def app_js():
    return send_from_directory(FRONTEND_DIR, "app.js")


@app.get('/api/state')
def get_state():
    which = (request.args.get('which') or '').lower()
    if which == 'local':
        state._update_house_temp_local()
        return jsonify({"house": state.HOUSE_LOCAL, "rooms": state.STATE_LOCAL})
    if which == 'cloud':
        state._update_house_temp_cloud()
        return jsonify({"house": state.HOUSE_CLOUD, "rooms": state.STATE_CLOUD})
    state._update_house_temp()
    return jsonify({"house": state.HOUSE, "rooms": state.STATE})


@app.get('/api/state/stream')
def api_state_stream():
    def gen(q: queue.Queue):
        try:
            init = state._current_state_snapshot()
            yield f"event: state\ndata: {json.dumps(init)}\n\n"
        except Exception:
            pass
        while True:
            try:
                payload = q.get(timeout=30)
            except Exception:
                yield ": ping\n\n"
                continue
            yield f"event: state\ndata: {payload}\n\n"

    q = queue.Queue()
    with state._state_sub_lock:
        state._state_subscribers.append(q)

    def cleanup():
        try:
            yield from gen(q)
        finally:
            with state._state_sub_lock:
                try:
                    state._state_subscribers.remove(q)
                except Exception:
                    pass

    return Response(stream_with_context(cleanup()), mimetype='text/event-stream')


@app.post('/api/device')
def device():
    data = request.get_json(force=True) or {}
    payload = {
        'tool': 'manage_device',
        'room': data.get('room', ''),
        'device': data.get('device', ''),
        'action': data.get('action', ''),
        'value': data.get('value', None)
    }
    try:
        res = llm_api.apply_toolcall(payload, target='local')
        try:
            state.publish_state_event()
        except Exception:
            pass
        return jsonify(res)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.post('/api/chat/local')
def api_chat_local():
    if not getattr(llm_api, 'local_llm', None):
        return jsonify({'ok': False, 'error': 'no local LLM available'}), 500
    data = request.get_json(force=True) or {}
    history = data.get('history') or data.get('messages') or []
    user = data.get('user')
    if user and not history:
        history = [{'role': 'user', 'content': user}]
    try:
        t0 = time.time()
        resp = llm_api.local_llm.post_chat(history)
        t1 = time.time()
        content = llm_api.local_llm.get_message_content(resp) if hasattr(llm_api.local_llm, 'get_message_content') else str(resp)
        parsed = None
        applied = None
        try:
            if hasattr(llm_api.local_llm, 'extract_json'):
                parsed = llm_api.local_llm.extract_json(content)
        except Exception:
            parsed = None
        if parsed:
            last_text = history[-1]['content'] if history else (user or '')
            applied = llm_api.apply_toolcall(parsed, target='local', last_user_text=last_text)
            try:
                state.publish_state_event()
            except Exception:
                pass
        ms = int((t1 - t0) * 1000)
        return jsonify({'ok': True, 'resp': resp, 'content': content, 'parsed': parsed, 'applied': applied, 'ms': ms})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.post('/api/chat/cloud')
def api_chat_cloud():
    if not getattr(llm_api, 'OPENAI_KEY', None):
        return jsonify({'ok': False, 'error': 'OPENAI_API_KEY not set'}), 500
    data = request.get_json(force=True) or {}
    history = data.get('history') or data.get('messages') or []
    user = data.get('user')
    if user and not history:
        history = [{'role': 'user', 'content': user}]
    try:
        t0 = time.time()
        resp = llm_api.post_chat_openai(history)
        t1 = time.time()
        # extract content
        try:
            content = llm_api.llm_helper.get_message_content(resp) if getattr(llm_api, 'llm_helper', None) else resp.get('choices', [])[0].get('message', {}).get('content', '')
        except Exception:
            content = ''
        parsed = None
        applied = None
        try:
            if getattr(llm_api, 'llm_helper', None) and hasattr(llm_api.llm_helper, 'extract_json'):
                parsed = llm_api.llm_helper.extract_json(content)
        except Exception:
            parsed = None
        if parsed:
            last_text = history[-1]['content'] if history else (user or '')
            applied = llm_api.apply_toolcall(parsed, target='cloud', last_user_text=last_text)
            try:
                state.publish_state_event()
            except Exception:
                pass
        ms = int((t1 - t0) * 1000)
        return jsonify({'ok': True, 'resp': resp, 'content': content, 'parsed': parsed, 'applied': applied, 'ms': ms})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.get('/api/chat/stream')
def api_chat_stream():
    user = request.args.get('user', '')
    history = [{'role': 'user', 'content': user}] if user else []

    def generate():
        start = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            futures = {}
            if getattr(llm_api, 'local_llm', None):
                futures[ex.submit(llm_api.local_llm.post_chat, history)] = 'local'
            if getattr(llm_api, 'OPENAI_KEY', None):
                futures[ex.submit(llm_api.post_chat_openai, history)] = 'cloud'

            for fut in concurrent.futures.as_completed(futures):
                who = futures[fut]
                t = int((time.time() - start) * 1000)
                try:
                    resp = fut.result()
                except Exception as e:
                    payload = {'model': who, 'ok': False, 'error': str(e), 'ms': t}
                    yield f"event: model\ndata: {json.dumps(payload)}\n\n"
                    continue

                try:
                    if who == 'local' and getattr(llm_api, 'local_llm', None):
                        content = llm_api.local_llm.get_message_content(resp)
                    elif getattr(llm_api, 'llm_helper', None):
                        content = llm_api.llm_helper.get_message_content(resp)
                    else:
                        content = resp.get('choices', [])[0].get('message', {}).get('content', '')
                except Exception:
                    content = ''

                parsed = None
                applied = None
                try:
                    if who == 'local' and getattr(llm_api, 'local_llm', None) and hasattr(llm_api.local_llm, 'extract_json'):
                        parsed = llm_api.local_llm.extract_json(content)
                    elif getattr(llm_api, 'llm_helper', None) and hasattr(llm_api.llm_helper, 'extract_json'):
                        parsed = llm_api.llm_helper.extract_json(content)
                except Exception:
                    parsed = None

                if parsed:
                    last_text = history[-1]['content'] if history else (user or '')
                    applied = llm_api.apply_toolcall(parsed, target=who, last_user_text=last_text)
                    try:
                        state.publish_state_event()
                    except Exception:
                        pass

                payload = {'model': who, 'ok': True, 'content': content, 'parsed': parsed, 'applied': applied, 'ms': t}
                yield f"event: model\ndata: {json.dumps(payload, default=str)}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@app.route('/api/tts/summarize', methods=['POST'])
def api_tts_summarize():
    data = request.get_json(force=True) or {}
    text = data.get('text', '')
    applied = data.get('applied')
    prefer = (data.get('prefer') or '').lower()

    def _sanitize(s: str) -> str:
        if not s:
            return ''
        try:
            j = json.loads(s)
            if isinstance(j, dict):
                for k in ('reply', 'content', 'text', 'message'):
                    if k in j and j.get(k):
                        return str(j.get(k)).strip()
                vals = [str(v).strip() for v in j.values() if isinstance(v, str) and v.strip()]
                if vals:
                    return ' '.join(vals)
        except Exception:
            pass
        return ' '.join(str(s).split())

    safe_text = _sanitize(text)
    if not safe_text:
        return jsonify({'ok': True, 'summary': ''})

    system_msg = (
        "You are a concise assistant that rewrites an assistant's output into a single natural, "
        "spoken-English sentence suitable for playback by a TTS system. Keep it under 20 words."
    )
    user_msg = f"Assistant content: {safe_text}\nApplied: {json.dumps(applied, default=str)}"

    def try_cloud():
        if not getattr(llm_api, 'OPENAI_KEY', None):
            raise RuntimeError('no OPENAI_API_KEY')
        # reuse post_chat_openai which wraps system prompt
        resp = llm_api.post_chat_openai([{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': user_msg}])
        try:
            if getattr(llm_api, 'llm_helper', None):
                return llm_api.llm_helper.get_message_content(resp) or ''
        except Exception:
            pass
        return resp.get('choices', [])[0].get('message', {}).get('content', '')

    def try_local():
        if not getattr(llm_api, 'local_llm', None):
            raise RuntimeError('no local LLM')
        msgs = [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': user_msg}]
        resp = llm_api.local_llm.post_chat(msgs)
        try:
            return llm_api.local_llm.get_message_content(resp) or ''
        except Exception:
            return str(resp)

    attempts = []
    if prefer == 'cloud':
        attempts = [try_cloud, try_local]
    elif prefer == 'local':
        attempts = [try_local, try_cloud]
    else:
        attempts = [try_cloud, try_local] if getattr(llm_api, 'OPENAI_KEY', None) else [try_local, try_cloud]

    summary = ''
    for fn in attempts:
        try:
            out = fn()
            out = _sanitize(out)
            if out:
                summary = out
                break
        except Exception:
            continue

    if not summary:
        summary = 'Okay — I performed the requested action.'

    return jsonify({'ok': True, 'summary': summary})


@app.route('/api/stt', methods=['POST'])
def api_stt():
    audio_bytes = None
    if 'audio' in request.files:
        audio_bytes = request.files['audio'].read()
    else:
        audio_bytes = request.get_data()
    if not audio_bytes:
        return jsonify({'ok': False, 'error': 'no audio provided'}), 400
    if stt_provider is None:
        return jsonify({'ok': False, 'error': 'local STT provider not configured'}), 500
    language = request.args.get('lang', 'en')
    try:
        healthy = False
        try:
            healthy = stt_provider.check_sensevoice_health()
        except Exception:
            healthy = False
        if not healthy:
            return jsonify({'ok': False, 'error': 'SenseVoice STT HTTP not reachable; please start the server manually and retry'}), 502
        res = stt_provider.transcribe_audio(audio_bytes, language=language)
        def _int(v):
            try:
                return int(round(float(v)))
            except Exception:
                return None
        timings = {'inference_ms': _int(res.get('inference_ms')), 'upload_ms': _int(res.get('upload_ms')), 'convert_ms': _int(res.get('convert_ms')), 'total_ms': _int(res.get('total_ms'))}
        return jsonify({'ok': True, 'transcript': res.get('text'), 'timings': timings, 'raw': res})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/stt/cloud', methods=['POST'])
def api_stt_cloud():
    if not llm_api.OPENAI_KEY:
        return jsonify({'ok': False, 'error': 'OPENAI_API_KEY not set'}), 500
    if 'audio' in request.files:
        audio_bytes = request.files['audio'].read()
        filename = request.files['audio'].filename or 'audio.webm'
    else:
        audio_bytes = request.get_data() or b''
        filename = 'audio.webm'
    if not audio_bytes:
        return jsonify({'ok': False, 'error': 'no audio provided'}), 400
    language = request.args.get('lang', 'en')
    try:
        t_start = time.time()
        files = {'file': (filename, audio_bytes, 'application/octet-stream')}
        data = {'model': 'whisper-1', 'language': language}
        headers = {'Authorization': f'Bearer {llm_api.OPENAI_KEY}'}
        t_upload_start = time.time()
        resp = requests.post('https://api.openai.com/v1/audio/transcriptions', headers=headers, files=files, data=data, timeout=60)
        t_upload_end = time.time()
        resp.raise_for_status()
        j = resp.json()
        text = j.get('text') or j.get('transcript') or ''
        upload_ms = int((t_upload_end - t_upload_start) * 1000)
        inference_ms = int((time.time() - t_upload_end) * 1000)
        total_ms = int((time.time() - t_start) * 1000)
        return jsonify({'ok': True, 'transcript': text, 'timings': {'upload_ms': upload_ms, 'inference_ms': inference_ms, 'total_ms': total_ms}, 'raw': j})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/tts', methods=['POST'])
def api_tts_start():
    data = request.get_json(force=True) or {}
    text = (data.get('text') or '')
    if not text:
        return jsonify({'ok': False, 'error': 'no text provided'}), 400
    voice = data.get('voice')
    job_id = uuid.uuid4().hex
    with tts._TTS_JOB_LOCK:
        tts.JOBS[job_id] = {'text': text, 'created_at': time.time(), 'status': {'local': 'pending' if tts.local_tts_provider else 'disabled', 'cloud': 'pending' if tts.cloud_tts_provider else 'disabled'}, 'timings': {}}
    try:
        tts._purge_old_job_files(except_job_id=job_id)
        tts._start_tts_background(job_id, text, voice=voice)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    return jsonify({'ok': True, 'job_id': job_id, 'stream_url': f"/api/tts/stream?job_id={job_id}", 'files': {'local': f"/api/tts/file/{job_id}?source=local", 'cloud': f"/api/tts/file/{job_id}?source=cloud"}})


@app.route('/api/tts/stream', methods=['GET'])
def api_tts_stream():
    job_id = request.args.get('job_id')
    if not job_id:
        return jsonify({'ok': False, 'error': 'job_id required'}), 400
    timeout = float(request.args.get('timeout') or 30.0)
    start = time.time()
    while True:
        with tts._TTS_JOB_LOCK:
            job = tts.JOBS.get(job_id)
            if not job:
                return jsonify({'ok': False, 'error': 'unknown job_id'}), 404
            local_b = job.get('local')
            cloud_b = job.get('cloud')
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
    source = (request.args.get('source') or 'local').lower()
    if source not in {'local', 'cloud'}:
        return jsonify({'ok': False, 'error': 'source must be local or cloud'}), 400
    with tts._TTS_JOB_LOCK:
        job = tts.JOBS.get(job_id)
        if not job:
            return jsonify({'ok': False, 'error': 'unknown job_id'}), 404
        path = job.get(f"{source}_path")
        data = job.get(source) or job.get(f"{source}_bytes")
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
    job_id = request.args.get('job_id')
    if not job_id:
        return jsonify({'ok': False, 'error': 'job_id required'}), 400
    with tts._TTS_JOB_LOCK:
        job = tts.JOBS.get(job_id)
        if not job:
            return jsonify({'ok': False, 'error': 'unknown job_id'}), 404
        out = {'job_id': job_id, 'text': job.get('text'), 'created_at': job.get('created_at'), 'status': job.get('status', {}), 'timings': job.get('timings', {}), 'local_path': job.get('local_path'), 'cloud_path': job.get('cloud_path')}
    return jsonify({'ok': True, 'job': out})


@app.route('/api/tts/stream_sentences', methods=['POST'])
def api_tts_stream_sentences():
    try:
        body = request.get_json(force=True) or {}
    except Exception:
        body = {}
    text = (body.get('text') or '').strip()
    source = (body.get('source') or 'local').lower()
    voice = body.get('voice') or 'alloy'
    if not text:
        def bad():
            yield f"event: app_error\ndata: {json.dumps({'message': 'Text is empty'})}\n\n"
            yield "event: done\ndata: {}\n\n"
        return Response(stream_with_context(bad()), mimetype='text/event-stream')

    headers = {'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'}

    def gen():
        run_id = uuid.uuid4().hex
        tts._purge_old_job_files(except_job_id=run_id)
        parts = []
        t_start = time.perf_counter()
        sentences, remainder = tts._extract_sentences(text)
        if remainder.strip():
            sentences.append(remainder.strip())
        if not sentences:
            yield f"event: app_error\ndata: {json.dumps({'message': 'No sentences to synthesize'})}\n\n"
            yield "event: done\ndata: {}\n\n"
            return
        for idx, sentence in enumerate(sentences):
            if not sentence.strip():
                continue
            a0 = time.perf_counter()
            try:
                if source == 'local':
                    if not tts.local_tts_provider:
                        raise RuntimeError('local TTS provider not configured')
                    audio_bytes = tts.local_tts_provider.synthesize_speech(sentence, voice)
                elif source == 'cloud':
                    if not tts.cloud_tts_provider:
                        raise RuntimeError('cloud TTS provider not configured')
                    audio_bytes = tts.cloud_tts_provider.synthesize_speech(sentence, voice)
                else:
                    def call_local():
                        if not tts.local_tts_provider:
                            raise RuntimeError('local TTS provider not configured')
                        return tts.local_tts_provider.synthesize_speech(sentence, voice)
                    def call_cloud():
                        if not tts.cloud_tts_provider:
                            raise RuntimeError('cloud TTS provider not configured')
                        return tts.cloud_tts_provider.synthesize_speech(sentence, voice)
                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                        futures = {ex.submit(call_local): 'local', ex.submit(call_cloud): 'cloud'}
                        audio_bytes = None
                        for f in concurrent.futures.as_completed(futures):
                            try:
                                audio_bytes = f.result()
                                break
                            except Exception:
                                continue
                a1 = time.perf_counter()
            except Exception as e:
                yield f"event: app_error\ndata: {json.dumps({'message': f'TTS failed for sentence {idx}: {repr(e)}'})}\n\n"
                continue
            if not audio_bytes:
                yield f"event: app_error\ndata: {json.dumps({'message': f'TTS returned empty audio for sentence {idx}'})}\n\n"
                continue
            parts.append(bytes(audio_bytes))
            try:
                chunk_path = tts._save_chunk_audio(run_id, idx, source, audio_bytes)
                with tts._TTS_JOB_LOCK:
                    job = tts.JOBS.get(run_id) or {}
                    job.setdefault('chunks', []).append(chunk_path)
                    tts.JOBS[run_id] = job
            except Exception:
                pass
            audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')
            yield f"event: sentence\ndata: {json.dumps({'index': idx, 'audio_data': audio_b64, 'mime_type': 'audio/mpeg', 'text': sentence, 'tts_ms': (a1-a0)*1000.0})}\n\n"

        full = b''.join(parts)
        try:
            with tts._TTS_JOB_LOCK:
                tts.JOBS[run_id] = {'text': text, 'created_at': time.time()}
            save_path = tts._save_job_audio(run_id, source, full)
            with tts._TTS_JOB_LOCK:
                tts.JOBS[run_id][f"{source}_path"] = save_path
                tts.JOBS[run_id][f"{source}_bytes"] = full
                tts.JOBS[run_id][source] = full
                tts.JOBS[run_id]['status'] = {source: 'done'}
        except Exception:
            yield f"event: app_error\ndata: {json.dumps({'message': 'Failed to persist final audio'})}\n\n"

        yield f"event: final_audio\ndata: {json.dumps({'url': f'/api/tts/file/{run_id}?source={source}'})}\n\n"
        try:
            audio_b64_full = base64.b64encode(full).decode('utf-8')
            yield f"event: final_audio_bytes\ndata: {json.dumps({'audio_data': audio_b64_full, 'mime_type': 'audio/mpeg'})}\n\n"
        except Exception:
            pass
        yield f"event: tts_metrics\ndata: {json.dumps({'ttfa_ms': None, 'tts_total_ms': int((time.perf_counter()-t_start)*1000.0), 'sentences': len(sentences)})}\n\n"
        yield "event: done\ndata: {}\n\n"

    return Response(stream_with_context(gen()), mimetype='text/event-stream', headers=headers)


if __name__ == '__main__':
    # Allow quick local start: `python backend/web.py`
    app.run(host='127.0.0.1', port=5000, debug=True)
