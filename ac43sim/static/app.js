"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

let layout = null;
let state = null;
const doorEls = new Map();    // door id -> section element
const pointEls = new Map();   // output/input id -> {led, btn}

// --- API -------------------------------------------------------------------

async function post(path, body) {
  try {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    render(data);
  } catch (e) {
    toast(e.message, true);
  }
}

let toastTimer;
function toast(msg, error = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (error ? " error" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 3500);
}

// --- build -----------------------------------------------------------------

function boardById(id) {
  return (state || layout).boards.find((b) => b.id === id);
}

function buildStatusLeds() {
  const box = $("#status-leds");
  box.innerHTML = "";
  const items = [{ id: "_link", label: "Link" }].concat(
    layout.boards.map((b) => ({ id: b.id, label: b.id }))
  );
  for (const it of items) {
    const w = el("div", "status-led");
    const led = el("span", "led");
    led.dataset.board = it.id;
    w.append(led, el("span", null, it.label));
    w.title = it.id === "_link" ? "Browser ↔ Pi connection" : `Board "${it.id}"`;
    box.append(w);
  }
}

function buildPoint(o, container) {
  const p = el("div", "point");
  const led = el("span", "led");
  const txt = el("div", "name", o.name);
  txt.append(el("div", "sub", `${o.board} · ch ${o.channel}`));
  const btn = el("button", "btn", o.mode === "pulse" ? "Pulse" : "Toggle");
  btn.addEventListener("click", () =>
    post(`/api/outputs/${encodeURIComponent(o.id)}`, { action: o.mode === "pulse" ? "pulse" : "toggle" })
  );
  p.append(led, txt, btn);
  container.append(p);
  pointEls.set(o.id, { led, btn, mode: o.mode });
}

function buildInputPoint(i, container) {
  const p = el("div", "point");
  const led = el("span", "led");
  const txt = el("div", "name", i.name);
  txt.append(el("div", "sub", `${i.board} · pin ${i.pin} (input)`));
  p.append(led, txt);
  container.append(p);
  pointEls.set(i.id, { led });
}

function buildDoor(d) {
  const node = $("#door-tpl").content.firstElementChild.cloneNode(true);
  node.dataset.door = d.id;
  $(".door-num", node).textContent = d.id;
  $(".door-name", node).textContent = d.name;

  const visual = $(".door-visual", node);
  const toggleDoor = () => {
    if (!d.dpi) return;
    const open = state && state.doors.find((x) => x.id === d.id)?.open;
    post(`/api/doors/${d.id}/${open ? "close" : "open"}`);
  };
  visual.addEventListener("click", toggleDoor);
  visual.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleDoor(); }
  });

  for (const b of node.querySelectorAll(".btn.rex")) {
    const n = b.dataset.rex;
    if (!d[`rex${n}`]) b.disabled = true;
    b.addEventListener("click", () => post(`/api/doors/${d.id}/rex/${n}`));
  }
  const dpiBtn = $(".btn.dpi", node);
  if (!d.dpi) dpiBtn.disabled = true;
  dpiBtn.addEventListener("click", toggleDoor);

  doorEls.set(d.id, node);
  return node;
}

function build() {
  buildStatusLeds();

  const aux = $("#aux");
  aux.innerHTML = "";
  layout.outputs.filter((o) => o.group === "aux").forEach((o) => buildPoint(o, aux));
  layout.inputs.filter((i) => i.group === "aux").forEach((i) => buildInputPoint(i, aux));
  if (!aux.children.length) aux.append(el("div", "fai-body", "No AUX points configured"));

  const doors = $("#doors");
  doors.innerHTML = "";
  layout.doors.forEach((d) => doors.append(buildDoor(d)));

  // any other groups from config.toml get their own generic section
  const known = new Set(["aux", "fai", ...layout.doors.map((d) => `door${d.id}`)]);
  const groups = {};
  for (const o of layout.outputs) if (!known.has(o.group)) (groups[o.group] ||= { o: [], i: [] }).o.push(o);
  for (const i of layout.inputs) if (!known.has(i.group)) (groups[i.group] ||= { o: [], i: [] }).i.push(i);
  const gbox = $("#groups");
  gbox.innerHTML = "";
  for (const [name, g] of Object.entries(groups)) {
    const sec = el("section", "row");
    const left = el("div", "col-left");
    const main = el("div", "col-main");
    main.append(el("div", "section-title", name.toUpperCase()));
    const body = el("div", "group-body");
    g.o.forEach((o) => buildPoint(o, body));
    g.i.forEach((i) => buildInputPoint(i, body));
    main.append(body);
    sec.append(left, main);
    gbox.append(sec);
  }

  const fai = $("#fai-body");
  fai.innerHTML = "";
  if (layout.fai) fai.append(buildFai(layout.fai));
  const faiOut = layout.outputs.filter((o) => o.group === "fai");
  const faiIn = layout.inputs.filter((i) => i.group === "fai");
  if (faiOut.length || faiIn.length) {
    const body = el("div", "group-body");
    faiOut.forEach((o) => buildPoint(o, body));
    faiIn.forEach((i) => buildInputPoint(i, body));
    fai.append(body);
  } else if (!layout.fai) {
    fai.append(el("div", "future", "Fire alarm input not configured — see [fai] in config.toml."));
  }
}

const faiRelayEls = {};

function buildFai(f) {
  const node = $("#fai-tpl").content.firstElementChild.cloneNode(true);
  for (const b of node.querySelectorAll(".seg .btn")) {
    if (!f.modes.includes(b.dataset.mode)) b.disabled = true;
    b.addEventListener("click", () => post("/api/fai/mode", { mode: b.dataset.mode }));
  }
  $("#fai-fire", node).addEventListener("click", () =>
    post(state?.fai?.triggered ? "/api/fai/clear" : "/api/fai/trigger")
  );
  const latch = $("#fai-latch", node);
  if (!f.relays.latch) latch.hidden = true;
  latch.addEventListener("click", () => post("/api/fai/latch", { open: !state?.fai?.latch_open }));

  const box = $("#fai-relays", node);
  for (const [role, r] of Object.entries(f.relays)) {
    const p = el("div", "point small");
    const led = el("span", "led");
    const txt = el("div", "name", r.name);
    txt.append(el("div", "sub", `${r.terminals} · ${r.board} ch ${r.channel}`));
    p.append(led, txt);
    box.append(p);
    faiRelayEls[role] = led;
  }
  for (const [key, s] of Object.entries(f.sense)) {
    const p = el("div", "point small");
    const led = el("span", "led");
    const txt = el("div", "name", s.name);
    txt.append(el("div", "sub", `${s.board} · pin ${s.pin} (input)`));
    p.append(led, txt);
    box.append(p);
    faiRelayEls[key] = led;
  }
  return node;
}

function renderFai(f) {
  const banner = $("#fire-banner");
  if (!f) { banner.hidden = true; return; }
  const confirmed = f.confirm === "triggered";
  banner.hidden = !(f.triggered || confirmed);
  setLed($("#fai-led"), f.confirm === "unknown" ? f.triggered : confirmed, "red");

  for (const b of document.querySelectorAll("#fai-body .seg .btn")) {
    b.classList.toggle("active", b.dataset.mode === f.mode);
    if (layout.fai.modes.includes(b.dataset.mode)) b.disabled = f.triggered && b.dataset.mode !== f.mode;
  }
  const fire = $("#fai-fire");
  fire.textContent = f.triggered ? "Clear fire alarm" : "Trigger fire alarm";
  fire.classList.toggle("armed", f.triggered);
  $("#fai-latch").textContent = `Latch loop: ${f.latch_open ? "Open" : "Closed"}`;
  $("#fai-latch").classList.toggle("active", f.latch_open);

  const sim = $("#fai-sim");
  sim.className = "chip " + (f.triggered ? "locked" : "closed");
  sim.textContent = `${f.mode.toUpperCase()} mode · ${f.triggered ? "TRIGGERED" : "Normal"}`;

  const conf = $("#fai-confirm");
  const labels = { triggered: "✓ Triggered", normal: "Normal", fault: "Wiring fault", unknown: "No sensor" };
  const cls = { triggered: "locked", normal: "unlocked", fault: "open", unknown: "closed" };
  conf.className = "chip " + cls[f.confirm];
  conf.textContent = labels[f.confirm];

  const hint = $("#fai-hint");
  hint.textContent =
    f.confirm === "fault" ? "NO and NC inputs disagree — check the wiring to the AC43 FAI output relay."
    : f.triggered && f.confirm === "normal" ? "Waiting for the AC43 to report the alarm…"
    : !f.triggered && f.confirm === "triggered" ? "AC43 still in alarm — latched? Open the latch loop or reset in Command."
    : "";

  for (const [role, on] of Object.entries(f.relays)) setLed(faiRelayEls[role], on);
  for (const [key, active] of Object.entries(f.sense)) setLed(faiRelayEls[key], active === true, "green");
}

// --- render ----------------------------------------------------------------

function setLed(led, on, color) {
  led.className = "led" + (on ? ` on ${color || ""}` : "");
}

function render(snap) {
  if (!snap || !snap.doors) return;
  state = snap;

  // status LEDs + mode
  const anyMock = snap.boards.some((b) => b.enabled && b.mock);
  $("#mode").innerHTML = anyMock
    ? '<span class="pill mock">SIMULATED HARDWARE</span>'
    : '<span class="pill live">LIVE I2C</span>';
  for (const led of document.querySelectorAll("#status-leds .led")) {
    const id = led.dataset.board;
    if (id === "_link") { setLed(led, true, "green"); continue; }
    const b = snap.boards.find((x) => x.id === id);
    led.parentElement.title = !b.enabled ? `${id}: disabled in config`
      : b.fault ? `${id}: FAULT — ${b.fault}`
      : `${id}: ${b.driver} @ 0x${b.address.toString(16)}${b.mock ? " (mock)" : ""}`;
    if (!b.enabled) setLed(led, false);
    else if (b.fault) setLed(led, true, "red");
    else setLed(led, true, b.mock ? "amber" : "green");
  }

  // generic points
  for (const [id, refs] of pointEls) {
    if (snap.outputs[id]) {
      const on = snap.outputs[id].on;
      setLed(refs.led, on);
      if (refs.mode !== "pulse") {
        refs.btn.classList.toggle("active", on);
        refs.btn.textContent = on ? "On" : "Off";
      } else {
        refs.btn.classList.toggle("active", on);
      }
    } else if (snap.inputs[id]) {
      const a = snap.inputs[id].active;
      setLed(refs.led, a === true, "green");
      refs.led.title = a == null ? "no reading" : a ? "active" : "inactive";
    }
  }

  // doors
  for (const d of snap.doors) {
    const node = doorEls.get(d.id);
    if (!node) continue;
    const visual = $(".door-visual", node);
    const unlocked = d.lock === "unlocked";
    visual.classList.toggle("open", !!d.open);
    visual.classList.toggle("unlocked", unlocked);
    visual.classList.toggle("expected", d.lock_source !== "sensor");

    const dc = $(".door-chip", node);
    dc.className = "chip door-chip " + (d.open ? "open" : "closed");
    dc.textContent = d.open == null ? "No DPI" : d.open ? "Door open" : "Door closed";

    const lc = $(".lock-chip", node);
    lc.className = "chip lock-chip " + d.lock;
    lc.textContent = unlocked ? "Unlocked" : "Locked";

    const src = $(".lock-source", node);
    const sense = d.lock_input && snap.boards.find((b) => layout.inputs.find((i) => i.id === d.lock_input)?.board === b.id);
    if (d.lock_source === "sensor") {
      src.className = "lock-source confirmed";
      src.textContent = `✓ Confirmed by lock sensor${sense && sense.mock ? " (simulated)" : ""}`;
    } else if (sense && sense.enabled && sense.fault) {
      src.className = "lock-source fault";
      src.textContent = "Lock sensor fault — showing expected state";
    } else {
      src.className = "lock-source";
      src.textContent = !unlocked ? "Assumed — no lock sensor"
        : snap.fai?.triggered ? "Expected (fire alarm) — no lock sensor"
        : "Expected after REX — no lock sensor";
    }

    setLed($(".trigger .led", node), unlocked, "green");

    for (const role of ["dpi", "rex1", "rex2"]) {
      const oid = d[role];
      const on = !!(oid && snap.outputs[oid] && snap.outputs[oid].on);
      setLed($(`.tl[data-role="${role}"] .led`, node), on);
    }
    for (const b of node.querySelectorAll(".btn.rex")) {
      const oid = d[`rex${b.dataset.rex}`];
      b.classList.toggle("firing", !!(oid && snap.outputs[oid]?.on));
    }
    const dpiBtn = $(".btn.dpi", node);
    dpiBtn.textContent = d.open ? "Close door" : "Open door";
    dpiBtn.classList.toggle("is-open", !!d.open);
  }

  renderFai(snap.fai);
}

// --- live updates ----------------------------------------------------------

function connect() {
  const conn = $("#conn");
  const es = new EventSource("/api/events");
  es.onopen = () => { conn.className = "conn ok"; $("#conn-text").textContent = "Live"; };
  es.onmessage = (e) => render(JSON.parse(e.data));
  es.onerror = () => {
    conn.className = "conn bad";
    $("#conn-text").textContent = "Disconnected — retrying…";
    const link = $('#status-leds .led[data-board="_link"]');
    if (link) setLed(link, true, "red");
  };
}

async function init() {
  $("#all-off").addEventListener("click", () => post("/api/all-off"));
  const r = await fetch("/api/layout");
  layout = await r.json();
  build();
  render(await (await fetch("/api/state")).json());
  connect();
}

init().catch((e) => toast("Failed to load: " + e.message, true));
