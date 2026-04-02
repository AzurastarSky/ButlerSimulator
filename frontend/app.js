// frontend/app.js
const API_STATE  = "/api/state";
const API_DEVICE = "/api/device";

// 2×3 plan, fixed order:
const PLAN = [
  "living room", "dining room", "kitchen",
  "bathroom", "bedroom", "office"
];

const floorLocal = document.getElementById("floorplan-local");
const floorCloud = document.getElementById("floorplan-cloud");

let LOCAL_HOUSE = { target: 20, current: 19, mode: "heat" };
let LOCAL_ROOMS = {};
let CLOUD_HOUSE = { target: 20, current: 19, mode: "heat" };
let CLOUD_ROOMS = {};

const elPrompt = document.getElementById('prompt-input');
const elPromptBtn = document.getElementById('prompt-send');
const elClearHistory = document.getElementById('clear-history');
const elFillerToggle = document.getElementById('filler-toggle');
const elFillerMode = document.getElementById('filler-mode');
const recStartBtn = document.getElementById('rec-start');
const recStopBtn  = document.getElementById('rec-stop');
const sttStatusEl = document.getElementById('stt-status');
const sttTranscriptEl = document.getElementById('stt-transcript');
const playerLocal = document.getElementById('player-local');
const playerCloud = document.getElementById('player-cloud');

// Conversation history: maintain last 4 messages (2 user + 2 assistant turns)
let conversationHistory = [];

// Filler mode state
let currentFillerMode = 'auto'; // can be 'on', 'off', or 'auto'

// Simple per-model playback queues for SSE audio chunks
const _playQueues = { local: [], cloud: [] };
const _playing = { local: false, cloud: false };

function _enqueueAudioFor(who, blob){
  if (!who) who = 'local';
  const q = _playQueues[who] || [];
  const url = URL.createObjectURL(blob);
  q.push({ url, blob });
  _playQueues[who] = q;
  if (!_playing[who]) _playNextFor(who);
}

function _playNextFor(who){
  const q = _playQueues[who] || [];
  const player = who === 'local' ? playerLocal : playerCloud;
  if (!q || q.length === 0){
    _playing[who] = false;
    return;
  }
  const item = q.shift();
  _playQueues[who] = q;
  _playing[who] = true;
  try{ player.src = item.url; player.play().catch(()=>{}); }catch(e){ console.warn('play failed', e); }
  player.onended = () => { setTimeout(()=> _playNextFor(who), 50); };
}

// Basic runtime debug hooks to help trace load-time errors
console.log('app.js loaded');
window.addEventListener('error', (ev) => {
  console.error('Window error', ev.error || ev.message || ev);
  try{ if (sttTranscriptEl) sttTranscriptEl.textContent = 'Frontend error: see console'; }catch(e){}
});
window.addEventListener('unhandledrejection', (ev) => {
  console.error('Unhandled rejection', ev.reason);
  try{ if (sttTranscriptEl) sttTranscriptEl.textContent = 'Frontend rejection: see console'; }catch(e){}
});

function titleCase(s){ return s.replace(/\b\w/g, c => c.toUpperCase()); }

// Convert recorded Blob (any codec) to 16kHz mono WAV in-browser
async function blobTo16kMonoWav(blob, targetRate = 16000){
  const arrayBuffer = await blob.arrayBuffer();
  const AudioCtx = window.OfflineAudioContext || window.webkitOfflineAudioContext || window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) throw new Error('Web Audio API not available');
  const decodeCtx = new (window.AudioContext || window.webkitAudioContext)();
  const audioBuffer = await decodeCtx.decodeAudioData(arrayBuffer);
  decodeCtx.close && decodeCtx.close();

  // If already target sample rate and mono, use directly
  let renderedBuffer = audioBuffer;
  if (audioBuffer.sampleRate !== targetRate){
    const offlineCtx = new OfflineAudioContext(Math.max(1, audioBuffer.numberOfChannels), Math.ceil(audioBuffer.duration * targetRate), targetRate);
    const src = offlineCtx.createBufferSource();
    src.buffer = audioBuffer;
    src.connect(offlineCtx.destination);
    src.start(0);
    renderedBuffer = await offlineCtx.startRendering();
  }

  // Mix to mono
  const chanCount = renderedBuffer.numberOfChannels;
  const len = renderedBuffer.length;
  const mono = new Float32Array(len);
  for (let c=0;c<chanCount;c++){
    const data = renderedBuffer.getChannelData(c);
    for (let i=0;i<len;i++) mono[i] += data[i] / chanCount;
  }

  // 16-bit PCM
  const bytesPerSample = 2;
  const blockAlign = bytesPerSample * 1;
  const buffer = new ArrayBuffer(44 + len * bytesPerSample);
  const view = new DataView(buffer);

  function writeString(view, offset, string){
    for (let i=0;i<string.length;i++) view.setUint8(offset+i, string.charCodeAt(i));
  }

  /* RIFF identifier */ writeString(view, 0, 'RIFF');
  /* file length */ view.setUint32(4, 36 + len * bytesPerSample, true);
  /* RIFF type */ writeString(view, 8, 'WAVE');
  /* format chunk identifier */ writeString(view, 12, 'fmt ');
  /* format chunk length */ view.setUint32(16, 16, true);
  /* sample format (raw) */ view.setUint16(20, 1, true);
  /* channel count */ view.setUint16(22, 1, true);
  /* sample rate */ view.setUint32(24, targetRate, true);
  /* byte rate (sampleRate * blockAlign) */ view.setUint32(28, targetRate * blockAlign, true);
  /* block align (channel count * bytes per sample) */ view.setUint16(32, blockAlign, true);
  /* bits per sample */ view.setUint16(34, bytesPerSample * 8, true);
  /* data chunk identifier */ writeString(view, 36, 'data');
  /* data chunk length */ view.setUint32(40, len * bytesPerSample, true);

  // write PCM samples
  let offset = 44;
  for (let i=0;i<len;i++){
    let s = Math.max(-1, Math.min(1, mono[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
    offset += 2;
  }

  return new Blob([view], { type: 'audio/wav' });
}

function roomBox(roomName, devices){
  const light  = (devices && devices.light)  || "off";
  const isOn   = light === "on";

  const el = document.createElement("div");
  el.className = "room" + (isOn ? " active" : "");
  el.dataset.room = roomName;
  el.innerHTML = `
    <div class="room-header">
      <div class="room-name">${titleCase(roomName)}</div>
      <div class="room-status">
        <span class="badge ${isOn ? "on" : "off"}"></span>
        <span class="status-text ${isOn ? "on" : "off"}">${light.toUpperCase()}</span>
      </div>
    </div>
    <div class="room-body">
      <div class="pills">
        <div class="device-pill">Light</div>
      </div>
      <div class="controls">
        <button class="btn" data-device="light"  data-action="turn_on">On</button>
        <button class="btn" data-device="light"  data-action="turn_off">Off</button>
      </div>
    </div>
  `;

  // Click room to toggle light state (send explicit on/off)
  el.addEventListener("click", (e) => {
    if (e.target.closest(".btn")) return;
    // determine current state from badge/status text
    const current = (el.querySelector('.status-text') || {}).textContent || '';
    const isCurrentlyOn = String(current || '').toLowerCase().includes('on');
    sendAction(roomName, "light", isCurrentlyOn ? "turn_off" : "turn_on");
  });

  // Button handlers
  el.querySelectorAll(".btn").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const action = btn.dataset.action;
      const device = btn.dataset.device;
      sendAction(roomName, device, action);
    });
  });

  return el;
}

function renderRoomsInto(container, rooms){
  container.innerHTML = "";
  PLAN.forEach(room => {
    const devices = rooms[room] || { light: "off" };
    container.appendChild(roomBox(room, devices));
  });
}

async function fetchStateFor(which){
  const res = await fetch(`${API_STATE}?which=${which}`);
  if (!res.ok) return;
  const data = await res.json();
    if (which === 'local'){
    LOCAL_HOUSE = data.house || LOCAL_HOUSE;
    LOCAL_ROOMS = data.rooms || LOCAL_ROOMS;
    renderRoomsInto(floorLocal, LOCAL_ROOMS);
    // update local thermo chips (target only)
    const m = document.getElementById('thermo-mode-local');
    const t = document.getElementById('thermo-target-local');
    if (m) m.textContent = (LOCAL_HOUSE.mode === 'heat') ? 'HEAT' : 'OFF';
    if (m) m.style.color = (LOCAL_HOUSE.mode === 'heat') ? '#30d158' : '#9aa4b2';
    if (t) t.textContent = `Target ${Number(LOCAL_HOUSE.target || 0).toFixed(0)}°C`;
  } else {
    CLOUD_HOUSE = data.house || CLOUD_HOUSE;
    CLOUD_ROOMS = data.rooms || CLOUD_ROOMS;
    renderRoomsInto(floorCloud, CLOUD_ROOMS);
    // update cloud thermo chips (target only)
    const mc = document.getElementById('thermo-mode-cloud');
    const tc = document.getElementById('thermo-target-cloud');
    if (mc) mc.textContent = (CLOUD_HOUSE.mode === 'heat') ? 'HEAT' : 'OFF';
    if (mc) mc.style.color = (CLOUD_HOUSE.mode === 'heat') ? '#30d158' : '#9aa4b2';
    if (tc) tc.textContent = `Target ${Number(CLOUD_HOUSE.target || 0).toFixed(0)}°C`;
  }
}

// Main thermostat UI removed; fetching main house state no longer needed.

async function sendAction(room, device, action, value){
  await fetch(API_DEVICE, {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ room, device, action, value })
  });
  // refresh both displays
  await fetchStateFor('local');
  await fetchStateFor('cloud');
  // main thermostat removed
}

// Toolbar: thermostat controls remain global and call main device endpoint
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".tbtn");
  if (!btn) return;

  // Thermostat controls
  if (btn.dataset.thermo){
    const kind = btn.dataset.thermo;
    if (kind === "increase") return sendAction("all", "thermostat", "increase");
    if (kind === "decrease") return sendAction("all", "thermostat", "decrease");
    if (kind === "on")       return sendAction("all", "thermostat", "turn_on");
    if (kind === "off")      return sendAction("all", "thermostat", "turn_off");
    if (kind === "set")      return sendAction("all", "thermostat", "set_value", Number(btn.dataset.value||20));
    return;
  }

  // Scope device controls
  const scope  = btn.dataset.scope;   // all | upstairs | downstairs
  const device = btn.dataset.device;  // light
  const action = btn.dataset.action;
  if (!scope || !device || !action) return;
  sendAction(scope, device, action);
});

// small response areas
function ensureRespEl(who){
  let id = who === 'local' ? 'resp-local' : 'resp-cloud';
  let el = document.getElementById(id);
  if (!el){
    const parent = who === 'local' ? floorLocal.parentElement : floorCloud.parentElement;
    el = document.createElement('div');
    el.id = id;
    el.style.fontSize = '12px';
    el.style.color = '#9aa4b2';
    el.style.margin = '6px 10px';
    parent.insertBefore(el, parent.children[1]);
  }
  // ensure a persistent metrics element just below the blurb
  const metricsId = who === 'local' ? 'metrics-local' : 'metrics-cloud';
  let mel = document.getElementById(metricsId);
  if (!mel){
    const parent = who === 'local' ? floorLocal.parentElement : floorCloud.parentElement;
    mel = document.createElement('div');
    mel.id = metricsId;
    mel.style.fontSize = '16px';
    mel.style.fontWeight = '600';
    mel.style.color = '#a0aab5';
    mel.style.margin = '8px 10px 12px 10px';
    mel.style.minHeight = '24px';
    mel.style.textAlign = 'center';
    parent.insertBefore(mel, parent.children[2]);
  }
  return el;
}

// Persistent metrics helper: store numeric metrics in data attributes and render
// Replace previous multi-metric UI with single TTFA metric display
function _metricsSet(who, key, val){
  // Only handle TTFA metric now: key === 'ttfa'
  if (key !== 'ttfa') return;
  const metricsId = who === 'local' ? 'metrics-local' : 'metrics-cloud';
  let mid = document.getElementById(metricsId);
  if (!mid) {
    // Element doesn't exist yet, ensure it's created
    ensureRespEl(who);
    mid = document.getElementById(metricsId);
  }
  if (!mid) return;
  try{
    // If TTFA is already set for this request, don't overwrite it
    const currentText = mid.textContent || '';
    if (currentText.includes('TTFA:') && currentText.includes('ms')) {
      console.debug(`[TTFA] Skipping update for ${who} - already set to ${currentText}`);
      return; // Don't overwrite existing TTFA value
    }
    
    if (val === null || typeof val === 'undefined' || val === ''){
      mid.textContent = '';
    } else {
      mid.textContent = `TTFA: ${Math.round(Number(val))}ms`;
    }
  }catch(e){/*ignore*/}
}

// Prompt handling + SSE
function openRaceSSE(text){
  // Build URL with user message and conversation history
  let url = `/api/chat/stream?user=${encodeURIComponent(text)}`;
  if (conversationHistory.length > 0) {
    // Send last 4 messages (2 turns) as context
    url += `&history=${encodeURIComponent(JSON.stringify(conversationHistory))}`;
  }
  const promptStart = Date.now();
  
  // Clear TTFA metrics for new request
  try {
    const localMetrics = document.getElementById('metrics-local');
    const cloudMetrics = document.getElementById('metrics-cloud');
    if (localMetrics) localMetrics.textContent = '';
    if (cloudMetrics) cloudMetrics.textContent = '';
  } catch(e) {/*ignore*/}
  
  const es = new EventSource(url);
  let esStreamId = null;
  let sawSentence = false;
  let firstAudioReceived = { local: false, cloud: false };
  let assistantResponses = { local: '', cloud: '' };
  ensureRespEl('local').textContent = 'waiting...';
  ensureRespEl('cloud').textContent = 'waiting...';
  es.addEventListener('model', async (ev) => {
    try{
      const data = JSON.parse(ev.data);
      // record stream id for this EventSource
      try{ if (data.stream_id) esStreamId = data.stream_id; }catch(e){}
      const who = data.model; // 'local' or 'cloud'
      const ms = data.ms;
      const content = data.content || '';
      // Track assistant response for history
      if (content) assistantResponses[who] = content;
      // Blurb box: only show assistant/tool content (no timings), remove asterisks
      const displayContent = content.replace(/\*+/g, '').replace(/\n/g,' ');
      ensureRespEl(who).textContent = displayContent;
      // Metrics block below: persist LLM timing
      try{ _metricsSet(who, 'llm', ms); }catch(e){}
      await fetchStateFor(who);
      const panel = who === 'local' ? floorLocal.parentElement : floorCloud.parentElement;
      panel.style.boxShadow = '0 0 0 3px rgba(102,178,255,0.12)';
      setTimeout(()=> panel.style.boxShadow = '', 800);
      // Auto-synthesize and play: summarize only when a tool call was performed
      try{
        const safeContent = sanitizeForTTS(content);
        let textToSpeak = null;
        if (data.applied && typeof data.applied === 'object' && Object.keys(data.applied).length > 0){
          // Tool call present — ask backend summarizer to stream a concise spoken summary
          try{
            const pref = who === 'cloud' ? 'cloud' : 'local';
            const voice = who === 'cloud' ? 'sage' : undefined;
            const player = who === 'local' ? playerLocal : playerCloud;
            const source = pref;
            streamSummarizeAndPlay(safeContent, data.applied, pref, source, voice, player, promptStart, ()=>{ sawSentence = true }).catch(e=>{
              console.warn('summarize_stream failed', e);
              // fallback: synthesize a composed summary client-side
              try{
                const fallback = composeSummary(data.parsed, data.applied, safeContent);
                streamTtsSentencesViaFetch(sanitizeForTTS(fallback), source, voice, player, promptStart).catch(()=>{});
              }catch(err){ console.warn('fallback synth failed', err); }
            });
          }catch(e){ console.warn('summarize_stream error', e); try{ const fallback = composeSummary(data.parsed, data.applied, safeContent); streamTtsSentencesViaFetch(sanitizeForTTS(fallback), who === 'cloud' ? 'cloud' : 'local', who === 'cloud' ? 'sage' : undefined, who === 'local' ? playerLocal : playerCloud, promptStart).catch(()=>{}); }catch(err){}
          }
        } else {
          // No tool call — speak the assistant content directly
          textToSpeak = safeContent;
        }

        if (textToSpeak){
          const voice = who === 'cloud' ? 'sage' : undefined;
          const safeSpeak = sanitizeForTTS(textToSpeak);
          const player = who === 'local' ? playerLocal : playerCloud;
          const source = who === 'cloud' ? 'cloud' : 'local';
          // Delay client-initiated TTS slightly. If server-side `sentence` events
          // arrive within this window, skip the client TTS to avoid duplicate
          // synthesis and playback.
          (async function(){
            const delayMs = 250;
            await new Promise(r => setTimeout(r, delayMs));
            if (sawSentence) return; // server is already streaming sentence audio
            try{
              streamTtsSentencesViaFetch(safeSpeak, source, voice, player, promptStart).catch(e=>{ console.warn('streaming TTS failed', e); });
            }catch(e){ console.warn('streamTtsSentencesViaFetch error', e); }
          })();
        }
      }catch(e){ console.warn('Auto TTS error', e); }
    }catch(e){
      console.error(e);
    }
  });
  // Server indicates it has queued TTS work for this stream (prevents client-side TTS)
  es.addEventListener('tts_queued', (ev) => {
    try{
      const obj = JSON.parse(ev.data || '{}');
      if (obj && obj.stream_id && esStreamId && obj.stream_id === esStreamId){
        sawSentence = true;
      }
    }catch(e){ console.warn('tts_queued parse error', e); }
  });
      // Partial streaming text updates (deltas)
      es.addEventListener('model_text', (ev) => {
        try{
          const obj = JSON.parse(ev.data);
          const who = obj.model || 'local';
          const text = obj.text || '';
          if (!text) return;
          // Remove asterisks from streaming text
          const cleanText = text.replace(/\*/g, '');
          const el = ensureRespEl(who);
          try{ el.textContent = (el.textContent || '') + cleanText; }catch(e){}
        }catch(e){ console.warn('model_text parse error', e); }
      });

      // Incoming synthesized sentence audio (base64) sent from server stream
      es.addEventListener('sentence', (ev) => {
        try{
          const payload = JSON.parse(ev.data);
          // if stream ids are present, only accept events matching this ES
          if (payload.stream_id && esStreamId && payload.stream_id !== esStreamId) return;
          // mark that server is providing sentence audio (prevents duplicate client TTS)
          sawSentence = true;
          const who = payload.model || payload.source || 'local';
          
          // Track TTFA for first audio sentence from each model
          if (!firstAudioReceived[who]) {
            firstAudioReceived[who] = true;
            const ms = Date.now() - promptStart;
            _metricsSet(who, 'ttfa', ms);
          }
          
          const b64 = payload.audio_data || payload.audio || '';
          if (!b64) return;
          const binStr = atob(b64);
          const len = binStr.length;
          const arr = new Uint8Array(len);
          for (let i=0;i<len;i++) arr[i] = binStr.charCodeAt(i);
          const blob = new Blob([arr.buffer], { type: payload.mime_type || 'audio/mpeg' });
          _enqueueAudioFor(who, blob);
        }catch(e){ console.warn('sentence parse error', e); }
      });

      es.addEventListener('error', (e)=>{ 
        es.close(); 
        // Update conversation history when stream ends
        // Add user message
        conversationHistory.push({ role: 'user', content: text });
        // Add assistant response (prefer cloud if available, otherwise local)
        const assistantReply = assistantResponses.cloud || assistantResponses.local || '';
        if (assistantReply) {
          conversationHistory.push({ role: 'assistant', content: assistantReply });
        }
        // Keep only last 4 messages (2 turns)
        if (conversationHistory.length > 4) {
          conversationHistory = conversationHistory.slice(-4);
        }
      });
  return es;
}

elPromptBtn.addEventListener('click', () => {
  const text = elPrompt.value.trim();
  if (!text) return;
  openRaceSSE(text);
  elPrompt.value = ''; // Clear input after sending
});

// Clear conversation history
elClearHistory.addEventListener('click', () => {
  conversationHistory = [];
  console.log('Conversation history cleared');
  // Visual feedback
  const btn = elClearHistory;
  const originalText = btn.textContent;
  btn.textContent = 'Cleared!';
  setTimeout(() => { btn.textContent = originalText; }, 1000);
});

// Toggle filler mode: auto -> on -> off -> auto
elFillerToggle.addEventListener('click', async () => {
  const modes = ['auto', 'on', 'off'];
  const currentIndex = modes.indexOf(currentFillerMode);
  const nextMode = modes[(currentIndex + 1) % modes.length];
  
  try {
    const response = await fetch('/api/settings/filler-mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: nextMode })
    });
    
    if (response.ok) {
      const data = await response.json();
      currentFillerMode = data.filler_mode;
      elFillerMode.textContent = currentFillerMode.toUpperCase();
      console.log('Filler mode updated to:', currentFillerMode);
    } else {
      console.error('Failed to update filler mode');
    }
  } catch (error) {
    console.error('Error updating filler mode:', error);
  }
});

// Load current filler mode on startup
async function loadFillerMode() {
  try {
    const response = await fetch('/api/settings');
    if (response.ok) {
      const data = await response.json();
      currentFillerMode = data.filler_mode || 'auto';
      elFillerMode.textContent = currentFillerMode.toUpperCase();
      console.log('Loaded filler mode:', currentFillerMode);
    }
  } catch (error) {
    console.error('Error loading settings:', error);
  }
}

// Allow Enter key to send prompt
elPrompt.addEventListener('keypress', (e) => {
  if (e.key === 'Enter') {
    elPromptBtn.click();
  }
});

// ---------- TTS helpers (used to auto-play LLM responses) ----------
async function startTTS(text, voice){
  const body = { text };
  if (voice) body.voice = voice;
  const res = await fetch('/api/tts', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
  if (!res.ok) {
    const j = await res.json().catch(()=>({}));
    throw new Error(j.error || res.statusText || 'TTS start failed');
  }
  return await res.json();
}

// Poll TTS job status and render timing block above the thermostat target
async function monitorTTSStatus(job_id, who){
  const deadline = Date.now() + 30000;
  while(Date.now() < deadline){
    try{
      const resp = await fetch(`/api/tts/status?job_id=${encodeURIComponent(job_id)}`);
      if (!resp.ok) { break; }
      const j = await resp.json().catch(()=>null);
      if (!j || !j.job) { break; }
      const t = j.job.timings || {};
      const srcTiming = t[who] && typeof t[who].duration_ms !== 'undefined' ? t[who].duration_ms : null;
      // Persist the TTS timing into the matching persistent metrics block only
      try{ _metricsSet(who, 'tts', srcTiming); }catch(e){}
      // stop early if both providers finished
      const s = j.job.status || {};
      if ((s.local && s.local !== 'pending') && (s.cloud && s.cloud !== 'pending')) break;
    }catch(e){ console.warn('monitorTTSStatus', e); }
    await new Promise(r=>setTimeout(r, 400));
  }
}

async function fetchTTSStreamAndPlay(job_id, playerEl){
  const streamUrl = `/api/tts/stream?job_id=${encodeURIComponent(job_id)}`;
  const resp = await fetch(streamUrl);
  if (!resp.ok) {
    const j = await resp.json().catch(()=>null);
    throw new Error((j && j.error) ? j.error : resp.statusText);
  }
  const ct = resp.headers.get('Content-Type') || '';
  const source = resp.headers.get('X-TTS-Source') || 'unknown';
  const ab = await resp.arrayBuffer();
  const blob = new Blob([ab], { type: ct || 'audio/mpeg' });
  const url = URL.createObjectURL(blob);
  if (playerEl){
    playerEl.src = url;
    try{ await playerEl.play(); }catch(e){}
  }
  return source;
}


// Stream per-sentence TTS via POST streaming and play incrementally
async function streamTtsSentencesViaFetch(text, source='local', voice=undefined, playerEl=null, ttfaStart=null){
  // Creates a queue of audio blobs and plays them sequentially as they arrive.
  const queue = [];
  const collected = []; // all received blobs in order
  let playing = false;
  let firstSentReceived = false;

  function playNext(){
    if (playing) return;
    if (queue.length === 0) return;
    const item = queue.shift();
    playing = true;
    playerEl.src = item.url;
    playerEl.play().catch(()=>{});
    playerEl.onended = () => {
      // Do NOT revoke chunk object URL here; keep blobs around until final stitching completes
      playing = false;
      // small grace to allow next chunk to be set
      setTimeout(playNext, 50);
    };
  }

  const controller = new AbortController();
  const payload = { text };
  if (source) payload.source = source;
  if (voice) payload.voice = voice;

  const res = await fetch('/api/tts/stream_sentences', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload),
    signal: controller.signal
  });
  if (!res.ok) throw new Error('TTS stream request failed');

  const reader = res.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buf = '';
  let finalAudioUrl = null;
  let finalAudioBlobFromServer = null;

  const parseSSEChunk = (chunk) => {
    // chunk is text appended; split into complete event blocks by double newline
    const parts = chunk.split('\n\n');
    for (let i=0;i<parts.length-1;i++){
      const block = parts[i].trim();
      if (!block) continue;
      const lines = block.split('\n');
      let evt = 'message';
      let data = '';
      for (const L of lines){
        if (L.startsWith('event:')) evt = L.replace(/^event:\s*/,'').trim();
        else if (L.startsWith('data:')) data += L.replace(/^data:\s*/,'') + '\n';
      }
      data = data.trim();
      handleSSEEvent(evt, data);
    }
    return parts[parts.length-1];
  };

  const handleSSEEvent = (evt, data) => {
    try{
      if (evt === 'sentence'){
        const payload = JSON.parse(data);
        const b64 = payload.audio_data || payload.audio || '';
        if (!b64) return;
        // decode base64 to Uint8Array
        const binStr = atob(b64);
        const len = binStr.length;
        const arr = new Uint8Array(len);
        for (let i=0;i<len;i++) arr[i] = binStr.charCodeAt(i);
        const u8 = new Uint8Array(arr); // copy bytes
        const blob = new Blob([u8.buffer], { type: 'audio/mpeg' });
        const url = URL.createObjectURL(blob);
        queue.push({ url, blob });
        collected.push(u8);
        console.debug('[TTS stream] received sentence', payload.index, 'bytes', u8.byteLength, 'collectedCount', collected.length);
        // compute TTFA when first sentence arrives
        if (!firstSentReceived){
          firstSentReceived = true;
          try{
            if (ttfaStart){ 
              const ms = Date.now() - Number(ttfaStart); 
              _metricsSet(source, 'ttfa', ms); 
            }
          }catch(e){/*ignore*/}
        }
        // start playing if idle
        playNext();
      } else if (evt === 'final_audio'){
        // data contains url; record the server-side final audio URL but do not fetch it
        // immediately. We wait until the entire stream completes to avoid racing with
        // server-side file writes. On stream completion we'll prefer fetching this URL
        // as the authoritative final audio; otherwise we'll fall back to client-side
        // concatenation of received chunks.
        try{
          const obj = JSON.parse(data);
          if (obj && obj.url){ finalAudioUrl = obj.url; console.debug('[TTS stream] final_audio url', finalAudioUrl); }
        }catch(e){ console.warn('final_audio parse failed', e); }
      } else if (evt === 'final_audio_bytes'){
        // full audio bytes encoded as base64 from the server; prefer this if present
        try{
          const obj = JSON.parse(data);
          const b64 = obj && obj.audio_data;
          if (b64){
            const binStr = atob(b64);
            const len = binStr.length;
            const arr = new Uint8Array(len);
            for (let i=0;i<len;i++) arr[i] = binStr.charCodeAt(i);
            const blob = new Blob([arr.buffer], { type: obj.mime_type || 'audio/mpeg' });
            try{ finalAudioBlobFromServer = URL.createObjectURL(blob); }catch(e){ finalAudioBlobFromServer = null; }
            console.debug('[TTS stream] received final_audio_bytes length', len, 'finalAudioBlobFromServer', finalAudioBlobFromServer);
          }
        }catch(e){ console.warn('final_audio_bytes parse failed', e); }
      } else if (evt === 'tts_metrics'){
        // optional: render metrics
        try{ const m = JSON.parse(data); console.log('tts_metrics', m); }catch(e){}
      } else if (evt === 'app_error'){
        try{ const e = JSON.parse(data); console.warn('TTS stream error', e); }catch(e){ console.warn('TTS stream error', data); }
      }
    }catch(e){ console.error('handleSSEEvent', e); }
  };

  try{
    while(true){
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // process complete SSE blocks
      buf = parseSSEChunk(buf);
    }
    if (buf && buf.trim()) parseSSEChunk(buf + '\n\n');
  }catch(e){ console.warn('streamTtsSentencesViaFetch read error', e); }
  finally{
    controller.abort();
  }
  // When the stream completes, assemble collected blobs into a single full audio
  try{
    if (!playerEl) return;

    console.debug('[TTS stream] finalize: collected chunks', collected.length);
    // Always assemble a client-side full audio from collected chunks
    let clientFull = null;
    let clientFullUrl = null;
    if (collected.length){
      let total = 0;
      for (const c of collected) total += c.byteLength;
      const fullArr = new Uint8Array(total);
      let offset = 0;
      for (const c of collected){ fullArr.set(c, offset); offset += c.byteLength; }
      clientFull = new Blob([fullArr.buffer], { type: 'audio/mpeg' });
      clientFullUrl = URL.createObjectURL(clientFull);
    }

    // Assemble client-side full audio from collected chunks and expose it to the
    // player for manual replay, but do NOT autoplay it. We still stream chunks
    // as they arrive; this step only sets the final file once the streamed
    // playback has completed to avoid repeating audio.
    const clientFinalUrl = clientFullUrl;
    const serverFinalUrl = finalAudioBlobFromServer || null;

    // Helper: wait until the streaming playback queue is drained and the player
    // is idle (not currently playing). Then set the player's src to the final
    // combined file but do not call play(). If a server-provided blob exists and
    // is larger than the client-assembled file, prefer it.
    (async function waitAndExposeFinal(){
      const maxWaitMs = 30000;
      const start = Date.now();
      while(true){
        // idle condition: no queued incremental chunks and not playing
        if ((!queue || queue.length === 0) && !playing) break;
        if ((Date.now() - start) > maxWaitMs) break;
        await new Promise(r => setTimeout(r, 100));
      }

      // decide which final URL to use
      let chosen = clientFinalUrl;
      try{
        if (serverFinalUrl && chosen){
          // try to compare sizes; fetch server blob headers if possible
          try{
            // If the server provided a blob URL (object URL), browsers do not
            // support network HEAD requests against blob: URLs — use it directly.
            if (typeof serverFinalUrl === 'string' && serverFinalUrl.startsWith('blob:')){
              chosen = serverFinalUrl;
            } else {
              const resp = await fetch(serverFinalUrl, { method: 'HEAD' });
              if (resp && resp.ok){
                const sLen = parseInt(resp.headers.get('Content-Length') || '0', 10) || 0;
                const cLen = (clientFull && clientFull.size) ? clientFull.size : (clientFinalUrl ? 0 : 0);
                if (sLen >= cLen && sLen > 0) chosen = serverFinalUrl;
              } else {
                // fallback: prefer server blob if available
                chosen = serverFinalUrl;
              }
            }
          }catch(e){
            chosen = serverFinalUrl;
          }
        }
      }catch(e){/*ignore*/}

      if (chosen){
        try{ playerEl.src = chosen; }catch(e){ console.warn('set final src failed', e); }
        // mark final availability so UI can show it if needed
        try{ playerEl.dataset.finalAvailable = '1'; }catch(e){}
      }
    })();
  }catch(e){ console.warn('streamTtsSentencesViaFetch finalize error', e); }
}

// Expose helper for debugging in console
window.streamTtsSentencesViaFetch = streamTtsSentencesViaFetch;

// Stream summarizer (LLM) and server-side per-sentence TTS, play as sentences arrive
async function streamSummarizeAndPlay(text, applied, prefer='cloud', who='local', voice=undefined, playerEl=null, ttfaStart=null, onFirstSentence=null){
  const payload = { text };
  if (typeof applied !== 'undefined') payload.applied = applied;
  if (prefer) payload.prefer = prefer;

  const res = await fetch('/api/tts/summarize_stream', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  });
  if (!res.ok) {
    const j = await res.json().catch(()=>null);
    throw new Error((j && j.error) ? j.error : res.statusText);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buf = '';
  let firstSentReceived = false;

  const parseSSEChunk = (chunk) => {
    const parts = chunk.split('\n\n');
    for (let i=0;i<parts.length-1;i++){
      const block = parts[i].trim();
      if (!block) continue;
      const lines = block.split('\n');
      let evt = 'message';
      let data = '';
      for (const L of lines){
        if (L.startsWith('event:')) evt = L.replace(/^event:\s*/,'').trim();
        else if (L.startsWith('data:')) data += L.replace(/^data:\s*/,'') + '\n';
      }
      data = data.trim();
      handleSSEEvent(evt, data);
    }
    return parts[parts.length-1];
  };

  const handleSSEEvent = (evt, data) => {
    try{
      if (evt === 'sentence'){
        const payload = JSON.parse(data);
        const b64 = payload.audio_data || payload.audio || '';
        if (!b64) return;
        const binStr = atob(b64);
        const len = binStr.length;
        const arr = new Uint8Array(len);
        for (let i=0;i<len;i++) arr[i] = binStr.charCodeAt(i);
        const blob = new Blob([arr.buffer], { type: payload.mime_type || 'audio/mpeg' });
        // signal caller that server is providing audio
        if (!firstSentReceived){
          firstSentReceived = true;
          try{ if (ttfaStart) { const ms = Date.now() - Number(ttfaStart); _metricsSet(who, 'ttfa', ms); } }catch(e){}
          try{ if (onFirstSentence) onFirstSentence(); }catch(e){}
        }
        _enqueueAudioFor(who, blob);
      } else if (evt === 'model_text'){
        try{
          const obj = JSON.parse(data);
          const text = obj.text || '';
          if (!text) return;
          // Remove asterisks from streaming text
          const cleanText = text.replace(/\*/g, '');
          const el = ensureRespEl(who);
          try{ 
            const current = el.textContent || '';
            const separator = current && !current.endsWith(' ') && !current.endsWith('\n') ? '  ' : '';
            el.textContent = current + separator + cleanText;
          }catch(e){}
        }catch(e){ console.warn('model_text parse error (summarize_stream)', e); }
      } else if (evt === 'tts_queued'){
        try{ if (onFirstSentence) onFirstSentence(); }catch(e){}
      } else if (evt === 'final_audio'){
        // ignore here; streamSentences handler will otherwise surface final audio
      } else if (evt === 'final_audio_bytes'){
        // ignore
      } else if (evt === 'app_error'){
        try{ const e = JSON.parse(data); console.warn('summarize_stream error', e); }catch(e){ console.warn('summarize_stream error', data); }
      }
    }catch(e){ console.error('handleSSEEvent summarize_stream', e); }
  };

  try{
    while(true){
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      buf = parseSSEChunk(buf);
    }
    if (buf && buf.trim()) parseSSEChunk(buf + '\n\n');
  }catch(e){ console.warn('streamSummarizeAndPlay read error', e); }
}




// Sanitize assistant content before sending to summarizer or TTS
function sanitizeForTTS(s){
  try{
    if (!s) return '';
    let out = String(s).trim();
    // If the string is a JSON wrapper like {"tool":null,"reply":"..."}, try to extract reply/content
    try{
      const j = JSON.parse(out);
      if (j && typeof j === 'object'){
        for (const k of ['reply','content','text','message']){
          if (k in j && j[k]) return String(j[k]).trim();
        }
        // If this looks like a pure tool-invocation (no content fields), skip it
        const toolLike = Object.keys(j).some(k => {
          const lk = String(k).toLowerCase();
          return lk === 'tool' || lk === 'action' || lk === 'command' || lk.endsWith('_length') || lk.endsWith('_size') || lk === 'tool_name';
        });
        if (toolLike) return '';
        // fallback: join string-valued properties
        const vals = Object.values(j).filter(v => typeof v === 'string' && v.trim()).map(v => v.trim());
        if (vals.length) return vals.join(' ');
      }
    }catch(e){ /* not JSON */ }

    // remove common serialized toolcall artifacts like "tool:null" or "tool: null", covering quoted keys
    out = out.replace(/\"?tool\"?\s*:\s*null,?/gi, '');
    out = out.replace(/\"?tool\"?\s*:\s*\"[^\"]*\"\s*,?/gi, '');
    // remove any leading 'tool:...' tokens on their own lines
    out = out.split('\n').filter(line => !/^\s*\"?tool\"?\s*:/i.test(line)).join('\n');
    // remove markdown asterisks (bold and italic formatting)
    out = out.replace(/\*+/g, '');
    // collapse multiple whitespace and trim
    out = out.replace(/\s+/g,' ').trim();
    return out;
  }catch(e){ return '' }
}

// Compose a human-friendly summary from parsed/applied/content (used by SSE and recorder flows)
function composeSummary(parsed, applied, content){
  try{
    if (applied && typeof applied === 'object'){
      // Human-friendly verbs
      const verbMap = {
        'turn_on': 'turned on', 'turn_off': 'turned off',
        'increase': 'increased', 'decrease': 'decreased', 'set_value': 'set'
      };

      // Thermostat handling
      if (applied.ok && applied.device === 'thermostat'){
        const act = applied.action || '';
        const house = applied.house || {};
        if (act === 'set_value' && house.target != null){
          return `Okay — I've set the thermostat to ${Number(house.target).toFixed(0)}°C.`;
        }
        if (act === 'increase'){
          return `Okay — I've increased the thermostat.`;
        }
        if (act === 'decrease'){
          return `Okay — I've decreased the thermostat.`;
        }
        if (act === 'turn_on') return `Okay — I've turned the thermostat on.`;
        if (act === 'turn_off') return `Okay — I've turned the thermostat off.`;
      }

      // Device-specific (lights)
      if (applied.ok && (applied.device === 'light' || applied.device === 'blinds')){
        // Treat any legacy 'blinds' mentions as 'light' (blinds removed server-side)
        const dev = (applied.device === 'light' || applied.device === 'blinds') ? 'light' : applied.device;
        const act = applied.action || '';
        if (applied.bulk && Array.isArray(applied.applied)){
            // if any target is 'all', prefer a concise 'all rooms' phrasing
            const rawRooms = applied.applied.map(r => (r.room || '').toString().toLowerCase());
            if (rawRooms.includes('all')){
              return `Done — I've ${verbMap[act] || act} the ${dev} in all rooms.`;
            }
            const rooms = applied.applied.map(r=> (r.room ? ('the ' + titleCase(r.room)) : '')).filter(Boolean);
            if (rooms.length === 1) return `Done — I've ${verbMap[act] || act} the ${dev} in ${rooms[0]}.`;
            if (rooms.length > 1) return `Done — I've ${verbMap[act] || act} the ${dev} in ${rooms.slice(0,3).join(', ')}.`;
            return `Done — I've ${verbMap[act] || act} the ${dev} for you.`;
        }
        if (applied.room){
          const rn = (applied.room || '').toString().toLowerCase();
          if (rn === 'all') return `Done — I've ${verbMap[applied.action] || (applied.action || '').replace(/_/g,' ')} the ${dev} in all rooms.`;
          return `Done — I've ${verbMap[applied.action] || (applied.action || '').replace(/_/g,' ')} the ${dev} in the ${titleCase(applied.room)}.`;
        }
        // fallback
        return `Done — I've ${verbMap[applied.action] || (applied.action || '').replace(/_/g,' ')} the ${dev}.`;
      }

      // Generic applied.ok with message
      if (applied.ok && applied.message) return String(applied.message);
    }
  }catch(e){/*ignore*/}

  // Fallback: make the assistant content more conversational
  try{
    let s = String(content || '');
    // remove JSON/code
    s = s.replace(/```[\s\S]*?```/g, '').trim();
    // if content looks like JSON only, use a short friendly phrase
    if (!s) return 'Okay — I performed the requested action.';
    // If content contains JSON at start, strip it
    if (s.startsWith('{') || s.startsWith('[')){
      const after = s.replace(/^[\s\S]*?\}\s*/, '').trim();
      if (after) s = after;
      else return 'Okay — I performed the requested action.';
    }
    // Ensure punctuation and a friendly prefix
    s = s.replace(/\s+/g,' ').trim();
    if (!/[\.\!\?]$/.test(s)) s = s + '.';
    // Add a short humanizing prefix
    return `Okay — ${s.charAt(0).toUpperCase() + s.slice(1)}`;
  }catch(e){ return 'Okay — I performed the requested action.'; }
}

// ---------- Recorder logic ----------
let mediaRecorder = null;
let recordedChunks = [];


recStartBtn.addEventListener('click', async ()=>{
  // request mic
  try{
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    recordedChunks = [];
    mediaRecorder = new MediaRecorder(stream);
    mediaRecorder.ondataavailable = (e) => { if (e.data && e.data.size>0) recordedChunks.push(e.data); };
    mediaRecorder.onstop = async () => {
      const rawBlob = new Blob(recordedChunks, { type: 'audio/webm' });
      sttTranscriptEl.textContent = 'Uploading...';

      // Convert to WAV for local STT to avoid server-side conversion (faster)
      let localBlob = rawBlob;
      try{
        localBlob = await blobTo16kMonoWav(rawBlob, 16000);
      }catch(e){
        console.warn('WAV conversion failed, falling back to original blob', e);
        localBlob = rawBlob;
      }

      // create two FormData objects (FormData can't be reused reliably)
      const fdLocal = new FormData();
      fdLocal.append('audio', localBlob, 'rec.wav');
      const fdCloud = new FormData();
      fdCloud.append('audio', rawBlob, 'rec.webm');

      sttTranscriptEl.textContent = 'Requesting local and cloud transcriptions...';
      try{
        // Start both STT requests at the same timestamp to avoid skew
        const startTs = Date.now();
        const reqLocal = fetch('/api/stt?lang=en', { method: 'POST', body: fdLocal });
        const reqCloud = fetch('/api/stt/cloud?lang=en', { method: 'POST', body: fdCloud });

        const localPromise = reqLocal
          .then(async res => {
            const elapsed = Date.now() - startTs;
            const payload = res.ok ? await res.json() : { ok: false, error: res.statusText };
            try{ payload.__client_elapsed_ms = elapsed; }catch(e){}
            return payload;
          })
          .catch(e => ({ ok: false, error: String(e), __client_elapsed_ms: Date.now() - startTs }));

        const cloudPromise = reqCloud
          .then(async res => {
            const elapsed = Date.now() - startTs;
            const payload = res.ok ? await res.json() : { ok: false, error: res.statusText };
            try{ payload.__client_elapsed_ms = elapsed; }catch(e){}
            return payload;
          })
          .catch(e => ({ ok: false, error: String(e), __client_elapsed_ms: Date.now() - startTs }));

        // Handle local result as soon as it arrives
        localPromise.then(async (jLocal) => {
          const localPanel = ensureRespEl('local');
          if (!jLocal.ok){ localPanel.textContent = `Local STT error: ${jLocal.error || 'unknown'}`; console.warn('local STT payload', jLocal); return; }
          const localText = (jLocal.transcript || '').trim();
          const localTimings = jLocal.timings || null;
          const clientElapsed = jLocal.__client_elapsed_ms != null ? jLocal.__client_elapsed_ms : null;
          if (clientElapsed != null) console.log('Local STT client elapsed ms:', clientElapsed);
          // Don't show STT text or timings in the blurb; show processing state until LLM replies
          localPanel.textContent = 'waiting...';
          if (localText){
            elPrompt.value = localText;
            try{
              const r = await fetch('/api/chat/local', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ user: localText }) });
              const localModelResult = r.ok ? await r.json() : { ok: false, error: r.statusText };
              const c = localModelResult.content || (localModelResult.resp && JSON.stringify(localModelResult.resp)) || '';
              const llmMs = localModelResult.ms ? `LLM ${localModelResult.ms}ms` : '';
              const sttMsVal = localTimings && localTimings.total_ms ? localTimings.total_ms : (clientElapsed != null ? clientElapsed : null);
              // Blurb: only assistant/tool output
              localPanel.textContent = c.replace(/\n/g,' ');
              // Metrics: persistent block below blurb
              try{
                const mid = document.getElementById('metrics-local');
                if (mid){ _metricsSet('local', 'stt', sttMsVal); _metricsSet('local', 'llm', localModelResult.ms); }
              }catch(e){}
              if (localModelResult.applied) await fetchStateFor('local');
                  // Auto-TTS: only summarize when a tool call was performed; otherwise speak LLM content directly
                  try{
                    const safeText = sanitizeForTTS(c);
                    // stream summarizer + server-side TTS; fallback to client-side compose+stream
                    try{
                      streamSummarizeAndPlay(safeText, localModelResult.applied, 'local', 'local', undefined, playerLocal, startTs).catch(e=>{
                        console.warn('local summarize_stream failed', e);
                        try{ const fb = composeSummary(localModelResult.parsed, localModelResult.applied, safeText); streamTtsSentencesViaFetch(sanitizeForTTS(fb), 'local', undefined, playerLocal, startTs).catch(()=>{}); }catch(err){ console.warn('fallback synth failed', err); }
                      });
                    }catch(e){ console.warn('local summarize_stream error', e); try{ const fb = composeSummary(localModelResult.parsed, localModelResult.applied, safeText); streamTtsSentencesViaFetch(sanitizeForTTS(fb), 'local', undefined, playerLocal, startTs).catch(()=>{}); }catch(err){}
                    }
                  }catch(e){ console.warn('local auto-TTS failed', e); }
            }catch(e){ localPanel.textContent = `Local LLM error: ${e}`; }
          }
        });

        // Handle cloud result as soon as it arrives
        cloudPromise.then(async (jCloud) => {
          const cloudPanel = ensureRespEl('cloud');
          if (!jCloud.ok){ cloudPanel.textContent = `Cloud STT error: ${jCloud.error || 'unknown'}`; console.warn('cloud STT payload', jCloud); return; }
          const cloudText = (jCloud.transcript || '').trim();
          const cloudTimings = jCloud.timings || null;
          const clientElapsedCloud = jCloud.__client_elapsed_ms != null ? jCloud.__client_elapsed_ms : null;
          if (clientElapsedCloud != null) console.log('Cloud STT client elapsed ms:', clientElapsedCloud);
          // Keep blurb limited to tool output; show waiting until LLM returns
          cloudPanel.textContent = 'waiting...';
          if (cloudText){
            try{
              const r2 = await fetch('/api/chat/cloud', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ user: cloudText }) });
              const cloudModelResult = r2.ok ? await r2.json() : { ok: false, error: r2.statusText };
              const c2 = cloudModelResult.content || (cloudModelResult.resp && JSON.stringify(cloudModelResult.resp)) || '';
              const llmMs2 = cloudModelResult.ms ? `LLM ${cloudModelResult.ms}ms` : '';
              const sttMsVal2 = cloudTimings && cloudTimings.total_ms ? cloudTimings.total_ms : (clientElapsedCloud != null ? clientElapsedCloud : null);
              // Blurb: only assistant/tool content
              cloudPanel.textContent = c2.replace(/\n/g,' ');
              // Metrics: persistent block below blurb
              try{
                const mid = document.getElementById('metrics-cloud');
                if (mid){ _metricsSet('cloud', 'stt', sttMsVal2); _metricsSet('cloud', 'llm', cloudModelResult.ms); }
              }catch(e){}
              if (cloudModelResult.applied) await fetchStateFor('cloud');
                  // Auto-TTS: only summarize when a tool call was performed; otherwise speak LLM content directly
                  try{
                    const safeText = sanitizeForTTS(c2);
                    try{
                      streamSummarizeAndPlay(safeText, cloudModelResult.applied, 'cloud', 'cloud', 'sage', playerCloud, startTs).catch(e=>{
                        console.warn('cloud summarize_stream failed', e);
                        try{ const fb = composeSummary(cloudModelResult.parsed, cloudModelResult.applied, safeText); streamTtsSentencesViaFetch(sanitizeForTTS(fb), 'cloud', 'sage', playerCloud, startTs).catch(()=>{}); }catch(err){ console.warn('fallback synth failed', err); }
                      });
                    }catch(e){ console.warn('cloud summarize_stream error', e); try{ const fb = composeSummary(cloudModelResult.parsed, cloudModelResult.applied, safeText); streamTtsSentencesViaFetch(sanitizeForTTS(fb), 'cloud', 'sage', playerCloud, startTs).catch(()=>{}); }catch(err){}
                    }
                  }catch(e){ console.warn('cloud auto-TTS failed', e); }
            }catch(e){ cloudPanel.textContent = `Cloud LLM error: ${e}`; }
          }
        });

        // Clear upload status; results will appear as they arrive
        sttTranscriptEl.textContent = '';

      }catch(e){
        sttTranscriptEl.textContent = `Upload failed: ${e}`;
      }
    };
    mediaRecorder.start();
    recStartBtn.disabled = true;
    recStopBtn.disabled = false;
    sttTranscriptEl.textContent = 'Recording…';
  }catch(e){
    sttTranscriptEl.textContent = 'Microphone access denied';
    console.error(e);
  }
});

recStopBtn.addEventListener('click', ()=>{
  if (mediaRecorder && mediaRecorder.state !== 'inactive'){
    mediaRecorder.stop();
    recStartBtn.disabled = false;
    recStopBtn.disabled = true;
  }
});

// (No STT auto-start/status polling; user starts servers manually.)

// Init both displays
fetchStateFor('local').catch(e=>{ console.error('fetchStateFor local failed', e); if(sttTranscriptEl) sttTranscriptEl.textContent = 'State load error'; });
fetchStateFor('cloud').catch(e=>{ console.error('fetchStateFor cloud failed', e); if(sttTranscriptEl) sttTranscriptEl.textContent = 'State load error'; });
// main thermostat removed; no global state fetch required

// Load filler mode setting
loadFillerMode();

// Subscribe to server-sent events for live state updates
try{
  const stateEs = new EventSource('/api/state/stream');
  stateEs.addEventListener('state', (ev) => {
    try{
      const data = JSON.parse(ev.data);
      if (data.local){
        LOCAL_HOUSE = data.local.house || LOCAL_HOUSE;
        LOCAL_ROOMS = data.local.rooms || LOCAL_ROOMS;
        renderRoomsInto(floorLocal, LOCAL_ROOMS);
        const m = document.getElementById('thermo-mode-local');
        const c = document.getElementById('thermo-current-local');
        const t = document.getElementById('thermo-target-local');
        if (m) m.textContent = (LOCAL_HOUSE.mode === 'heat') ? 'HEAT' : 'OFF';
        if (c) c.textContent = `${Number(LOCAL_HOUSE.current || 0).toFixed(1)}°C`;
        if (t) t.textContent = `Target ${Number(LOCAL_HOUSE.target || 0).toFixed(0)}°C`;
      }
      if (data.cloud){
        CLOUD_HOUSE = data.cloud.house || CLOUD_HOUSE;
        CLOUD_ROOMS = data.cloud.rooms || CLOUD_ROOMS;
        renderRoomsInto(floorCloud, CLOUD_ROOMS);
        const m = document.getElementById('thermo-mode-cloud');
        const c = document.getElementById('thermo-current-cloud');
        const t = document.getElementById('thermo-target-cloud');
        if (m) m.textContent = (CLOUD_HOUSE.mode === 'heat') ? 'HEAT' : 'OFF';
        if (c) c.textContent = `${Number(CLOUD_HOUSE.current || 0).toFixed(1)}°C`;
        if (t) t.textContent = `Target ${Number(CLOUD_HOUSE.target || 0).toFixed(0)}°C`;
      }
      // main thermostat removed
    }catch(e){ console.error('state SSE parse', e); }
  });
  stateEs.addEventListener('error', (e)=>{ console.warn('state SSE error', e); /* keep trying */ });
}catch(e){ console.warn('SSE not supported', e); }
