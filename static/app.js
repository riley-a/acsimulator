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
  const note = el("div", "note");
  txt.append(note);
  p.append(led, txt); // read-only: the state comes from the panel
  container.append(p);
  pointEls.set(i.id, { led, note, board: i.board });
}

// REX buttons:
//   - latched (from the server)  -> any click releases it
//   - door in Latch mode         -> click latches it on until clicked again
//   - otherwise                  -> press-and-hold holds; a quick click still
//                                   gets the full minimum pulse (server side)
const releasers = new Set();

function attachHold(btn, path, opts) {
  btn.title = "Click to pulse · press and hold to hold · Latch mode: click on / click off";
  let holding = null; // the in-flight "hold" request while pressed

  const press = (e) => {
    if (btn.disabled || holding) return;
    e.preventDefault();
    if (opts.isLatched()) return void post(path, { action: "release" });
    if (opts.latchMode()) return void post(path, { action: "latch" });
    btn.classList.add("pressed");
    holding = post(path, { action: "hold" });
    releasers.add(release);
  };
  function release() {
    if (!holding) return;
    const h = holding;
    holding = null;
    releasers.delete(release);
    btn.classList.remove("pressed");
    h.then(() => post(path, { action: "release" })); // never overtakes the hold
  }

  btn.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    try {
      btn.setPointerCapture(e.pointerId); // keep getting pointerup if the finger slides off
    } catch {}
    press(e);
  });
  btn.addEventListener("pointerup", release);
  btn.addEventListener("pointercancel", release);
  btn.addEventListener("lostpointercapture", release);
  btn.addEventListener("contextmenu", (e) => e.preventDefault()); // long-press on touch
  btn.addEventListener("keydown", (e) => {
    if ((e.key === " " || e.key === "Enter") && !e.repeat) press(e);
  });
  btn.addEventListener("keyup", (e) => {
    if (e.key === " " || e.key === "Enter") release();
  });
  btn.addEventListener("blur", release);
}

window.addEventListener("blur", () => releasers.forEach((r) => r()));
window.addEventListener("pagehide", () => {
  // the page is going away mid-hold: tell the server directly
  for (const btn of document.querySelectorAll(".btn.rex.pressed")) {
    const door = btn.closest("[data-door]").dataset.door;
    navigator.sendBeacon(`/api/doors/${door}/rex/${btn.dataset.rex}`, JSON.stringify({ action: "release" }));
  }
});

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

  // Latch mode (per door, remembered in this browser): click REX on, click again off
  const latchBtn = $(".btn.latch-mode", node);
  const latchKey = `ac43.latch.door${d.id}`;
  const setLatchMode = (on) => {
    latchBtn.classList.toggle("active", on);
    latchBtn.setAttribute("aria-pressed", on ? "true" : "false");
    try { localStorage.setItem(latchKey, on ? "1" : "0"); } catch {}
  };
  let saved = false;
  try { saved = localStorage.getItem(latchKey) === "1"; } catch {}
  setLatchMode(saved);
  latchBtn.addEventListener("click", () => setLatchMode(!latchBtn.classList.contains("active")));
  if (!d.rex1 && !d.rex2) latchBtn.disabled = true;

  for (const b of node.querySelectorAll(".btn.rex")) {
    const n = b.dataset.rex;
    const oid = d[`rex${n}`];
    if (!oid) b.disabled = true;
    attachHold(b, `/api/doors/${d.id}/rex/${n}`, {
      latchMode: () => latchBtn.classList.contains("active"),
      isLatched: () => !!state?.outputs[oid]?.latched,
    });
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
  // fixed order: the MicroPython build can't guarantee JSON key order
  const byOrder = (obj, order) => order.filter((k) => obj[k]).map((k) => [k, obj[k]]);
  for (const [role, r] of byOrder(f.relays, ["mode", "no_trigger", "nc_trigger", "latch"])) {
    const p = el("div", "point small");
    const led = el("span", "led");
    const txt = el("div", "name", r.name);
    txt.append(el("div", "sub", `${r.terminals} · ${r.board} ch ${r.channel}`));
    const note = el("div", "note");
    txt.append(note);
    p.append(led, txt);
    box.append(p);
    faiRelayEls[role] = led;
    faiRelayEls[role + "#note"] = note;
  }
  for (const [key, s] of byOrder(f.sense, ["sense_no", "sense_nc"])) {
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

function renderFai(f, snap) {
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

  // Say loudly when the FAI relays aren't real: otherwise the panel shows
  // TRIGGERED while nothing on the bench moves.
  const faiBoards = [...new Set(Object.values(layout.fai.relays).map((r) => r.board))];
  const problems = faiBoards.map((id) => snap.boards.find((b) => b.id === id)).filter(Boolean).map((b) =>
    b.fault ? `FAI relay board "${b.id}" fault: ${b.fault}`
    : b.mock && layout.hardware_mode === "mock" ? `Simulated hardware mode — the FAI relays are not switched.`
    : b.mock ? `FAI relay board "${b.id}" was not found at 0x${b.address.toString(16)} — its relays are SIMULATED, nothing on the bench will switch. Check i2cdetect -y 1 and the board's address jumpers, then restart.`
    : null
  ).filter(Boolean);
  const warn = $("#fai-warn");
  warn.hidden = !problems.length;
  warn.textContent = problems.join(" ");

  const sim = $("#fai-sim");
  sim.className = "chip " + (f.triggered ? "locked" : "closed");
  sim.textContent = `${f.mode.toUpperCase()} mode · ${f.triggered ? "TRIGGERED" : "Normal"}`;

  const conf = $("#fai-confirm");
  const labels = { triggered: "✓ Triggered", normal: "Normal", fault: "Wiring fault", unknown: "No sensor" };
  const cls = { triggered: "locked", normal: "unlocked", fault: "open", unknown: "closed" };
  conf.className = "chip " + cls[f.confirm];
  conf.textContent = labels[f.confirm];

  const hint = $("#fai-hint");
  const pins = Object.values(layout.fai.sense).map((s) => `pin ${s.pin}`).join(" / ");
  hint.textContent =
    f.confirm === "fault" && !f.sense.sense_no && !f.sense.sense_nc
      ? `Neither FAI sense input (${pins}) is pulled low — the AC43 FAI output relay doesn't look connected. ` +
        `Wire COM → GND, NO → sense_no pin, NC → sense_nc pin, or remove sense_no / sense_nc from [fai] if you're not using it.`
    : f.confirm === "fault"
      ? `Both FAI sense inputs (${pins}) are pulled low — a relay can't be on NO and NC at once. Check for a short, or COM wired to both pins.`
    : f.triggered && f.confirm === "normal" ? "Waiting for the AC43 to report the alarm…"
    : !f.triggered && f.confirm === "triggered" ? "AC43 still in alarm — latched? Open the latch loop or reset in Command."
    : "";

  // what each relay is doing right now: [text, LED colour]
  const isolating = f.mode === "no";
  const notes = {
    mode: (on) => on ? ["Jumper fitted — NC mode", ""] : ["Jumper removed — NO mode", ""],
    no_trigger: (on) => on ? ["Contact closed — ALARM", "red"] : ["Contact open", ""],
    nc_trigger: (on) => !on ? ["Loop closed", ""]
      : isolating ? ["Loop open — keeps FAIP isolated (NO mode)", "amber"]
      : ["Loop broken — ALARM", "red"],
    latch: (on) => on ? ["Loop open", "amber"] : ["Loop closed", ""],
  };
  for (const [role, on] of Object.entries(f.relays)) {
    // text describes the circuit (logical state); LED + Energised/Off is the real coil
    const coil = f.energised ? f.energised[role] : on;
    const [text, color] = notes[role](on);
    setLed(faiRelayEls[role], coil, color);
    faiRelayEls[role + "#note"].textContent = (coil ? "Energised · " : "Off · ") + text;
    faiRelayEls[role + "#note"].className = "note" + (color ? " " + color : "");
  }
  for (const [key, active] of Object.entries(f.sense)) setLed(faiRelayEls[key], active === true, "green");
}

// --- render ----------------------------------------------------------------

function setLed(led, on, color) {
  led.className = "led" + (on ? ` on ${color || ""}` : "");
}

let lastSeq = -1;

function render(snap) {
  if (!snap || !snap.doors) return;
  // a button's HTTP response can land after a newer live update - ignore it
  if (typeof snap.seq === "number") {
    if (snap.seq < lastSeq) return;
    lastSeq = snap.seq;
  }
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
      const b = snap.boards.find((x) => x.id === refs.board);
      setLed(refs.led, a === true, "green");
      refs.note.textContent =
        a === true ? "On"
        : a === false ? "Off"
        : b && !b.enabled ? "Sensing board disabled"
        : b && b.fault ? "Sensing board fault"
        : "No reading";
      refs.note.className = "note" + (b && b.enabled && b.fault ? " red" : "");
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
      const o = snap.outputs[d[`rex${b.dataset.rex}`]];
      b.classList.toggle("firing", !!o?.on);
      b.classList.toggle("latched", !!o?.latched);
      b.textContent = `REX ${b.dataset.rex}` + (o?.latched ? " · latched" : o?.held ? " · held" : "");
    }
    const dpiBtn = $(".btn.dpi", node);
    dpiBtn.textContent = d.open ? "Close door" : "Open door";
    dpiBtn.classList.toggle("is-open", !!d.open);
  }

  renderFai(snap.fai, snap);
}

// --- live updates ----------------------------------------------------------

function connect() {
  const conn = $("#conn");
  const es = new EventSource("/api/events");
  es.onopen = () => {
    lastSeq = -1; // the server may have restarted and reset its counter
    conn.className = "conn ok";
    $("#conn-text").textContent = "Live";
  };
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
