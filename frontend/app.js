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
const recStartBtn = document.getElementById('rec-start');
const recStopBtn  = document.getElementById('rec-stop');
const sttStatusEl = document.getElementById('stt-status');
const sttTranscriptEl = document.getElementById('stt-transcript');

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
        <button class="btn" data-device="light"  data-action="toggle">Toggle</button>
      </div>
    </div>
  `;

  // Click room to toggle light
  el.addEventListener("click", (e) => {
    if (e.target.closest(".btn")) return;
    sendAction(roomName, "light", "toggle");
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
  return el;
}

// Prompt handling + SSE
function openRaceSSE(text){
  const url = `/api/chat/stream?user=${encodeURIComponent(text)}`;
  const es = new EventSource(url);
  ensureRespEl('local').textContent = 'waiting...';
  ensureRespEl('cloud').textContent = 'waiting...';
  es.addEventListener('model', async (ev) => {
    try{
      const data = JSON.parse(ev.data);
      const who = data.model; // 'local' or 'cloud'
      const ms = data.ms;
      const content = data.content || '';
      ensureRespEl(who).textContent = `(${ms}ms) ${content.replace(/\n/g,' ')} `;
      await fetchStateFor(who);
      const panel = who === 'local' ? floorLocal.parentElement : floorCloud.parentElement;
      panel.style.boxShadow = '0 0 0 3px rgba(102,178,255,0.12)';
      setTimeout(()=> panel.style.boxShadow = '', 800);
    }catch(e){
      console.error(e);
    }
  });
  es.addEventListener('error', (e)=>{ es.close(); });
  return es;
}

elPromptBtn.addEventListener('click', () => {
  const text = elPrompt.value.trim();
  if (!text) return;
  openRaceSSE(text);
});

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
        const localStartTs = Date.now();
        const localPromise = fetch('/api/stt?lang=en', { method: 'POST', body: fdLocal })
          .then(async res => {
            const elapsed = Date.now() - localStartTs;
            const payload = res.ok ? await res.json() : { ok: false, error: res.statusText };
            try{ payload.__client_elapsed_ms = elapsed; }catch(e){}
            return payload;
          })
          .catch(e => ({ ok: false, error: String(e), __client_elapsed_ms: Date.now() - localStartTs }));

        const cloudStartTs = Date.now();
        const cloudPromise = fetch('/api/stt/cloud?lang=en', { method: 'POST', body: fdCloud })
          .then(async res => {
            const elapsed = Date.now() - cloudStartTs;
            const payload = res.ok ? await res.json() : { ok: false, error: res.statusText };
            try{ payload.__client_elapsed_ms = elapsed; }catch(e){}
            return payload;
          })
          .catch(e => ({ ok: false, error: String(e), __client_elapsed_ms: Date.now() - cloudStartTs }));

        // Handle local result as soon as it arrives
        localPromise.then(async (jLocal) => {
          const localPanel = ensureRespEl('local');
          if (!jLocal.ok){ localPanel.textContent = `Local STT error: ${jLocal.error || 'unknown'}`; console.warn('local STT payload', jLocal); return; }
          const localText = (jLocal.transcript || '').trim();
          const localTimings = jLocal.timings || null;
          const clientElapsed = jLocal.__client_elapsed_ms != null ? jLocal.__client_elapsed_ms : null;
          if (clientElapsed != null) console.log('Local STT client elapsed ms:', clientElapsed);
          localPanel.textContent = localText ? `STT: ${localText}` : 'Local STT empty';
          if (localText){
            elPrompt.value = localText;
            try{
              const r = await fetch('/api/chat/local', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ user: localText }) });
              const localModelResult = r.ok ? await r.json() : { ok: false, error: r.statusText };
              const c = localModelResult.content || (localModelResult.resp && JSON.stringify(localModelResult.resp)) || '';
              const llmMs = localModelResult.ms ? `LLM ${localModelResult.ms}ms` : '';
              const sttMs = localTimings && localTimings.total_ms ? `STT ${localTimings.total_ms}ms` : '';
              localPanel.textContent = `(local) ${c.replace(/\n/g,' ')} ${sttMs ? `(${sttMs})` : ''} ${llmMs ? `(${llmMs})` : ''} `;
              if (localModelResult.applied) await fetchStateFor('local');
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
          cloudPanel.textContent = cloudText ? `STT: ${cloudText}` : 'Cloud STT empty';
          if (cloudText){
            try{
              const r2 = await fetch('/api/chat/cloud', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ user: cloudText }) });
              const cloudModelResult = r2.ok ? await r2.json() : { ok: false, error: r2.statusText };
              const c2 = cloudModelResult.content || (cloudModelResult.resp && JSON.stringify(cloudModelResult.resp)) || '';
              const llmMs2 = cloudModelResult.ms ? `LLM ${cloudModelResult.ms}ms` : '';
              const sttMs2 = cloudTimings && cloudTimings.total_ms ? `STT ${cloudTimings.total_ms}ms` : '';
              cloudPanel.textContent = `(cloud) ${c2.replace(/\n/g,' ')} ${sttMs2 ? `(${sttMs2})` : ''} ${llmMs2 ? `(${llmMs2})` : ''} `;
              if (cloudModelResult.applied) await fetchStateFor('cloud');
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
