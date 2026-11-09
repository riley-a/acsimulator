"""Hardware-facing state machine.

Owns the boards, maps logical points (door1.dpi, aux.in1 ...) to relay
channels / input pins, handles REX pulses and auto-close timers, polls input
boards, and broadcasts a JSON-able snapshot to subscribers whenever anything
changes.
"""

import logging
import queue
import threading
import time

from . import drivers

log = logging.getLogger(__name__)


class PointError(Exception):
    pass


class HardwareError(Exception):
    pass


class Controller:
    def __init__(self, cfg):
        self.cfg = cfg
        self.timing = cfg["timing"]
        self.boards = {}       # id -> driver
        self.board_info = {}   # id -> {driver, address, mock, enabled, fault}
        self.outputs = {o["id"]: o for o in cfg["outputs"]}
        self.inputs = {i["id"]: i for i in cfg["inputs"]}
        self.doors = cfg["doors"]

        self._lock = threading.RLock()
        self._out_state = {oid: False for oid in self.outputs}   # logical state
        self._in_state = {iid: None for iid in self.inputs}      # None = no reading
        self._pulse_until = {}    # output id -> monotonic deadline
        self._held = {}           # output id -> monotonic time the hold/latch started
        self._latched = set()     # held outputs that were latched (click on / click off)
        self._timers = {}         # output id -> threading.Timer
        self._last_rex = {}       # door id -> monotonic time of last REX
        self._subs = set()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._last_snapshot = None
        self._seq = 0             # bumped on every broadcast change
        self._thread = None

        mode = cfg["hardware"]["mode"]
        for spec in cfg["boards"]:
            info = {
                "id": spec["id"], "driver": spec["driver"], "address": spec["address"],
                "enabled": spec["enabled"], "mock": False, "fault": None,
            }
            self.board_info[spec["id"]] = info
            if not spec["enabled"]:
                continue
            board, is_mock = drivers.build_board(spec, mode)
            info["mock"] = is_mock
            self.boards[spec["id"]] = board

        self.fai = cfg.get("fai")
        if self.fai and any(self.outputs[oid]["board"] not in self.boards for oid in self.fai["relays"].values()):
            log.warning("FAI disabled: its relay board is disabled")
            self.fai = None
        self._fai_mode = self.fai["default_mode"] if self.fai else None
        self._fai_triggered = False
        if self.fai:
            self._fai_apply()
            self._fai_sim()

    # --- lifecycle -----------------------------------------------------------

    def start(self):
        self._thread = threading.Thread(target=self._poll_loop, name="io-poll", daemon=True)
        self._thread.start()

    def shutdown(self):
        self._stop.set()
        self._wake.set()
        with self._lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()
        for bid, board in self.boards.items():
            if board.kind == "output":
                try:
                    board.all_off()
                except OSError as e:
                    log.error("all_off failed on %s: %s", bid, e)
        if self._thread:
            self._thread.join(timeout=2)

    # --- outputs -------------------------------------------------------------

    def _output_board(self, oid):
        if oid not in self.outputs:
            raise PointError(f"unknown output {oid!r}")
        o = self.outputs[oid]
        board = self.boards.get(o["board"])
        if board is None:
            raise PointError(f"board {o['board']!r} for {oid!r} is disabled")
        return o, board

    def _write(self, oid, on):
        o, board = self._output_board(oid)
        try:
            board.set(o["channel"], bool(on) ^ o["invert"])
        except OSError as e:
            self.board_info[o["board"]]["fault"] = str(e)
            self._wake.set()
            raise HardwareError(f"{o['board']}: {e}") from e
        self.board_info[o["board"]]["fault"] = None
        self._out_state[oid] = bool(on)

    def _cancel_timer(self, oid):
        t = self._timers.pop(oid, None)
        if t:
            t.cancel()

    def _after(self, oid, seconds, fn):
        self._cancel_timer(oid)
        t = threading.Timer(seconds, fn)
        t.daemon = True
        self._timers[oid] = t
        t.start()

    def set_output(self, oid, on):
        with self._lock:
            self._cancel_timer(oid)
            self._pulse_until.pop(oid, None)
            self._held.pop(oid, None)
            self._latched.discard(oid)
            self._write(oid, on)
            self._on_output_change(oid, on)
        self._wake.set()

    def toggle_output(self, oid):
        with self._lock:
            self.set_output(oid, not self._out_state.get(oid, False))

    def pulse_output(self, oid, ms=None):
        with self._lock:
            o, _ = self._output_board(oid)
            ms = ms or o.get("pulse_ms") or self.timing["rex_pulse_ms"]
            self._write(oid, True)
            self._pulse_until[oid] = time.monotonic() + ms / 1000
            self._on_output_change(oid, True)
            self._after(oid, ms / 1000, lambda: self._end_pulse(oid))
        self._wake.set()

    def hold_output(self, oid, latch=False):
        """Energise until release_output().

        hold  (press-and-hold): released after rex_max_hold_s in case the
              browser never lets go.
        latch (click on, click off): stays on until released on purpose;
              rex_max_latch_s > 0 adds a limit, 0 = none.
        """
        with self._lock:
            self._output_board(oid)
            self._cancel_timer(oid)
            self._pulse_until.pop(oid, None)
            self._write(oid, True)
            self._held[oid] = time.monotonic()
            if latch:
                self._latched.add(oid)
            else:
                self._latched.discard(oid)
            self._on_output_change(oid, True)
            limit = self.timing["rex_max_latch_s"] if latch else self.timing["rex_max_hold_s"]
            if limit and limit > 0:
                self._after(oid, limit, lambda: self._end_pulse(oid))
        self._wake.set()

    def release_output(self, oid):
        """End a hold or latch. A short tap still gets the full minimum pulse."""
        with self._lock:
            self._latched.discard(oid)
            started = self._held.pop(oid, None)
            if started is None:
                return  # never held, or already released by the safety timer
            o = self.outputs[oid]
            min_s = (o.get("pulse_ms") or self.timing["rex_pulse_ms"]) / 1000
            remaining = min_s - (time.monotonic() - started)
            self._on_rex_release(oid)
            if remaining > 0:
                self._pulse_until[oid] = time.monotonic() + remaining
                self._after(oid, remaining, lambda: self._end_pulse(oid))
            else:
                self._cancel_timer(oid)
                self._end_pulse(oid)
        self._wake.set()

    def _end_pulse(self, oid):
        with self._lock:
            self._timers.pop(oid, None)
            self._pulse_until.pop(oid, None)
            if self._held.pop(oid, None) is not None:
                log.warning("%s reached its hold/latch time limit - released", oid)
                self._latched.discard(oid)
                self._on_rex_release(oid)
            try:
                self._write(oid, False)
            except HardwareError as e:
                log.error("failed to release pulse on %s: %s", oid, e)
        self._wake.set()

    def all_off(self):
        """Release every free output and clear the fire alarm (keeps FAI mode)."""
        with self._lock:
            for oid, o in self.outputs.items():
                if o.get("managed") or o["board"] not in self.boards:
                    continue
                self.set_output(oid, False)
            if self.fai:
                self.fai_set_triggered(False)
                if "latch" in self.fai["relays"]:
                    self.fai_set_latch(False)
        self._wake.set()

    def action(self, oid, action):
        if oid not in self.outputs:
            raise PointError(f"unknown output {oid!r}")
        if self.outputs[oid].get("managed"):
            raise PointError(f"{oid!r} is interlocked - use the FAI controls")
        if action == "on":
            self.set_output(oid, True)
        elif action == "off":
            self.set_output(oid, False)
        elif action == "toggle":
            self.toggle_output(oid)
        elif action == "pulse":
            self.pulse_output(oid)
        elif action == "hold":
            self.hold_output(oid)
        elif action == "latch":
            self.hold_output(oid, latch=True)
        elif action == "release":
            self.release_output(oid)
        else:
            raise PointError(f"unknown action {action!r}")

    def _door_for(self, oid):
        for d in self.doors:
            for role in ("dpi", "rex1", "rex2"):
                if d[role] == oid:
                    return d, role
        return None, None

    def _on_output_change(self, oid, on):
        """Door-level side effects: REX timestamps, auto-close, mock lock sim."""
        door, role = self._door_for(oid)
        if not door:
            return
        if role in ("rex1", "rex2") and on:
            self._last_rex[door["id"]] = time.monotonic()
            self._simulate_lock(door)
        if role == "dpi" and on and door["auto_close_s"] > 0:
            self._after(oid, door["auto_close_s"], lambda: self._auto_close(oid))

    def _on_rex_release(self, oid):
        """The expected-unlock window runs from when a held REX is let go."""
        door, role = self._door_for(oid)
        if door and role in ("rex1", "rex2"):
            self._last_rex[door["id"]] = time.monotonic()
            self._simulate_lock(door)

    def _rex_held(self, door):
        return any(door[r] in self._held for r in ("rex1", "rex2") if door[r])

    def _auto_close(self, oid):
        with self._lock:
            self._timers.pop(oid, None)
            try:
                self._write(oid, False)
            except HardwareError as e:
                log.error("auto-close failed on %s: %s", oid, e)
        self._wake.set()

    def _simulate_lock(self, door):
        """When the sense board is a mock, pretend the AC43 unlocks on REX."""
        iid = door["lock"]
        if not iid:
            return
        board = self.boards.get(self.inputs[iid]["board"])
        if not isinstance(board, drivers.MockInput):
            return
        self.sim_input(iid, True)
        key = f"{iid}#sim"
        # doors stay unlocked while a fire alarm is active or a REX is held
        self._after(key, self.timing["expected_unlock_s"],
                    lambda: self.sim_input(iid, self._fai_triggered or self._rex_held(door)))

    # --- FAI (fire alarm input) ----------------------------------------------

    def _fai_targets(self, mode, triggered):
        """Logical state of each FAI relay for a given mode / alarm state."""
        return {
            "mode": mode == "nc",
            "no_trigger": mode == "no" and triggered,
            # in NO mode keep the NC relay energised so FAIP isn't tied to FAI-
            "nc_trigger": (mode == "nc" and triggered)
                          or (mode == "no" and self.fai["isolate_nc_in_no_mode"]),
        }

    def _fai_apply(self):
        """Drive the relays in an order that never presents a false alarm.

        -> NO: drop the jumper first (loop closed, so NC still reads normal),
               then open the NC loop, then the NO contact.
        -> NC: close the NC loop first, then fit the jumper.
        """
        mode = self._fai_mode
        order = ("mode", "nc_trigger", "no_trigger") if mode == "no" else ("no_trigger", "nc_trigger", "mode")
        targets = self._fai_targets(mode, self._fai_triggered)
        for role in order:
            oid = self.fai["relays"].get(role)
            if oid and self._out_state[oid] != targets[role]:
                self._write(oid, targets[role])

    def _fai_sim(self):
        """Mock sense board: act like the AC43 FAI output relay + door unlocks."""
        sense = self.fai["sense"]
        boards = {self.inputs[iid]["board"] for iid in sense.values()}
        if not boards or not all(isinstance(self.boards.get(b), drivers.MockInput) for b in boards):
            return

        def respond():
            with self._lock:
                on = self._fai_triggered
                if "sense_no" in sense:
                    self.sim_input(sense["sense_no"], on)
                if "sense_nc" in sense:
                    self.sim_input(sense["sense_nc"], not on)
                for d in self.doors:
                    if d["lock"] and isinstance(self.boards.get(self.inputs[d["lock"]]["board"]), drivers.MockInput):
                        rex_timer = self._timers.get(f"{d['lock']}#sim")
                        if not (rex_timer and rex_timer.is_alive()):
                            self.sim_input(d["lock"], on)

        self._after("fai#sim", 0.3, respond)

    def fai_set_triggered(self, on):
        if not self.fai:
            raise PointError("FAI not configured")
        with self._lock:
            self._fai_triggered = bool(on)
            self._fai_apply()
            self._fai_sim()
        self._wake.set()

    def fai_set_mode(self, mode):
        if not self.fai:
            raise PointError("FAI not configured")
        if mode not in self.fai["modes"]:
            raise PointError(f"FAI mode must be one of {self.fai['modes']}")
        with self._lock:
            if self._fai_triggered and mode != self._fai_mode:
                raise PointError("clear the fire alarm before changing FAI mode")
            self._fai_mode = mode
            self._fai_apply()
        self._wake.set()

    def fai_set_latch(self, open_):
        if not self.fai or "latch" not in self.fai["relays"]:
            raise PointError("FAI latch relay not configured")
        with self._lock:
            self._write(self.fai["relays"]["latch"], bool(open_))
        self._wake.set()

    def fai_confirm(self):
        """What the AC43's FAI output relay reports: triggered/normal/fault/unknown."""
        sense = self.fai["sense"]
        no = self._in_state.get(sense["sense_no"]) if "sense_no" in sense else "absent"
        nc = self._in_state.get(sense["sense_nc"]) if "sense_nc" in sense else "absent"
        if no is None or nc is None or (no == "absent" and nc == "absent"):
            return "unknown"
        if no == "absent":
            return "normal" if nc else "triggered"
        if nc == "absent":
            return "triggered" if no else "normal"
        if no and not nc:
            return "triggered"
        if nc and not no:
            return "normal"
        return "fault"  # both or neither: wire off or relay stuck

    def fai_state(self):
        if not self.fai:
            return None
        return {
            "mode": self._fai_mode,
            "triggered": self._fai_triggered,
            "latch_open": self._out_state.get(self.fai["relays"].get("latch"), False),
            "relays": {role: self._out_state[oid] for role, oid in self.fai["relays"].items()},
            # what the relay coil is actually doing (after mode_invert / nc_trigger_contacts)
            "energised": {role: self._out_state[oid] != self.outputs[oid]["invert"]
                          for role, oid in self.fai["relays"].items()},
            "sense": {k: self._in_state.get(iid) for k, iid in self.fai["sense"].items()},
            "confirm": self.fai_confirm(),
        }

    # --- inputs --------------------------------------------------------------

    def sim_input(self, iid, active):
        """Drive a mock input pin (only allowed when its board is a mock)."""
        if iid not in self.inputs:
            raise PointError(f"unknown input {iid!r}")
        i = self.inputs[iid]
        board = self.boards.get(i["board"])
        if not isinstance(board, drivers.MockInput):
            raise PointError(f"{iid!r} is not on a mock board")
        board.set_pin(i["pin"], (not active) if i["active_low"] else active)
        self._wake.set()

    def _read_inputs(self):
        by_board = {}
        for iid, i in self.inputs.items():
            by_board.setdefault(i["board"], []).append(i)
        for bid, points in by_board.items():
            board = self.boards.get(bid)
            if board is None:
                for i in points:
                    self._in_state[i["id"]] = None
                continue
            try:
                levels = board.read_all()
                self.board_info[bid]["fault"] = None
            except OSError as e:
                if self.board_info[bid]["fault"] is None:
                    log.error("read failed on %s: %s", bid, e)
                self.board_info[bid]["fault"] = str(e)
                for i in points:
                    self._in_state[i["id"]] = None
                continue
            for i in points:
                high = bool(levels & (1 << i["pin"]))
                self._in_state[i["id"]] = (not high) if i["active_low"] else high

    def _poll_loop(self):
        period = self.timing["input_poll_ms"] / 1000
        while not self._stop.is_set():
            with self._lock:
                self._read_inputs()
                snap = self.snapshot()
                comparable = {k: v for k, v in snap.items() if k not in ("ts", "seq")}
                changed = comparable != self._last_snapshot
                if changed:
                    # seq lets the browser drop an older snapshot (e.g. a slow
                    # POST response) that arrives after a newer live update
                    self._last_snapshot = comparable
                    self._seq += 1
                    snap["seq"] = self._seq
            if changed:
                self._broadcast(snap)
            self._wake.wait(period)
            self._wake.clear()

    # --- state ---------------------------------------------------------------

    def door_state(self, d, now=None):
        now = now if now is not None else time.monotonic()
        open_ = self._out_state.get(d["dpi"], False) if d["dpi"] else None
        rex_recent = now - self._last_rex.get(d["id"], -1e9) < self.timing["expected_unlock_s"]
        sensed = self._in_state.get(d["lock"]) if d["lock"] else None
        if sensed is not None:
            lock, source = ("unlocked" if sensed else "locked"), "sensor"
        else:
            unlocked = rex_recent or self._fai_triggered or self._rex_held(d)
            lock, source = ("unlocked" if unlocked else "locked"), "expected"
        return {
            "id": d["id"],
            "name": d["name"],
            "open": open_,
            "lock": lock,
            "lock_source": source,
            "dpi": d["dpi"],
            "rex1": d["rex1"],
            "rex2": d["rex2"],
            "lock_input": d["lock"],
        }

    def snapshot(self):
        with self._lock:
            now = time.monotonic()
            return {
                "ts": time.time(),
                "seq": self._seq,
                "boards": [dict(v) for v in self.board_info.values()],
                "outputs": {oid: {"on": on, "pulsing": oid in self._pulse_until, "held": oid in self._held and oid not in self._latched,
                                   "latched": oid in self._latched}
                            for oid, on in self._out_state.items()},
                "inputs": {iid: {"active": v} for iid, v in self._in_state.items()},
                "doors": [self.door_state(d, now) for d in self.doors],
                "fai": self.fai_state(),
            }

    def layout(self):
        """Static description the UI uses to build itself."""
        return {
            "doors": [{k: d[k] for k in ("id", "name", "dpi", "rex1", "rex2", "lock")} for d in self.doors],
            "outputs": [{k: o[k] for k in ("id", "name", "group", "mode", "board", "channel")}
                        for o in self.outputs.values() if not o.get("managed")],
            "fai": self.fai and {
                "modes": self.fai["modes"],
                "relays": {role: {k: self.outputs[oid].get(k) for k in ("id", "name", "terminals", "board", "channel")}
                           for role, oid in self.fai["relays"].items()},
                "sense": {k: {kk: self.inputs[iid][kk] for kk in ("id", "name", "board", "pin")}
                          for k, iid in self.fai["sense"].items()},
            },
            "inputs": [{k: i[k] for k in ("id", "name", "group", "board", "pin")}
                       for i in self.inputs.values() if not (self.fai and i["id"] in self.fai["sense"].values())],
            "boards": [dict(v) for v in self.board_info.values()],
            "timing": self.timing,
            "hardware_mode": self.cfg["hardware"]["mode"],
        }

    # --- pub/sub -------------------------------------------------------------

    def subscribe(self):
        q = queue.Queue(maxsize=50)
        with self._lock:
            self._subs.add(q)
        q.put(self.snapshot())
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs.discard(q)

    def _broadcast(self, snap):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(snap)
            except queue.Full:  # slow client - drop oldest
                try:
                    q.get_nowait()
                    q.put_nowait(snap)
                except queue.Empty:
                    pass
