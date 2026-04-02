from flask import Flask, send_from_directory, request, jsonify, Response, stream_with_context
from pathlib import Path
import os, time, base64, uuid, queue, threading, requests, concurrent.futures, json
from typing import List

# Prefer package-style relative imports, but allow running `python web.py`
try:
    from . import state, tts, llm_api, weather
except Exception:
    import state, tts, llm_api, weather

app = Flask(__name__, static_folder="../frontend", static_url_path="")
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# Application settings
SETTINGS = {
    'filler_mode': 'auto'  # Options: 'on', 'off', 'auto'
}
SETTINGS_LOCK = threading.Lock()

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


@app.get('/api/settings')
def get_settings():
    """Get current application settings"""
    with SETTINGS_LOCK:
        return jsonify(SETTINGS.copy())


@app.post('/api/settings/filler-mode')
def set_filler_mode():
    """Set filler mode: 'on', 'off', or 'auto'"""
    data = request.get_json() or {}
    mode = data.get('mode', '').lower()
    
    if mode not in ['on', 'off', 'auto']:
        return jsonify({'ok': False, 'error': 'Invalid mode. Must be on, off, or auto'}), 400
    
    with SETTINGS_LOCK:
        SETTINGS['filler_mode'] = mode
    
    return jsonify({'ok': True, 'filler_mode': mode})


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
    # Accept optional history as JSON-encoded query parameter
    history_param = request.args.get('history', '')
    if history_param:
        try:
            history = json.loads(history_param)
            # Append current user message if not already in history
            if user and (not history or history[-1].get('content') != user):
                history.append({'role': 'user', 'content': user})
        except Exception:
            history = [{'role': 'user', 'content': user}] if user else []
    else:
        history = [{'role': 'user', 'content': user}] if user else []
    
    # Determine if we have previous conversation context (more than just the current message)
    has_context = len(history) > 1

    def generate():
        # Stream tokens from both models concurrently and process deltas as they arrive.
        start = time.time()
        stream_id = uuid.uuid4().hex
        q = queue.Queue()
        done = {'local': False, 'cloud': False}

        def stream_worker_local():
            who = 'local'
            try:
                if not getattr(llm_api, 'local_llm', None):
                    q.put({'who': who, 'type': 'error', 'error': 'no local LLM configured'})
                    done[who] = True
                    return
                # build messages with dynamic system prompt based on filler mode
                with SETTINGS_LOCK:
                    filler_mode = SETTINGS.get('filler_mode', 'auto')
                sys_prompt = llm_api.local_llm.get_system_prompt(filler_mode) if hasattr(llm_api.local_llm, 'get_system_prompt') else (llm_api.local_llm.SYSTEM_PROMPT if hasattr(llm_api.local_llm, 'SYSTEM_PROMPT') else '')
                
                # Build messages with explicit context marking if history exists
                if has_context and len(history) > 1:
                    # Add context marker after system prompt
                    context_msgs = history[-getattr(llm_api.local_llm, 'MAX_HISTORY', 8):-1]  # All but current message
                    current_msg = history[-1:]  # Just the current message
                    
                    # Create enhanced system prompt with context indicator
                    enhanced_prompt = sys_prompt + "\n\nNote: The following conversation history is provided for context. The user's current request is at the end."
                    
                    msgs = [{'role': 'system', 'content': enhanced_prompt}] + context_msgs + current_msg
                else:
                    msgs = [{'role': 'system', 'content': sys_prompt}] + (history[-getattr(llm_api.local_llm, 'MAX_HISTORY', 8):] if history else [])
                
                payload = {'model': getattr(llm_api.local_llm, 'MODEL', None) or 'local', 'messages': msgs, 'stream': True, 'temperature': 0}
                url = getattr(llm_api.local_llm, 'LLM_SERVER_URL', None)
                if not url:
                    q.put({'who': who, 'type': 'error', 'error': 'local LLM URL not configured'})
                    done[who] = True
                    return
                resp = requests.post(url, json=payload, timeout=(5, 60), stream=True)
                buffer = ''
                accumulated = ''
                t0 = time.time()
                for line in resp.iter_lines(decode_unicode=True):
                    if line is None:
                        continue
                    s = line.strip()
                    if not s:
                        continue
                    # Attempt to extract JSON from standard 'data: {...}' lines
                    if s.startswith('data:'):
                        s = s[len('data:'):].strip()
                    if s == '[DONE]':
                        break
                    try:
                        obj = json.loads(s)
                    except Exception:
                        continue
                    # OpenAI-like shape: choices[].delta.content
                    try:
                        delta = obj.get('choices', [])[0].get('delta', {}).get('content', '')
                    except Exception:
                        delta = ''
                    if not delta:
                        continue
                    accumulated += str(delta)
                    q.put({'who': who, 'type': 'delta', 'text': delta, 'ts': int((time.time()-start)*1000), 'stream_id': stream_id})
                # final full content attempt
                q.put({'who': who, 'type': 'done', 'content': accumulated, 'ms': int((time.time()-start)*1000)})
            except Exception as e:
                q.put({'who': who, 'type': 'error', 'error': str(e)})
            finally:
                done['local'] = True

        def stream_worker_cloud():
            who = 'cloud'
            try:
                if not getattr(llm_api, 'OPENAI_KEY', None):
                    q.put({'who': who, 'type': 'error', 'error': 'OPENAI_API_KEY not set'})
                    done[who] = True
                    return
                # construct messages with dynamic system prompt based on filler mode
                with SETTINGS_LOCK:
                    filler_mode = SETTINGS.get('filler_mode', 'auto')
                llm_module = llm_api.llm_helper if getattr(llm_api, 'llm_helper', None) else llm_api.local_llm
                sys_prompt = llm_module.get_system_prompt(filler_mode) if hasattr(llm_module, 'get_system_prompt') else (llm_module.SYSTEM_PROMPT if hasattr(llm_module, 'SYSTEM_PROMPT') else '')
                
                # Build messages with explicit context marking if history exists
                max_hist = llm_api.llm_helper.MAX_HISTORY if getattr(llm_api, 'llm_helper', None) and hasattr(llm_api.llm_helper, 'MAX_HISTORY') else (llm_api.local_llm.MAX_HISTORY if getattr(llm_api, 'local_llm', None) and hasattr(llm_api.local_llm, 'MAX_HISTORY') else 8)
                
                if has_context and len(history) > 1:
                    # Add context marker after system prompt
                    context_msgs = history[-max_hist:-1]  # All but current message
                    current_msg = history[-1:]  # Just the current message
                    
                    # Create enhanced system prompt with context indicator
                    enhanced_prompt = sys_prompt + "\n\nNote: The following conversation history is provided for context. The user's current request is at the end."
                    
                    msgs = [{'role': 'system', 'content': enhanced_prompt}] + context_msgs + current_msg
                else:
                    msgs = [{'role': 'system', 'content': sys_prompt}] + (history[-max_hist:] if history else [])
                
                payload = {'model': 'gpt-5.2', 'messages': msgs, 'temperature': 0, 'stream': True}
                headers = {'Authorization': f'Bearer {llm_api.OPENAI_KEY}', 'Content-Type': 'application/json'}
                resp = requests.post(llm_api.OPENAI_URL, json=payload, headers=headers, timeout=(5, 60), stream=True)
                accumulated = ''
                for line in resp.iter_lines(decode_unicode=True):
                    if line is None:
                        continue
                    s = line.strip()
                    if not s:
                        continue
                    if s.startswith('data:'):
                        s = s[len('data:'):].strip()
                    if s == '[DONE]':
                        break
                    try:
                        obj = json.loads(s)
                    except Exception:
                        continue
                    try:
                        delta = obj.get('choices', [])[0].get('delta', {}).get('content', '')
                    except Exception:
                        delta = ''
                    if not delta:
                        continue
                    accumulated += str(delta)
                    q.put({'who': who, 'type': 'delta', 'text': delta, 'ts': int((time.time()-start)*1000), 'stream_id': stream_id})
                q.put({'who': who, 'type': 'done', 'content': accumulated, 'ms': int((time.time()-start)*1000)})
            except Exception as e:
                q.put({'who': who, 'type': 'error', 'error': str(e)})
            finally:
                done['cloud'] = True

        # start both workers simultaneously (if configured)
        threads = []
        t_local = threading.Thread(target=stream_worker_local, daemon=True)
        t_cloud = threading.Thread(target=stream_worker_cloud, daemon=True)
        threads.append(t_local)
        threads.append(t_cloud)
        t_local.start()
        t_cloud.start()

        # buffers per model to accumulate until punctuation
        buffers = {'local': '', 'cloud': ''}
        run_ids = {'local': None, 'cloud': None}
        # toolcall collection state per model (bracket-aware, handles strings/escapes)
        tool_state = {
            'local': {'collecting': False, 'buffer': '', 'depth': 0, 'in_string': False, 'escape': False},
            'cloud': {'collecting': False, 'buffer': '', 'depth': 0, 'in_string': False, 'escape': False}
        }
        # executor for background TTS synthesis so we don't block token consumption
        synth_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        pending_futures: List[concurrent.futures.Future] = []
        synth_lock = threading.Lock()
        # per-who monotonic chunk counters to ensure indices increase
        chunk_counters = {'local': 0, 'cloud': 0}
        # expected next index to emit per who, and pending storage for out-of-order results
        expected_idx = {'local': 0, 'cloud': 0}
        pending_sentences = {'local': {}, 'cloud': {}}

        def _process_toolcall_chunk(who, delta_text):
            """Process an incoming token delta for potential toolcall JSON.
            Returns the text that should be spoken (delta with toolcall JSON removed).
            If a complete JSON toolcall is found, schedules apply_toolcall in background
            and enqueues a 'tool_result' item into the main queue when ready.
            """
            out_chars = []
            st = tool_state.get(who)
            # iterate characters to correctly track strings/escapes and brace depth
            i = 0
            L = len(delta_text)
            while i < L:
                ch = delta_text[i]
                if not st['collecting']:
                    if ch == '{':
                        # start collecting
                        st['collecting'] = True
                        st['buffer'] = ch
                        st['depth'] = 1
                        st['in_string'] = False
                        st['escape'] = False
                    else:
                        out_chars.append(ch)
                else:
                    st['buffer'] += ch
                    # handle escape within string
                    if st['in_string']:
                        if st['escape']:
                            st['escape'] = False
                        elif ch == '\\':
                            st['escape'] = True
                        elif ch == '"':
                            st['in_string'] = False
                    else:
                        if ch == '"':
                            st['in_string'] = True
                        elif ch == '{':
                            st['depth'] += 1
                        elif ch == '}':
                            st['depth'] -= 1
                            if st['depth'] <= 0:
                                # complete JSON candidate
                                candidate = st['buffer']
                                # reset state before scheduling to avoid reentrancy
                                st['collecting'] = False
                                st['buffer'] = ''
                                st['depth'] = 0
                                st['in_string'] = False
                                st['escape'] = False
                                # try to parse JSON
                                parsed = None
                                try:
                                    parsed = json.loads(candidate)
                                except Exception:
                                    parsed = None
                                if isinstance(parsed, dict) and parsed.get('tool'):
                                    # schedule apply_toolcall in background thread
                                    def _apply_and_enqueue(parsed_obj, target_who, sid):
                                        try:
                                            t0 = time.time()
                                            last_text = history[-1]['content'] if history else (user or '')
                                            
                                            # Check for filler and emit it immediately before executing tool
                                            filler = parsed_obj.get('filler', '').strip()
                                            if filler:
                                                q.put({'who': target_who, 'type': 'delta', 'text': filler, 'ts': int((time.time()-t0)*1000), 'stream_id': sid, 'is_filler': True})
                                            
                                            # Special handling for weather tool - stream LLM summary instead of returning raw data
                                            if parsed_obj.get('tool') == 'get_weather':
                                                try:
                                                    # First emit the tool call event
                                                    q.put({'who': target_who, 'type': 'tool_call', 'parsed': parsed_obj, 'stream_id': sid})
                                                    
                                                    # Get weather data
                                                    weather_data = weather.get_current_weather()
                                                    
                                                    if weather_data.get('ok'):
                                                        # Stream the weather summary from dedicated LLM
                                                        accumulated = ''
                                                        for token in weather.stream_weather_summary(weather_data, last_text):
                                                            accumulated += token
                                                            q.put({'who': target_who, 'type': 'delta', 'text': token, 'ts': int((time.time()-t0)*1000), 'stream_id': sid})
                                                        
                                                        # Send completion marker with empty applied to prevent re-summarization
                                                        # (content was already streamed as deltas and processed by TTS)
                                                        t1 = time.time()
                                                        result_summary = {'ok': True, 'message': accumulated, 'weather': weather_data}
                                                        q.put({'who': target_who, 'type': 'tool_result', 'result': result_summary, 'parsed': parsed_obj, 'ms': int((t1-t0)*1000), 'stream_id': sid})
                                                    else:
                                                        # Weather fetch failed, return error
                                                        t1 = time.time()
                                                        q.put({'who': target_who, 'type': 'tool_result', 'result': weather_data, 'parsed': parsed_obj, 'ms': int((t1-t0)*1000), 'stream_id': sid})
                                                except Exception as e:
                                                    q.put({'who': target_who, 'type': 'tool_result', 'result': {'ok': False, 'error': f'Weather streaming error: {str(e)}'}, 'parsed': parsed_obj, 'ms': 0, 'stream_id': sid})
                                            else:
                                                # Standard tool call handling for non-weather tools
                                                res = llm_api.apply_toolcall(parsed_obj, target=target_who, last_user_text=last_text)
                                                t1 = time.time()
                                                q.put({'who': target_who, 'type': 'tool_result', 'result': res, 'parsed': parsed_obj, 'ms': int((t1-t0)*1000), 'stream_id': sid})
                                        except Exception as e:
                                            try:
                                                q.put({'who': target_who, 'type': 'tool_result', 'result': {'ok': False, 'error': str(e)}, 'parsed': parsed_obj, 'ms': 0, 'stream_id': sid})
                                            except Exception:
                                                pass
                                    thr = threading.Thread(target=_apply_and_enqueue, args=(parsed, who, stream_id), daemon=True)
                                    thr.start()
                                else:
                                    # not valid toolcall JSON — treat candidate as spoken text
                                    out_chars.append(candidate)
                    # end collecting branch
                i += 1
            return ''.join(out_chars)

        # consume queued token deltas and emit SSE events; finish when both done
        while True:
            try:
                item = q.get(timeout=30)
            except Exception:
                # if both workers finished, queue empty, and no pending synths, break
                with synth_lock:
                    no_pending = (len(pending_futures) == 0)
                if done.get('local') and done.get('cloud') and q.empty() and no_pending:
                    break
                else:
                    yield ': ping\n\n'
                    continue

            who = item.get('who')

            if item.get('type') == 'error':
                payload = {'model': who, 'ok': False, 'error': item.get('error'), 'ms': int((time.time()-start)*1000), 'stream_id': item.get('stream_id') or stream_id}
                yield f"event: model\ndata: {json.dumps(payload)}\n\n"
                continue

            if item.get('type') == 'delta':
                # emit original model_text fragment for backwards compatibility
                delta = str(item.get('text') or '')
                is_filler = item.get('is_filler', False)
                try:
                    yield f"event: model_text\ndata: {json.dumps({'model': who, 'index': None, 'text': delta, 'ms': item.get('ts'), 'stream_id': item.get('stream_id') or stream_id})}\n\n"
                except Exception:
                    pass
                
                # For filler responses, bypass toolcall processing and treat as complete sentence
                # Queue immediately for TTS to ensure it plays before tool results
                if is_filler:
                    # Ensure filler ends with period for proper sentence detection
                    filler_text = delta.strip()
                    if not filler_text.endswith(('.', '!', '?')):
                        filler_text += '.'
                    
                    # Directly queue filler for TTS without buffering
                    run_id = run_ids.get(who) or (uuid.uuid4().hex)
                    run_ids[who] = run_id
                    idx = chunk_counters.get(who, 0)
                    chunk_counters[who] = idx + 1

                    def _synth_filler(s=filler_text, w=who, rid=run_id, idx=idx, sid=stream_id):
                        tts_start = time.perf_counter()
                        if w == 'cloud':
                            if not tts.cloud_tts_provider:
                                raise RuntimeError('cloud TTS provider not configured')
                            audio = tts.cloud_tts_provider.synthesize_speech(s, None)
                            src = 'cloud'
                        else:
                            if not tts.local_tts_provider:
                                raise RuntimeError('local TTS provider not configured')
                            audio = tts.local_tts_provider.synthesize_speech(s, None)
                            src = 'local'
                        tts_end = time.perf_counter()
                        tts_ms = (tts_end - tts_start) * 1000.0
                        try:
                            path = tts._save_chunk_audio(rid, idx, src, audio)
                            with tts._TTS_JOB_LOCK:
                                job = tts.JOBS.get(rid) or {}
                                job.setdefault('chunks', []).append(path)
                                tts.JOBS[rid] = job
                        except Exception:
                            pass
                        return {'who': w, 'run_id': rid, 'index': idx, 'audio': bytes(audio), 'source': src, 'tts_ms': tts_ms, 'text': s, 'stream_id': sid}

                    # Submit filler synthesis with high priority (process immediately)
                    fut = synth_executor.submit(_synth_filler)
                    
                    def _on_filler_done(futobj, w=who):
                        try:
                            res = None
                            try:
                                res = futobj.result()
                            except Exception as e:
                                q.put({'who': w, 'type': 'synth_error', 'error': str(e), 'stream_id': stream_id})
                                return
                            if res:
                                try:
                                    with synth_lock:
                                        try:
                                            pending_futures.remove(futobj)
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                                q.put({'who': w, 'type': 'synth', 'res': res, 'stream_id': stream_id})
                        except Exception as e:
                            pass
                    
                    fut.add_done_callback(_on_filler_done)
                    with synth_lock:
                        pending_futures.append(fut)
                    
                    # Don't add filler to buffer - it's been handled directly
                    # Skip to next queue item
                    continue
                
                # Normal delta processing (not a filler)
                # process delta for toolcall JSON and append spoken text only
                try:
                    speak_delta = _process_toolcall_chunk(who, delta)
                except Exception:
                    speak_delta = delta
                if speak_delta:
                    buffers[who] += speak_delta
                
                # check for sentences in buffer
                sentences, remainder = tts._extract_sentences(buffers[who])
                if sentences:
                    for s_idx, sent in enumerate(sentences):
                        run_id = run_ids.get(who) or (uuid.uuid4().hex)
                        run_ids[who] = run_id
                        # allocate a monotonic index for this chunk
                        idx = chunk_counters.get(who, 0)
                        chunk_counters[who] = idx + 1

                        def _synth_and_save(s=sent, w=who, rid=run_id, idx=idx, sid=stream_id):
                            tts_start = time.perf_counter()
                            if w == 'cloud':
                                if not tts.cloud_tts_provider:
                                    raise RuntimeError('cloud TTS provider not configured')
                                audio = tts.cloud_tts_provider.synthesize_speech(s, None)
                                src = 'cloud'
                            else:
                                if not tts.local_tts_provider:
                                    raise RuntimeError('local TTS provider not configured')
                                audio = tts.local_tts_provider.synthesize_speech(s, None)
                                src = 'local'
                            tts_end = time.perf_counter()
                            tts_ms = (tts_end - tts_start) * 1000.0
                            try:
                                path = tts._save_chunk_audio(rid, idx, src, audio)
                                with tts._TTS_JOB_LOCK:
                                    job = tts.JOBS.get(rid) or {}
                                    job.setdefault('chunks', []).append(path)
                                    tts.JOBS[rid] = job
                            except Exception:
                                pass
                            return {'who': w, 'run_id': rid, 'index': idx, 'audio': bytes(audio), 'source': src, 'tts_ms': tts_ms, 'text': s, 'stream_id': sid}

                        # submit synthesis to background executor so we keep consuming tokens
                        fut = synth_executor.submit(_synth_and_save)
                        # attach callback to push completed synth results into the central queue
                        def _on_done(futobj, w=who):
                            try:
                                res = None
                                try:
                                    res = futobj.result()
                                except Exception as e:
                                    q.put({'who': w, 'type': 'synth_error', 'error': str(e), 'stream_id': stream_id})
                                    return
                                if res:
                                    # remove from pending list
                                    try:
                                        with synth_lock:
                                            try:
                                                pending_futures.remove(futobj)
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass
                                    q.put({'who': w, 'type': 'synth', 'res': res, 'stream_id': stream_id})
                            except Exception:
                                try:
                                    q.put({'who': w, 'type': 'synth_error', 'error': 'synth callback error', 'stream_id': stream_id})
                                except Exception:
                                    pass

                        fut.add_done_callback(_on_done)
                        with synth_lock:
                            pending_futures.append(fut)
                    # notify clients that a TTS task was queued for this stream
                    try:
                        yield f"event: tts_queued\ndata: {json.dumps({'model': who, 'stream_id': stream_id, 'index': idx})}\n\n"
                    except Exception:
                        pass
                    buffers[who] = remainder

            # synth completions are pushed into the queue by future callbacks
            # handle completed synth items (sent by _on_done)
            if item.get('type') == 'synth':
                try:
                    res = item.get('res') or {}
                    who = item.get('who') or res.get('who')
                    idx = res.get('index')
                    if who is None or idx is None:
                        # fallback to immediate emit if metadata missing
                        if res and res.get('audio'):
                            try:
                                audio_b64 = base64.b64encode(res.get('audio')).decode('utf-8')
                                yield f"event: sentence\ndata: {json.dumps({'index': res.get('index'), 'audio_data': audio_b64, 'mime_type': 'audio/mpeg', 'text': res.get('text'), 'model': res.get('who'), 'source': res.get('source'), 'tts_ms': res.get('tts_ms'), 'stream_id': res.get('stream_id') or stream_id})}\n\n"
                            except Exception:
                                pass
                        continue
                    # store result and attempt in-order emission
                    pending_sentences.setdefault(who, {})[idx] = res
                    # emit while next expected is available
                    while True:
                        nex = expected_idx.get(who, 0)
                        if nex in pending_sentences.get(who, {}):
                            r = pending_sentences[who].pop(nex)
                            try:
                                audio_b64 = base64.b64encode(r.get('audio')).decode('utf-8')
                                yield f"event: sentence\ndata: {json.dumps({'index': r.get('index'), 'audio_data': audio_b64, 'mime_type': 'audio/mpeg', 'text': r.get('text'), 'model': r.get('who'), 'source': r.get('source'), 'tts_ms': r.get('tts_ms'), 'stream_id': r.get('stream_id') or stream_id})}\n\n"
                            except Exception:
                                pass
                            expected_idx[who] = nex + 1
                            continue
                        break
                except Exception:
                    try:
                        yield f"event: app_error\ndata: {json.dumps({'message': 'TTS task failed', 'error': str(item.get('error') or '')})}\n\n"
                    except Exception:
                        pass
                continue
            if item.get('type') == 'synth_error':
                try:
                    yield f"event: app_error\ndata: {json.dumps({'message': 'TTS synth error', 'error': item.get('error'), 'stream_id': item.get('stream_id')})}\n\n"
                except Exception:
                    pass
                continue

            if item.get('type') == 'tool_call':
                try:
                    who = item.get('who')
                    parsed = item.get('parsed')
                    payload = {'model': who, 'tool': parsed.get('tool'), 'params': parsed, 'stream_id': item.get('stream_id')}
                    yield f"event: tool_call\ndata: {json.dumps(payload, default=str)}\n\n"
                except Exception:
                    pass
                continue

            if item.get('type') == 'tool_result':
                try:
                    who = item.get('who')
                    res = item.get('result') or {}
                    parsed = item.get('parsed')
                    # For streaming tools like weather, include the accumulated message as content
                    content = res.get('message', None)
                    # If weather was streamed (has message), use empty applied to prevent re-summarization in frontend
                    applied = {} if res.get('message') else res
                    payload = {'model': who, 'ok': bool(res.get('ok', True)), 'content': content, 'parsed': parsed, 'applied': applied, 'ms': item.get('ms'), 'stream_id': item.get('stream_id')}
                    yield f"event: model\ndata: {json.dumps(payload, default=str)}\n\n"
                    try:
                        # if tool changed state, publish state event
                        state.publish_state_event()
                    except Exception:
                        pass
                except Exception:
                    try:
                        yield f"event: app_error\ndata: {json.dumps({'message': 'Tool result handling failed'})}\n\n"
                    except Exception:
                        pass
                continue

            if item.get('type') == 'done':
                # final model event with full content
                payload = {'model': who, 'ok': True, 'content': item.get('content'), 'parsed': None, 'applied': None, 'ms': item.get('ms'), 'stream_id': item.get('stream_id') or stream_id}
                try:
                    yield f"event: model\ndata: {json.dumps(payload, default=str)}\n\n"
                except Exception:
                    pass
                
                # Flush any remaining buffered text for this model
                # This ensures incomplete sentences are still synthesized
                if who in buffers and buffers[who].strip():
                    remaining_text = buffers[who].strip()
                    run_id = run_ids.get(who) or (uuid.uuid4().hex)
                    run_ids[who] = run_id
                    idx = chunk_counters.get(who, 0)
                    chunk_counters[who] = idx + 1
                    
                    def _synth_final(s=remaining_text, w=who, rid=run_id, idx=idx, sid=stream_id):
                        tts_start = time.perf_counter()
                        if w == 'cloud':
                            if not tts.cloud_tts_provider:
                                raise RuntimeError('cloud TTS provider not configured')
                            audio = tts.cloud_tts_provider.synthesize_speech(s, None)
                            src = 'cloud'
                        else:
                            if not tts.local_tts_provider:
                                raise RuntimeError('local TTS provider not configured')
                            audio = tts.local_tts_provider.synthesize_speech(s, None)
                            src = 'local'
                        tts_end = time.perf_counter()
                        tts_ms = (tts_end - tts_start) * 1000.0
                        try:
                            path = tts._save_chunk_audio(rid, idx, src, audio)
                            with tts._TTS_JOB_LOCK:
                                job = tts.JOBS.get(rid) or {}
                                job.setdefault('chunks', []).append(path)
                                tts.JOBS[rid] = job
                        except Exception:
                            pass
                        return {'who': w, 'run_id': rid, 'index': idx, 'audio': bytes(audio), 'source': src, 'tts_ms': tts_ms, 'text': s, 'stream_id': sid}
                    
                    fut = synth_executor.submit(_synth_final)
                    
                    def _on_final_done(futobj, w=who):
                        try:
                            res = futobj.result()
                            if res:
                                try:
                                    with synth_lock:
                                        try:
                                            pending_futures.remove(futobj)
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                                q.put({'who': w, 'type': 'synth', 'res': res, 'stream_id': stream_id})
                        except Exception:
                            pass
                    
                    fut.add_done_callback(_on_final_done)
                    with synth_lock:
                        pending_futures.append(fut)
                    
                    buffers[who] = ''  # Clear the buffer after flushing

        # ensure workers have finished and clean up executor
        try:
            synth_executor.shutdown(wait=False)
        except Exception:
            pass

        for t in threads:
            try:
                t.join(timeout=0.1)
            except Exception:
                pass

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

    # Heuristic: if the apply result indicates many rooms had their lights toggled,
    # prefer a concise, spoken summary rather than asking the LLM to rewrite.
    try:
        app_obj = applied
        if isinstance(applied, str):
            try:
                app_obj = json.loads(applied)
            except Exception:
                app_obj = applied
        if isinstance(app_obj, list) and len(app_obj) >= 3:
            # collect new_state values and detect whether this was a light action
            new_states = set()
            is_light_action = 'light' in safe_text.lower()
            for it in app_obj:
                if not isinstance(it, dict):
                    continue
                if not is_light_action:
                    # some apply entries may include device hints
                    dev = it.get('device') or it.get('device_type') or ''
                    if dev and 'light' in str(dev).lower():
                        is_light_action = True
                ns = it.get('new_state') or it.get('state') or it.get('status')
                if ns is not None:
                    try:
                        new_states.add(str(ns).lower())
                    except Exception:
                        pass
            if is_light_action and len(new_states) == 1 and list(new_states)[0] in ('on', 'off'):
                verb = 'turned on' if list(new_states)[0] == 'on' else 'turned off'
                return jsonify({'ok': True, 'summary': f'I have {verb} all the lights'})
    except Exception:
        pass

    system_msg = (
        "You are a concise assistant that rewrites an assistant's output into a single natural, "
        "spoken-English sentence suitable for playback by a TTS system. Keep it under 20 words.\n\n"
        "Return only the spoken sentence (no quotes or extra commentary).\n\n"
        "Examples:\n"
        "Assistant content: Done — I've turned on the light in the Living Room, the Dining Room, the Kitchen.\n"
        "Applied: [{\"room\":\"living room\",\"new_state\":\"on\"},{\"room\":\"dining room\",\"new_state\":\"on\"},{\"room\":\"kitchen\",\"new_state\":\"on\"}]\n"
        "Spoken summary: I have turned on the lights in the living room, dining room, and kitchen.\n\n"
        "Assistant content: Done — I've turned on the light in the Living Room, the Dining Room, the Kitchen, the Bathroom, the Bedroom, and the Office.\n"
        "Applied: [{\"room\":\"living room\",\"new_state\":\"on\"},{\"room\":\"dining room\",\"new_state\":\"on\"},{\"room\":\"kitchen\",\"new_state\":\"on\"},{\"room\":\"bathroom\",\"new_state\":\"on\"},{\"room\":\"bedroom\",\"new_state\":\"on\"},{\"room\":\"office\",\"new_state\":\"on\"}]\n"
        "Spoken summary: I have turned on all the lights.\n\n"
        "Assistant content: Done — I've turned on the living room light.\n"
        "Applied: {\"room\":\"living room\",\"new_state\":\"on\"}\n"
        "Spoken summary: I have turned on the living room light.\n\n"
        "Assistant content: Done — I've increased the thermostat in the bedroom by 2 degrees.\n"
        "Applied: {\"room\":\"bedroom\",\"device\":\"thermostat\",\"action\":\"increase\",\"value\":\"2\"}\n"
        "Spoken summary: I have increased the thermostat by 2 degrees.\n\n"
        "Assistant content: Done — I've set the thermostat to 22 degrees.\n"
        "Applied: {\"room\":\"upstairs\",\"device\":\"thermostat\",\"action\":\"set_value\",\"value\":\"72\"}\n"
        "Spoken summary: I set the thermostat to 22 degrees.\n\n"
        "Rules: If 'applied' includes 'all' or 4+ rooms, say 'I have turned on all the lights'.\n"
        "When listing rooms, use 'the' before room names and list up to three rooms, joining with commas and 'and'.\n"
        "Keep output short and directly suitable for TTS."
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


@app.route('/api/tts/summarize_stream', methods=['POST'])
def api_tts_summarize_stream():
    try:
        body = request.get_json(force=True) or {}
    except Exception:
        body = {}
    text = body.get('text', '')
    applied = body.get('applied')
    prefer = (body.get('prefer') or '').lower()
    try:
        print(f"[summarize_stream] entry prefer={prefer} text_len={len(str(text))} applied_type={type(applied)} OPENAI_KEY_set={bool(getattr(llm_api, 'OPENAI_KEY', None))} local_llm={bool(getattr(llm_api, 'local_llm', None))}", flush=True)
    except Exception:
        pass

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
    # If there's no assistant text but an apply result is provided, allow
    # the LLM to summarize based on the `applied` data. Only return early
    # when both text and applied are empty.
    if not safe_text and not applied:
        def _empty():
            yield "event: done\ndata: {}\n\n"
        return Response(stream_with_context(_empty()), mimetype='text/event-stream')

    # (No fast-path): proceed to streaming LLM summarization below

    system_msg = (
        "You are a concise assistant that rewrites an assistant's output into a single natural, "
        "spoken-English sentence suitable for playback by a TTS system. Keep it under 20 words.\n\n"
        "Return only the spoken sentence (no quotes or extra commentary).\n\n"
        "Examples:\n"
        "Assistant content: Done — I've turned on the light in the Living Room, the Dining Room, the Kitchen.\n"
        "Applied: [{\"room\":\"living room\",\"new_state\":\"on\"},{\"room\":\"dining room\",\"new_state\":\"on\"},{\"room\":\"kitchen\",\"new_state\":\"on\"}]\n"
        "Spoken summary: I have turned on the lights in the living room, dining room, and kitchen.\n\n"
        "Assistant content: Done — I've turned on the light in the Living Room, the Dining Room, the Kitchen, the Bathroom, the Bedroom, and the Office.\n"
        "Applied: [{\"room\":\"living room\",\"new_state\":\"on\"},{\"room\":\"dining room\",\"new_state\":\"on\"},{\"room\":\"kitchen\",\"new_state\":\"on\"},{\"room\":\"bathroom\",\"new_state\":\"on\"},{\"room\":\"bedroom\",\"new_state\":\"on\"},{\"room\":\"office\",\"new_state\":\"on\"}]\n"
        "Spoken summary: I have turned on all the lights.\n\n"
        "Assistant content: Done — I've turned on the living room light.\n"
        "Applied: {\"room\":\"living room\",\"new_state\":\"on\"}\n"
        "Spoken summary: I have turned on the living room light.\n\n"
        "Assistant content: Done — I've increased the thermostat in the bedroom by 2 degrees.\n"
        "Applied: {\"room\":\"bedroom\",\"device\":\"thermostat\",\"action\":\"increase\",\"value\":\"2\"}\n"
        "Spoken summary: I have increased the thermostat by 2 degrees in the bedroom.\n\n"
        "Assistant content: Done — I've set the thermostat to 72 degrees upstairs.\n"
        "Applied: {\"room\":\"upstairs\",\"device\":\"thermostat\",\"action\":\"set_value\",\"value\":\"72\"}\n"
        "Spoken summary: I set the thermostat to 72 degrees upstairs.\n\n"
        "Rules: If 'applied' includes 'all' or 4+ rooms, say 'I have turned on all the lights'.\n"
        "When listing rooms, use 'the' before room names and list up to three rooms, joining with commas and 'and'.\n"
        "Keep output short and directly suitable for TTS."
    )
    user_msg = f"Assistant content: {safe_text}\nApplied: {json.dumps(applied, default=str)}"

    # choose preferred model order
    if prefer == 'cloud':
        order = ['cloud', 'local']
    elif prefer == 'local':
        order = ['local', 'cloud']
    else:
        order = ['cloud', 'local'] if getattr(llm_api, 'OPENAI_KEY', None) else ['local', 'cloud']

    chosen = None
    for s in order:
        if s == 'cloud' and getattr(llm_api, 'OPENAI_KEY', None):
            chosen = 'cloud'
            break
        if s == 'local' and getattr(llm_api, 'local_llm', None):
            chosen = 'local'
            break
    if not chosen:
        def _no():
            yield f"event: app_error\ndata: {json.dumps({'message': 'No LLM available for summarize_stream'})}\n\n"
            yield "event: done\ndata: {}\n\n"
        return Response(stream_with_context(_no()), mimetype='text/event-stream')
    try:
        print(f"[summarize_stream] chosen={chosen}", flush=True)
    except Exception:
        pass

    headers = {'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'}

    def gen():
        run_id = uuid.uuid4().hex
        parts = []
        parts_lock = threading.Lock()
        idx = 0
        buffer = ''
        stream_id = uuid.uuid4().hex
        src = 'cloud' if chosen == 'cloud' else 'local'
        # executor + queue for non-blocking per-sentence synthesis
        synth_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        synth_done_q = queue.Queue()
        pending_futures = []
        try:
            print(f"[summarize_stream][{run_id}] starting using model={chosen} src={src}", flush=True)
        except Exception:
            pass
        try:
            if chosen == 'cloud':
                msgs = [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': user_msg}]
                payload = {'model': 'gpt-5.2', 'messages': msgs, 'temperature': 0, 'stream': True}
                hdrs = {'Authorization': f'Bearer {llm_api.OPENAI_KEY}', 'Content-Type': 'application/json'}
                try:
                    print(f"[summarize_stream][{run_id}] POSTing to OpenAI...", flush=True)
                except Exception:
                    pass
                resp = requests.post(llm_api.OPENAI_URL, json=payload, headers=hdrs, timeout=(5, 60), stream=True)
                try:
                    print(f"[summarize_stream][{run_id}] got response status={getattr(resp, 'status_code', 'NA')}", flush=True)
                except Exception:
                    pass
                for line in resp.iter_lines(decode_unicode=True):
                    if line is None:
                        continue
                    s = line.strip()
                    if not s:
                        continue
                    if s.startswith('data:'):
                        s = s[len('data:'):].strip()
                    if s == '[DONE]':
                        break
                    try:
                        obj = json.loads(s)
                    except Exception:
                        continue
                    try:
                        delta = obj.get('choices', [])[0].get('delta', {}).get('content', '')
                    except Exception:
                        delta = ''
                    if not delta:
                        continue
                    try:
                        print(f"[summarize_stream][{run_id}] received delta len={len(delta)}", flush=True)
                    except Exception:
                        pass
                    # emit streaming text delta for clients
                    try:
                        yield f"event: model_text\ndata: {json.dumps({'model': 'summarizer', 'text': delta, 'stream_id': stream_id})}\n\n"
                    except Exception:
                        pass
                    buffer += str(delta)
                    # check for complete sentences
                    try:
                        sentences, remainder = tts._extract_sentences(buffer)
                    except Exception:
                        sentences, remainder = [], buffer
                    if sentences:
                        for sent in sentences:
                            try:
                                try:
                                    print(f"[summarize_stream][{run_id}] scheduling synth idx={idx} (src={src})", flush=True)
                                except Exception:
                                    pass

                                def _synth_and_save(sent_text=sent, idx_local=idx, src_local=src, run_id_local=run_id, sid=stream_id):
                                    tts_start = time.perf_counter()
                                    if src_local == 'cloud':
                                        if not tts.cloud_tts_provider:
                                            raise RuntimeError('cloud TTS provider not configured')
                                        audio_local = tts.cloud_tts_provider.synthesize_speech(sent_text, None)
                                    else:
                                        if not tts.local_tts_provider:
                                            raise RuntimeError('local TTS provider not configured')
                                        audio_local = tts.local_tts_provider.synthesize_speech(sent_text, None)
                                    tts_end = time.perf_counter()
                                    tts_ms_local = (tts_end - tts_start) * 1000.0
                                    try:
                                        chunk_path = tts._save_chunk_audio(run_id_local, idx_local, src_local, audio_local)
                                        with tts._TTS_JOB_LOCK:
                                            job = tts.JOBS.get(run_id_local) or {}
                                            job.setdefault('chunks', []).append(chunk_path)
                                            tts.JOBS[run_id_local] = job
                                    except Exception:
                                        pass
                                    b = bytes(audio_local)
                                    with parts_lock:
                                        parts.append(b)
                                    return {'index': idx_local, 'audio': b, 'text': sent_text, 'tts_ms': tts_ms_local, 'source': src_local, 'model': src_local, 'stream_id': sid}

                                fut = synth_executor.submit(_synth_and_save)

                                def _on_done(futobj):
                                    try:
                                        res = futobj.result()
                                        synth_done_q.put({'type': 'sentence', 'res': res})
                                    except Exception as e:
                                        synth_done_q.put({'type': 'synth_error', 'error': str(e)})

                                fut.add_done_callback(_on_done)
                                pending_futures.append(fut)

                                # notify clients that a TTS task was queued for this summary sentence
                                try:
                                    yield f"event: tts_queued\ndata: {json.dumps({'model': src, 'stream_id': stream_id, 'index': idx})}\n\n"
                                except Exception:
                                    pass
                            except Exception as e:
                                try:
                                    yield f"event: app_error\ndata: {json.dumps({'message': 'TTS scheduling failed', 'error': str(e)})}\n\n"
                                except Exception:
                                    pass
                            idx += 1
                        buffer = remainder
                    # drain any completed synths and yield sentence events
                    while True:
                        try:
                            done_item = synth_done_q.get_nowait()
                        except Exception:
                            break
                        if done_item.get('type') == 'sentence':
                            r = done_item.get('res') or {}
                            try:
                                audio_b64 = base64.b64encode(r.get('audio')).decode('utf-8')
                                yield f"event: sentence\ndata: {json.dumps({'index': r.get('index'), 'audio_data': audio_b64, 'mime_type': 'audio/mpeg', 'text': r.get('text'), 'tts_ms': r.get('tts_ms'), 'source': r.get('source'), 'model': r.get('model'), 'stream_id': r.get('stream_id')})}\n\n"
                            except Exception:
                                pass
                        elif done_item.get('type') == 'synth_error':
                            try:
                                yield f"event: app_error\ndata: {json.dumps({'message': 'TTS synth error', 'error': done_item.get('error')})}\n\n"
                            except Exception:
                                pass

                # final model event with full content
                try:
                    yield f"event: model\ndata: {json.dumps({'model': 'summarizer', 'ok': True, 'content': buffer, 'stream_id': stream_id})}\n\n"
                except Exception:
                    pass

            else:
                # local streaming LLM (similar to local worker in chat stream)
                if not getattr(llm_api, 'local_llm', None):
                    yield f"event: app_error\ndata: {json.dumps({'message': 'local LLM not configured'})}\n\n"
                else:
                    # Use the summarizer-specific system prompt (not the generic assistant prompt)
                    msgs = [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': user_msg}]
                    payload = {'model': getattr(llm_api.local_llm, 'MODEL', None) or 'local', 'messages': msgs, 'stream': True, 'temperature': 0}
                    url = getattr(llm_api.local_llm, 'LLM_SERVER_URL', None)
                    if not url:
                        yield f"event: app_error\ndata: {json.dumps({'message': 'local LLM URL not configured'})}\n\n"
                    else:
                        resp = requests.post(url, json=payload, timeout=(5, 60), stream=True)
                        for line in resp.iter_lines(decode_unicode=True):
                            if line is None:
                                continue
                            s = line.strip()
                            if not s:
                                continue
                            if s.startswith('data:'):
                                s = s[len('data:'):].strip()
                            if s == '[DONE]':
                                break
                            try:
                                obj = json.loads(s)
                            except Exception:
                                continue
                            try:
                                delta = obj.get('choices', [])[0].get('delta', {}).get('content', '')
                            except Exception:
                                delta = ''
                            if not delta:
                                continue
                            try:
                                yield f"event: model_text\ndata: {json.dumps({'model': 'summarizer', 'text': delta, 'stream_id': stream_id})}\n\n"
                            except Exception:
                                pass
                            buffer += str(delta)
                            try:
                                sentences, remainder = tts._extract_sentences(buffer)
                            except Exception:
                                sentences, remainder = [], buffer
                            if sentences:
                                for sent in sentences:
                                    try:
                                        try:
                                            print(f"[summarize_stream][{run_id}] scheduling synth idx={idx} (src={src})", flush=True)
                                        except Exception:
                                            pass

                                        def _synth_and_save_local(sent_text=sent, idx_local=idx, src_local=src, run_id_local=run_id, sid=stream_id):
                                            tts_start = time.perf_counter()
                                            if src_local == 'cloud':
                                                if not tts.cloud_tts_provider:
                                                    raise RuntimeError('cloud TTS provider not configured')
                                                audio_local = tts.cloud_tts_provider.synthesize_speech(sent_text, None)
                                            else:
                                                if not tts.local_tts_provider:
                                                    raise RuntimeError('local TTS provider not configured')
                                                audio_local = tts.local_tts_provider.synthesize_speech(sent_text, None)
                                            tts_end = time.perf_counter()
                                            tts_ms_local = (tts_end - tts_start) * 1000.0
                                            try:
                                                chunk_path = tts._save_chunk_audio(run_id_local, idx_local, src_local, audio_local)
                                                with tts._TTS_JOB_LOCK:
                                                    job = tts.JOBS.get(run_id_local) or {}
                                                    job.setdefault('chunks', []).append(chunk_path)
                                                    tts.JOBS[run_id_local] = job
                                            except Exception:
                                                pass
                                            b = bytes(audio_local)
                                            with parts_lock:
                                                parts.append(b)
                                            return {'index': idx_local, 'audio': b, 'text': sent_text, 'tts_ms': tts_ms_local, 'source': src_local, 'model': src_local, 'stream_id': sid}

                                        fut = synth_executor.submit(_synth_and_save_local)

                                        def _on_done_local(futobj):
                                            try:
                                                res = futobj.result()
                                                synth_done_q.put({'type': 'sentence', 'res': res})
                                            except Exception as e:
                                                synth_done_q.put({'type': 'synth_error', 'error': str(e)})

                                        fut.add_done_callback(_on_done_local)
                                        pending_futures.append(fut)

                                        try:
                                            yield f"event: tts_queued\ndata: {json.dumps({'model': src, 'stream_id': stream_id, 'index': idx})}\n\n"
                                        except Exception:
                                            pass
                                    except Exception as e:
                                        try:
                                            yield f"event: app_error\ndata: {json.dumps({'message': 'TTS scheduling failed', 'error': str(e)})}\n\n"
                                        except Exception:
                                            pass
                                    idx += 1
                                buffer = remainder
                            # drain any completed synths and yield sentence events
                            while True:
                                try:
                                    done_item = synth_done_q.get_nowait()
                                except Exception:
                                    break
                                if done_item.get('type') == 'sentence':
                                    r = done_item.get('res') or {}
                                    try:
                                        audio_b64 = base64.b64encode(r.get('audio')).decode('utf-8')
                                        yield f"event: sentence\ndata: {json.dumps({'index': r.get('index'), 'audio_data': audio_b64, 'mime_type': 'audio/mpeg', 'text': r.get('text'), 'tts_ms': r.get('tts_ms'), 'source': r.get('source'), 'model': r.get('model'), 'stream_id': r.get('stream_id')})}\n\n"
                                    except Exception:
                                        pass
                                elif done_item.get('type') == 'synth_error':
                                    try:
                                        yield f"event: app_error\ndata: {json.dumps({'message': 'TTS synth error', 'error': done_item.get('error')})}\n\n"
                                    except Exception:
                                        pass
                        try:
                            yield f"event: model\ndata: {json.dumps({'model': 'summarizer', 'ok': True, 'content': buffer, 'stream_id': stream_id})}\n\n"
                        except Exception:
                            pass

        except Exception as e:
            try:
                yield f"event: app_error\ndata: {json.dumps({'message': str(e)})}\n\n"
            except Exception:
                pass

        # allow a short grace period for pending synths to finish and emit their events
        try:
            try:
                concurrent.futures.wait(pending_futures, timeout=2)
            except Exception:
                pass
            # drain any remaining completed synths
            while True:
                try:
                    done_item = synth_done_q.get_nowait()
                except Exception:
                    break
                if done_item.get('type') == 'sentence':
                    r = done_item.get('res') or {}
                    try:
                        audio_b64 = base64.b64encode(r.get('audio')).decode('utf-8')
                        yield f"event: sentence\ndata: {json.dumps({'index': r.get('index'), 'audio_data': audio_b64, 'mime_type': 'audio/mpeg', 'text': r.get('text'), 'tts_ms': r.get('tts_ms'), 'source': r.get('source'), 'model': r.get('model'), 'stream_id': r.get('stream_id')})}\n\n"
                    except Exception:
                        pass
                elif done_item.get('type') == 'synth_error':
                    try:
                        yield f"event: app_error\ndata: {json.dumps({'message': 'TTS synth error', 'error': done_item.get('error')})}\n\n"
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            synth_executor.shutdown(wait=False)
        except Exception:
            pass

        # persist final combined audio if we produced chunks
        try:
            if parts:
                full = b''.join(parts)
                try:
                    with tts._TTS_JOB_LOCK:
                        tts.JOBS[run_id] = {'text': safe_text, 'created_at': time.time()}
                    save_path = tts._save_job_audio(run_id, src, full)
                    with tts._TTS_JOB_LOCK:
                        tts.JOBS[run_id][f"{src}_path"] = save_path
                        tts.JOBS[run_id][f"{src}_bytes"] = full
                        tts.JOBS[run_id][src] = full
                        tts.JOBS[run_id]['status'] = {src: 'done'}
                except Exception:
                    pass
                try:
                    yield f"event: final_audio\ndata: {json.dumps({'url': f'/api/tts/file/{run_id}?source={src}'})}\n\n"
                except Exception:
                    pass
                try:
                    audio_b64_full = base64.b64encode(full).decode('utf-8')
                    yield f"event: final_audio_bytes\ndata: {json.dumps({'audio_data': audio_b64_full, 'mime_type': 'audio/mpeg'})}\n\n"
                except Exception:
                    pass
        except Exception:
            pass

        yield "event: done\ndata: {}\n\n"

    return Response(stream_with_context(gen()), mimetype='text/event-stream', headers=headers)


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
