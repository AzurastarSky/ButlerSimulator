// frontend/app.js
const API_STATE  = "/api/state";
const API_DEVICE = "/api/device";

// 2×3 plan, fixed order:
const PLAN = [
  "living room", "dining room", "kitchen",
  "bathroom", "bedroom", "office"
];

const floor = document.getElementById("floorplan");
const elMode    = document.getElementById("thermo-mode");
const elCurrent = document.getElementById("thermo-current");
const elTarget  = document.getElementById("thermo-target");

let HOUSE = { target: 20, current: 19, mode: "heat" };
let ROOMS = {};

function titleCase(s){ return s.replace(/\b\w/g, c => c.toUpperCase()); }

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

function renderRooms(){
  floor.innerHTML = "";
  PLAN.forEach(room => {
    const devices = ROOMS[room] || { light: "off" };
    floor.appendChild(roomBox(room, devices));
  });
}

function renderThermo(){
  elMode.textContent    = HOUSE.mode === "heat" ? "HEAT" : "OFF";
  elMode.style.color    = HOUSE.mode === "heat" ? "#30d158" : "#9aa4b2";
  elCurrent.textContent = `${Number(HOUSE.current).toFixed(1)}°C`;
  elTarget.textContent  = `Target ${Number(HOUSE.target).toFixed(0)}°C`;
}

async function fetchState(){
  const res = await fetch(API_STATE);
  if (!res.ok) return;
  const data = await res.json();
  HOUSE = data.house || HOUSE;
  ROOMS = data.rooms || {};
  renderThermo();
  renderRooms();
}

async function sendAction(room, device, action, value){
  await fetch(API_DEVICE, {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ room, device, action, value })
  });
  await fetchState();
}

// Toolbar: lights/blinds scopes
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
  const device = btn.dataset.device;  // light | blinds
  const action = btn.dataset.action;
  if (!scope || !device || !action) return;
  sendAction(scope, device, action);
});

// Init + poll
fetchState();
setInterval(fetchState, 2000);