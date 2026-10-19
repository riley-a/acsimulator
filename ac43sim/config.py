"""Load and validate config.toml into plain dicts the controller can use."""

try:
    import tomllib  # Python 3.11+
except ImportError:  # Raspberry Pi OS Bullseye ships 3.9
    import tomli as tomllib


class ConfigError(ValueError):
    pass


def load(path):
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return normalise(raw)


def normalise(raw):
    cfg = {
        "server": {"host": "0.0.0.0", "port": 8080, **raw.get("server", {})},
        "hardware": {"mode": "auto", **raw.get("hardware", {})},
        "timing": {"rex_pulse_ms": 500, "expected_unlock_s": 5, "input_poll_ms": 100, **raw.get("timing", {})},
        "boards": [],
        "outputs": [],
        "inputs": [],
        "doors": [],
    }
    if cfg["hardware"]["mode"] not in ("auto", "real", "mock"):
        raise ConfigError("hardware.mode must be auto, real or mock")

    boards = {}
    for b in raw.get("boards", []):
        b = {"bus": 1, "enabled": True, **b}
        for key in ("id", "driver", "address"):
            if key not in b:
                raise ConfigError(f"board missing {key!r}: {b}")
        if b["id"] in boards:
            raise ConfigError(f"duplicate board id {b['id']!r}")
        boards[b["id"]] = b
        cfg["boards"].append(b)

    used = {}  # (board, channel) -> output id, catches double-mapped relays

    def add_output(o):
        if o["board"] not in boards:
            raise ConfigError(f"output {o['id']!r} references unknown board {o['board']!r}")
        if not 0 <= o["channel"] <= 15:
            raise ConfigError(f"output {o['id']!r} channel must be 0-15")
        key = (o["board"], o["channel"])
        if key in used:
            raise ConfigError(f"{o['id']!r} and {used[key]!r} both use {o['board']} channel {o['channel']}")
        used[key] = o["id"]
        cfg["outputs"].append(o)

    used_pins = {}

    def add_input(i):
        if i["board"] not in boards:
            raise ConfigError(f"input {i['id']!r} references unknown board {i['board']!r}")
        if not 0 <= i["pin"] <= 15:
            raise ConfigError(f"input {i['id']!r} pin must be 0-15")
        key = (i["board"], i["pin"])
        if key in used_pins:
            raise ConfigError(f"{i['id']!r} and {used_pins[key]!r} both use {i['board']} pin {i['pin']}")
        used_pins[key] = i["id"]
        cfg["inputs"].append(i)

    defaults = {"dpi_invert": False, "lock_active_low": True, "auto_close_s": 0, **raw.get("door_defaults", {})}
    seen_doors = set()
    for d in raw.get("doors", []):
        d = {**defaults, **d}
        n = d["id"]
        if n in seen_doors:
            raise ConfigError(f"duplicate door id {n}")
        seen_doors.add(n)
        door = {
            "id": n,
            "name": d.get("name", f"Door {n}"),
            "auto_close_s": d["auto_close_s"],
            "dpi": None, "rex1": None, "rex2": None, "lock": None,
        }
        for role in ("dpi", "rex1", "rex2"):
            if role in d:
                oid = f"door{n}.{role}"
                add_output({
                    "id": oid,
                    "name": f"{door['name']} {role.upper()}",
                    "group": f"door{n}",
                    "board": d[role]["board"],
                    "channel": d[role]["channel"],
                    "mode": "toggle" if role == "dpi" else "pulse",
                    "pulse_ms": d.get("rex_pulse_ms", cfg["timing"]["rex_pulse_ms"]),
                    "invert": bool(d["dpi_invert"]) if role == "dpi" else False,
                })
                door[role] = oid
        if "lock_sense" in d:
            iid = f"door{n}.lock"
            add_input({
                "id": iid,
                "name": f"{door['name']} lock relay",
                "group": f"door{n}",
                "board": d["lock_sense"]["board"],
                "pin": d["lock_sense"]["pin"],
                "active_low": bool(d["lock_active_low"]),
            })
            door["lock"] = iid
        cfg["doors"].append(door)

    cfg["fai"] = _fai(raw.get("fai"), add_output, add_input)

    for o in raw.get("outputs", []):
        o = {"group": "extra", "mode": "toggle", "pulse_ms": cfg["timing"]["rex_pulse_ms"], "invert": False, **o}
        o.setdefault("name", o["id"])
        if o["mode"] not in ("toggle", "pulse"):
            raise ConfigError(f"output {o['id']!r} mode must be toggle or pulse")
        add_output(o)

    for i in raw.get("inputs", []):
        i = {"group": "extra", "active_low": True, **i}
        i.setdefault("name", i["id"])
        add_input(i)

    ids = [o["id"] for o in cfg["outputs"]] + [i["id"] for i in cfg["inputs"]]
    dupes = {x for x in ids if ids.count(x) > 1}
    if dupes:
        raise ConfigError(f"duplicate point ids: {sorted(dupes)}")
    return cfg


FAI_RELAYS = {
    # role: (label, terminals)
    "mode": ("Mode jumper", "FAI+ ↔ FAIP"),
    "no_trigger": ("NO trigger", "FAI+ ↔ FAI−"),
    "nc_trigger": ("NC trigger", "FAI− ↔ FAIP"),
    "latch": ("Latch loop", "L ↔ L"),
}


def _fai(f, add_output, add_input):
    """Fire Alarm Input simulator: 4 interlocked relays + optional confirmation inputs.

    Logical relay states (before mode_invert):
      mode        True = jumper FAI+<->FAIP fitted (NC mode)
      no_trigger  True = contact closed (alarm in NO mode)
      nc_trigger  True = relay energised, its NC contact OPEN (alarm in NC mode)
      latch       True = relay energised, L<->L loop OPEN
    """
    if not f or not f.get("enabled", True):
        return None
    fai = {
        "default_mode": f.get("default_mode", "no"),
        "isolate_nc_in_no_mode": bool(f.get("isolate_nc_in_no_mode", True)),
        "relays": {},
        "sense": {},
    }
    for role, (label, terms) in FAI_RELAYS.items():
        if role not in f:
            continue
        oid = f"fai.{role}"
        add_output({
            "id": oid,
            "name": label,
            "terminals": terms,
            "group": "fai",
            "board": f[role]["board"],
            "channel": f[role]["channel"],
            "mode": "toggle",
            "pulse_ms": 0,
            "invert": bool(f.get("mode_invert", False)) if role == "mode" else False,
            "managed": True,
        })
        fai["relays"][role] = oid

    r = fai["relays"]
    modes = [m for m, role in (("no", "no_trigger"), ("nc", "nc_trigger")) if role in r]
    if not modes:
        raise ConfigError("[fai] needs at least one of no_trigger / nc_trigger")
    if len(modes) == 2 and "mode" not in r:
        raise ConfigError("[fai] with both NO and NC triggers needs a mode relay")
    if fai["default_mode"] not in modes:
        raise ConfigError(f"[fai] default_mode must be one of {modes}")
    fai["modes"] = modes

    for key, label in (("sense_no", "AC43 FAI output NO"), ("sense_nc", "AC43 FAI output NC")):
        if key in f:
            iid = f"fai.{key}"
            add_input({
                "id": iid,
                "name": label,
                "group": "fai",
                "board": f[key]["board"],
                "pin": f[key]["pin"],
                "active_low": bool(f.get("sense_active_low", True)),
            })
            fai["sense"][key] = iid
    return fai
