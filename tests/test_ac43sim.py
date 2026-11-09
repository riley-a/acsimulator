"""Run with:  python -m unittest discover -s tests -v   (from the ac43-simulator folder)"""

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ac43sim import config as config_mod  # noqa: E402
from ac43sim import drivers  # noqa: E402
from ac43sim.controller import Controller, PointError  # noqa: E402


class FakeBus:
    """Register-level fake of an I2C bus with one or more chips on it."""

    def __init__(self):
        self.regs = {}   # (addr, reg) -> byte
        self.present = set()
        self.writes = []

    def write_block(self, addr, reg, data):
        if addr not in self.present:
            raise OSError(121, "Remote I/O error")
        self.writes.append((addr, reg, list(data)))
        for i, b in enumerate(data):
            self.regs[(addr, reg + i)] = b

    def read_block(self, addr, reg, length):
        if addr not in self.present:
            raise OSError(121, "Remote I/O error")
        return [self.regs.get((addr, reg + i), 0) for i in range(length)]

    def probe(self, addr):
        return addr in self.present


class XL9535Tests(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        self.bus.present.add(0x20)
        self.bus.regs[(0x20, 0x02)] = 0xAA  # garbage left from before
        self.r = drivers.XL9535Relay(self.bus, 0x20)

    def out(self):
        return self.bus.regs[(0x20, 0x02)] | (self.bus.regs[(0x20, 0x03)] << 8)

    def test_init_clears_outputs_and_enables_circuits(self):
        self.assertEqual(self.out(), 0)
        self.assertEqual(self.bus.regs[(0x20, 0x06)], 0)  # config low = circuits enabled
        self.assertEqual(self.bus.regs[(0x20, 0x07)], 0)
        self.assertEqual(self.bus.regs[(0x20, 0x04)], 0)  # inversion off

    def test_channel_bit_mapping_port_a_and_b(self):
        self.r.set(0, True)
        self.assertEqual(self.out(), 0x0001)
        self.r.set(8, True)   # B0
        self.assertEqual(self.out(), 0x0101)
        self.r.set(15, True)  # B7
        self.assertEqual(self.out(), 0x8101)
        self.r.set(0, False)
        self.assertEqual(self.out(), 0x8100)
        self.assertTrue(self.r.get(15))
        self.assertFalse(self.r.get(0))

    def test_set_preserves_other_channels(self):
        for ch in range(16):
            self.r.set(ch, True)
        self.assertEqual(self.out(), 0xFFFF)
        self.r.set(7, False)
        self.assertEqual(self.out(), 0xFF7F)

    def test_all_off(self):
        self.r.set(3, True)
        self.r.set(12, True)
        self.r.all_off()
        self.assertEqual(self.out(), 0)

    def test_range_check(self):
        with self.assertRaises(AssertionError):
            self.r.set(16, True)

    def test_missing_board(self):
        with self.assertRaises(OSError):
            drivers.XL9535Relay(self.bus, 0x27)


class MCP23017Tests(unittest.TestCase):
    def test_configures_inputs_with_pullups_and_reads_both_ports(self):
        bus = FakeBus()
        bus.present.add(0x21)
        m = drivers.MCP23017Input(bus, 0x21, pullups=True)
        self.assertEqual(bus.regs[(0x21, 0x00)], 0xFF)  # IODIRA all input
        self.assertEqual(bus.regs[(0x21, 0x01)], 0xFF)  # IODIRB
        self.assertEqual(bus.regs[(0x21, 0x0C)], 0xFF)  # GPPUA
        self.assertEqual(bus.regs[(0x21, 0x0D)], 0xFF)  # GPPUB
        bus.regs[(0x21, 0x12)] = 0b11111110  # GPA0 low
        bus.regs[(0x21, 0x13)] = 0b01111111  # GPB7 low
        self.assertEqual(m.read_all(), 0x7FFE)


class ConfigTests(unittest.TestCase):
    def test_shipped_config_maps_16_relays(self):
        cfg = config_mod.load(ROOT / "config.toml")
        chans = {(o["board"], o["channel"]): o["id"] for o in cfg["outputs"] if o["board"] == "relays"}
        expected = {}
        for n in range(1, 5):
            base = (n - 1) * 3
            expected[("relays", base)] = f"door{n}.dpi"
            expected[("relays", base + 1)] = f"door{n}.rex1"
            expected[("relays", base + 2)] = f"door{n}.rex2"
        expected[("relays", 12)] = "aux.in1"
        expected[("relays", 13)] = "aux.in2"
        self.assertEqual(chans, expected)

    def test_shipped_config_sense_board(self):
        cfg = config_mod.load(ROOT / "config.toml")
        sense = next(b for b in cfg["boards"] if b["id"] == "sense")
        self.assertEqual(sense["driver"], "mcp23017")
        self.assertFalse(sense["enabled"])
        pins = {i["id"]: i["pin"] for i in cfg["inputs"]}
        self.assertEqual(pins, {"door1.lock": 0, "door2.lock": 1, "door3.lock": 2, "door4.lock": 3,
                                "fai.sense_no": 4, "fai.sense_nc": 5,
                                "aux.out1": 6, "aux.out2": 7})
        boards = {i["id"]: i["board"] for i in cfg["inputs"]}
        self.assertEqual((boards["aux.out1"], boards["aux.out2"]), ("sense", "sense"),
                         "AUX OUT is read from the sensing board, not driven")

    def test_shipped_config_fai_relays(self):
        cfg = config_mod.load(ROOT / "config.toml")
        chans = {o["id"]: (o["board"], o["channel"]) for o in cfg["outputs"] if o["board"] == "fai"}
        self.assertEqual(chans, {"fai.mode": ("fai", 0), "fai.no_trigger": ("fai", 1),
                                 "fai.nc_trigger": ("fai", 2), "fai.latch": ("fai", 3)})
        addrs = [b["address"] for b in cfg["boards"]]
        self.assertEqual(len(addrs), len(set(addrs)), "boards must not share an I2C address")

    def test_duplicate_input_pin_rejected(self):
        raw = {
            "boards": [{"id": "s", "driver": "mcp23017", "address": 0x21}],
            "inputs": [{"id": "a", "board": "s", "pin": 4}, {"id": "b", "board": "s", "pin": 4}],
        }
        with self.assertRaises(config_mod.ConfigError):
            config_mod.normalise(raw)

    def test_duplicate_channel_rejected(self):
        raw = {
            "boards": [{"id": "r", "driver": "xl9535", "address": 0x20}],
            "outputs": [
                {"id": "a", "board": "r", "channel": 1},
                {"id": "b", "board": "r", "channel": 1},
            ],
        }
        with self.assertRaises(config_mod.ConfigError):
            config_mod.normalise(raw)

    def test_new_output_group_is_easy_to_add(self):
        raw = {
            "boards": [{"id": "fai", "driver": "xl9535", "address": 0x22}],
            "outputs": [{"id": "fai.trigger", "name": "Fire", "group": "fai", "board": "fai", "channel": 0}],
        }
        cfg = config_mod.normalise(raw)
        self.assertEqual(cfg["outputs"][0]["mode"], "toggle")


class ControllerTests(unittest.TestCase):
    def setUp(self):
        cfg = config_mod.load(ROOT / "config.toml")
        cfg["hardware"]["mode"] = "mock"
        cfg["timing"]["rex_pulse_ms"] = 80
        cfg["timing"]["expected_unlock_s"] = 0.3
        for o in cfg["outputs"]:
            if o["mode"] == "pulse":
                o["pulse_ms"] = 80
        self.cfg = cfg
        self.ctl = Controller(cfg)
        self.relays = self.ctl.boards["relays"]

    def tearDown(self):
        self.ctl.shutdown()

    def door(self, n):
        return next(d for d in self.ctl.snapshot()["doors"] if d["id"] == n)

    def test_sense_board_disabled_by_default(self):
        self.assertNotIn("sense", self.ctl.boards)
        self.assertEqual(self.door(1)["lock_source"], "expected")

    def test_open_close_door_drives_dpi_relay(self):
        self.ctl.set_output("door2.dpi", True)
        self.assertTrue(self.relays.get(3))
        self.assertTrue(self.door(2)["open"])
        self.assertFalse(self.door(1)["open"])
        self.ctl.set_output("door2.dpi", False)
        self.assertFalse(self.relays.get(3))
        self.assertFalse(self.door(2)["open"])

    def test_dpi_invert(self):
        cfg = config_mod.load(ROOT / "config.toml")
        cfg["hardware"]["mode"] = "mock"
        next(o for o in cfg["outputs"] if o["id"] == "door1.dpi")["invert"] = True
        ctl = Controller(cfg)
        try:
            ctl.set_output("door1.dpi", True)   # door open
            self.assertFalse(ctl.boards["relays"].get(0))  # relay released
            ctl.set_output("door1.dpi", False)
            self.assertTrue(ctl.boards["relays"].get(0))
        finally:
            ctl.shutdown()

    def test_rex_is_momentary_and_shows_expected_unlock(self):
        self.ctl.pulse_output("door4.rex2")
        self.assertTrue(self.relays.get(11))
        d = self.door(4)
        self.assertEqual((d["lock"], d["lock_source"]), ("unlocked", "expected"))
        time.sleep(0.2)
        self.assertFalse(self.relays.get(11), "REX relay should release after pulse")
        time.sleep(0.25)
        self.assertEqual(self.door(4)["lock"], "locked")

    def test_each_door_maps_to_its_own_channels(self):
        for n in range(1, 5):
            base = (n - 1) * 3
            for i, role in enumerate(("dpi", "rex1", "rex2")):
                self.relays.all_off()
                self.ctl.set_output(f"door{n}.{role}", True)
                self.assertEqual(self.relays.relays(), 1 << (base + i), f"door{n}.{role}")
                self.ctl.set_output(f"door{n}.{role}", False)

    def test_aux_toggle(self):
        self.ctl.action("aux.in2", "toggle")
        self.assertTrue(self.relays.get(13))
        self.ctl.action("aux.in2", "toggle")
        self.assertFalse(self.relays.get(13))
        self.ctl.action("aux.in1", "on")
        self.assertTrue(self.relays.get(12))
        with self.assertRaises(PointError):  # AUX OUT is an input now
            self.ctl.action("aux.out1", "on")

    def test_rex_hold_stays_on_until_release(self):
        self.ctl.hold_output("door1.rex1")
        time.sleep(0.2)  # well past the 80 ms pulse
        self.assertTrue(self.relays.get(1), "held REX stays energised")
        self.assertTrue(self.ctl.snapshot()["outputs"]["door1.rex1"]["held"])
        self.assertEqual(self.door(1)["lock"], "unlocked")
        time.sleep(0.25)  # past expected_unlock_s while still held
        self.assertEqual(self.door(1)["lock"], "unlocked", "still unlocked while held")
        self.ctl.release_output("door1.rex1")
        self.assertFalse(self.relays.get(1), "released immediately after a long hold")
        self.assertEqual(self.door(1)["lock"], "unlocked", "expected window restarts on release")
        time.sleep(0.4)
        self.assertEqual(self.door(1)["lock"], "locked")

    def test_rex_quick_tap_gets_minimum_pulse(self):
        self.ctl.hold_output("door2.rex2")
        self.ctl.release_output("door2.rex2")
        self.assertTrue(self.relays.get(5), "tap still gets the minimum pulse")
        time.sleep(0.2)
        self.assertFalse(self.relays.get(5))

    def test_rex_hold_safety_timeout(self):
        self.ctl.timing["rex_max_hold_s"] = 0.15
        self.ctl.hold_output("door3.rex1")
        time.sleep(0.3)
        self.assertFalse(self.relays.get(7), "forgotten hold is released")
        self.assertFalse(self.ctl.snapshot()["outputs"]["door3.rex1"]["held"])
        self.ctl.release_output("door3.rex1")  # late release is harmless
        self.assertFalse(self.relays.get(7))

    def test_rex_latch_stays_until_released(self):
        self.ctl.timing["rex_max_hold_s"] = 0.1  # a hold would be released by now
        self.ctl.action("door4.rex1", "latch")
        out = self.ctl.snapshot()["outputs"]["door4.rex1"]
        self.assertEqual((out["latched"], out["held"]), (True, False))
        time.sleep(0.3)
        self.assertTrue(self.relays.get(10), "latch ignores the hold time limit")
        self.assertEqual(self.door(4)["lock"], "unlocked", "unlocked while latched")
        self.ctl.action("door4.rex1", "release")
        self.assertFalse(self.relays.get(10), "second click releases")
        self.assertFalse(self.ctl.snapshot()["outputs"]["door4.rex1"]["latched"])

    def test_rex_latch_cleared_by_all_off_and_optional_limit(self):
        self.ctl.action("door1.rex2", "latch")
        self.ctl.all_off()
        self.assertFalse(self.relays.get(2))
        self.assertFalse(self.ctl.snapshot()["outputs"]["door1.rex2"]["latched"])
        self.ctl.timing["rex_max_latch_s"] = 0.15
        self.ctl.action("door1.rex2", "latch")
        time.sleep(0.3)
        self.assertFalse(self.relays.get(2), "rex_max_latch_s > 0 releases a latch")
        self.assertFalse(self.ctl.snapshot()["outputs"]["door1.rex2"]["latched"])

    def test_aux_out_read_from_sense_board(self):
        cfg = config_mod.load(ROOT / "config.toml")
        cfg["hardware"]["mode"] = "mock"
        for b in cfg["boards"]:
            b["enabled"] = True
        ctl = Controller(cfg)
        try:
            ctl._read_inputs()
            self.assertFalse(ctl.snapshot()["inputs"]["aux.out1"]["active"])
            ctl.boards["sense"].set_pin(6, 0)  # AC43 AUX OUT 1 relay closes NO->GND
            ctl._read_inputs()
            self.assertTrue(ctl.snapshot()["inputs"]["aux.out1"]["active"])
            self.assertFalse(ctl.snapshot()["inputs"]["aux.out2"]["active"])
        finally:
            ctl.shutdown()

    def test_unknown_point(self):
        with self.assertRaises(PointError):
            self.ctl.action("door9.dpi", "on")
        with self.assertRaises(PointError):
            self.ctl.action("aux.in1", "explode")

    def test_shutdown_releases_all_relays(self):
        self.ctl.set_output("door1.dpi", True)
        self.ctl.set_output("aux.in2", True)
        self.ctl.shutdown()
        self.assertEqual(self.relays.relays(), 0)

    def test_subscribers_get_updates(self):
        self.ctl.start()
        q = self.ctl.subscribe()
        first = q.get(timeout=1)  # initial snapshot
        time.sleep(0.3)
        idle_seq = self.ctl.snapshot()["seq"]
        time.sleep(0.3)
        self.assertEqual(self.ctl.snapshot()["seq"], idle_seq, "seq doesn't move when nothing changes")
        self.ctl.set_output("door3.dpi", True)
        deadline = time.time() + 2
        while time.time() < deadline:
            snap = q.get(timeout=2)
            if next(d for d in snap["doors"] if d["id"] == 3)["open"]:
                self.assertGreater(snap["seq"], first["seq"], "each change gets a newer seq")
                return
        self.fail("no update broadcast for door3 open")


class SensorControllerTests(unittest.TestCase):
    """With the MCP23017 enabled (mock), lock state comes from the sensor."""

    def setUp(self):
        cfg = config_mod.load(ROOT / "config.toml")
        cfg["hardware"]["mode"] = "mock"
        cfg["timing"]["expected_unlock_s"] = 0.3
        for b in cfg["boards"]:
            b["enabled"] = True
        self.ctl = Controller(cfg)
        self.sense = self.ctl.boards["sense"]

    def tearDown(self):
        self.ctl.shutdown()

    def door(self, n):
        self.ctl._read_inputs()
        return next(d for d in self.ctl.snapshot()["doors"] if d["id"] == n)

    def test_pin_low_means_unlocked_confirmed(self):
        self.assertEqual((self.door(2)["lock"], self.door(2)["lock_source"]), ("locked", "sensor"))
        self.sense.set_pin(1, 0)  # pulled low by AC43 lock relay
        d = self.door(2)
        self.assertEqual((d["lock"], d["lock_source"]), ("unlocked", "sensor"))
        self.assertEqual(self.door(1)["lock"], "locked")

    def test_sensor_overrides_rex_expectation(self):
        # Real (non-simulated) board would not follow REX; emulate by swapping in a plain object
        class Stuck(drivers.MockInput):
            pass
        self.ctl.boards["sense"] = Stuck()
        self.ctl._simulate_lock = lambda door: None
        self.ctl.pulse_output("door1.rex1")
        d = self.door(1)
        self.assertEqual((d["lock"], d["lock_source"]), ("locked", "sensor"),
                         "a REX that the AC43 ignored must NOT show as unlocked")

    def test_mock_sensor_follows_rex(self):
        self.ctl.pulse_output("door3.rex1")
        self.assertEqual(self.door(3)["lock"], "unlocked")
        time.sleep(0.45)
        self.assertEqual(self.door(3)["lock"], "locked")

    def test_read_fault_reported(self):
        class Broken(drivers.MockInput):
            def read_all(self):
                raise OSError(121, "Remote I/O error")
        self.ctl.boards["sense"] = Broken()
        d = self.door(1)
        self.assertEqual(d["lock_source"], "expected")
        info = next(b for b in self.ctl.snapshot()["boards"] if b["id"] == "sense")
        self.assertIn("Remote I/O", info["fault"])


class FaiTests(unittest.TestCase):
    MODE, NO, NC, LATCH = 0, 1, 2, 3

    def make(self, sense=False, **fai_overrides):
        cfg = config_mod.load(ROOT / "config.toml")
        cfg["hardware"]["mode"] = "mock"
        for b in cfg["boards"]:
            if b["id"] == "sense":
                b["enabled"] = sense
        cfg["fai"].update(fai_overrides)
        if "mode_invert" in fai_overrides:
            next(o for o in cfg["outputs"] if o["id"] == "fai.mode")["invert"] = fai_overrides["mode_invert"]
        ctl = Controller(cfg)
        self.addCleanup(ctl.shutdown)
        board = ctl.boards["fai"]
        writes = []
        orig = board.set
        board.set = lambda ch, v: (writes.append((ch, v)), orig(ch, v))[1]
        return ctl, board, writes

    def phys(self, board):
        return {n: board.get(ch) for n, ch in
                (("mode", self.MODE), ("no", self.NO), ("nc", self.NC), ("latch", self.LATCH))}

    def test_startup_no_mode_idle(self):
        ctl, board, _ = self.make()
        self.assertEqual(self.phys(board), {"mode": False, "no": False, "nc": True, "latch": False})
        self.assertEqual(ctl.fai_state()["mode"], "no")

    def test_trigger_and_clear_in_no_mode(self):
        ctl, board, _ = self.make()
        ctl.fai_set_triggered(True)
        self.assertTrue(board.get(self.NO), "NO contact must close FAI+/FAI-")
        self.assertFalse(board.get(self.MODE))
        ctl.fai_set_triggered(False)
        self.assertFalse(board.get(self.NO))

    def test_trigger_and_clear_in_nc_mode(self):
        ctl, board, _ = self.make()
        ctl.fai_set_mode("nc")
        self.assertEqual(self.phys(board), {"mode": True, "no": False, "nc": False, "latch": False})
        ctl.fai_set_triggered(True)
        self.assertTrue(board.get(self.NC), "NC relay energised = loop broken")
        self.assertFalse(board.get(self.NO), "NO trigger must never fire in NC mode")
        ctl.fai_set_triggered(False)
        self.assertFalse(board.get(self.NC))

    def test_mode_switch_order_never_false_alarms(self):
        ctl, board, writes = self.make()
        writes.clear()
        ctl.fai_set_mode("nc")
        # close the NC loop before fitting the jumper
        self.assertEqual(writes, [(self.NC, False), (self.MODE, True)])
        writes.clear()
        ctl.fai_set_mode("no")
        # remove the jumper before opening the NC loop
        self.assertEqual(writes, [(self.MODE, False), (self.NC, True)])

    def test_cannot_change_mode_while_triggered(self):
        ctl, board, _ = self.make()
        ctl.fai_set_triggered(True)
        with self.assertRaises(PointError):
            ctl.fai_set_mode("nc")
        ctl.fai_set_mode("no")  # same mode is fine

    def test_latch_is_independent(self):
        ctl, board, _ = self.make()
        ctl.fai_set_latch(True)
        self.assertTrue(board.get(self.LATCH))
        ctl.fai_set_triggered(True)
        ctl.fai_set_triggered(False)
        self.assertTrue(board.get(self.LATCH))
        ctl.fai_set_latch(False)
        self.assertFalse(board.get(self.LATCH))

    def test_mode_invert(self):
        ctl, board, _ = self.make(mode_invert=True, default_mode="nc")
        self.assertFalse(board.get(self.MODE), "COM/NC wiring: relay off = jumper fitted")
        ctl.fai_set_mode("no")
        self.assertTrue(board.get(self.MODE))

    def test_fai_relays_not_directly_writable(self):
        ctl, _, _ = self.make()
        with self.assertRaises(PointError):
            ctl.action("fai.no_trigger", "on")
        self.assertNotIn("fai.no_trigger", [o["id"] for o in ctl.layout()["outputs"]])

    def test_all_off_clears_alarm_keeps_mode(self):
        ctl, board, _ = self.make()
        ctl.fai_set_mode("nc")
        ctl.fai_set_triggered(True)
        ctl.fai_set_latch(True)
        ctl.all_off()
        self.assertFalse(ctl.fai_state()["triggered"])
        self.assertEqual(ctl.fai_state()["mode"], "nc")
        self.assertEqual(self.phys(board), {"mode": True, "no": False, "nc": False, "latch": False})

    def test_doors_expected_unlocked_during_alarm_without_sensor(self):
        ctl, _, _ = self.make()
        ctl.fai_set_triggered(True)
        for d in ctl.snapshot()["doors"]:
            self.assertEqual((d["lock"], d["lock_source"]), ("unlocked", "expected"))

    def test_confirmation_logic(self):
        ctl, _, _ = self.make(sense=True)
        sense = ctl.boards["sense"]
        cases = [  # (NO pin level, NC pin level) -> confirm ; low = active
            ((1, 0), "normal"),
            ((0, 1), "triggered"),
            ((1, 1), "fault"),   # wire off
            ((0, 0), "fault"),
        ]
        ctl._timers.pop("fai#sim").cancel()
        for (no, nc), want in cases:
            sense.set_pin(4, no)
            sense.set_pin(5, nc)
            ctl._read_inputs()
            self.assertEqual(ctl.fai_confirm(), want, (no, nc))

    def test_confirmation_unknown_without_sensor(self):
        ctl, _, _ = self.make()
        ctl._read_inputs()
        self.assertEqual(ctl.fai_state()["confirm"], "unknown")

    def test_mock_ac43_confirms_and_unlocks_doors(self):
        ctl, _, _ = self.make(sense=True)
        time.sleep(0.4)
        ctl._read_inputs()
        self.assertEqual(ctl.fai_confirm(), "normal")
        ctl.fai_set_triggered(True)
        time.sleep(0.4)
        ctl._read_inputs()
        self.assertEqual(ctl.fai_confirm(), "triggered")
        for d in ctl.snapshot()["doors"]:
            self.assertEqual((d["lock"], d["lock_source"]), ("unlocked", "sensor"))
        ctl.fai_set_triggered(False)
        time.sleep(0.4)
        ctl._read_inputs()
        self.assertEqual(ctl.fai_confirm(), "normal")
        self.assertEqual(ctl.snapshot()["doors"][0]["lock"], "locked")

    def test_config_rejects_bad_fai(self):
        base = {"boards": [{"id": "f", "driver": "xl9535", "address": 0x22}]}
        with self.assertRaises(config_mod.ConfigError):  # both triggers, no mode relay
            config_mod.normalise({**base, "fai": {"no_trigger": {"board": "f", "channel": 0},
                                                  "nc_trigger": {"board": "f", "channel": 1}}})
        with self.assertRaises(config_mod.ConfigError):  # default mode not wired
            config_mod.normalise({**base, "fai": {"no_trigger": {"board": "f", "channel": 0},
                                                  "default_mode": "nc"}})
        cfg = config_mod.normalise({**base, "fai": {"no_trigger": {"board": "f", "channel": 0}}})
        self.assertEqual(cfg["fai"]["modes"], ["no"])


if __name__ == "__main__":
    unittest.main()
