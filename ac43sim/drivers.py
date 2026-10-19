"""I2C board drivers.

CPython ports of the MicroPython XL9535 KxV5 relay driver (Mike Causer, MIT)
and a minimal MCP23017 input driver, using smbus2 on a Raspberry Pi.

Every driver exposes the same tiny interface so the controller doesn't care
what's on the bus:

    output boards:  set(channel, bool), get(channel) -> bool, all_off()
    input boards:   read_all() -> int (16-bit, bit N = raw pin level)
"""

import logging
import os
import threading

log = logging.getLogger(__name__)

# One SMBus handle + lock per bus number, shared by every board on that bus.
_buses = {}
_buses_lock = threading.Lock()


class I2CBus:
    def __init__(self, bus_num):
        from smbus2 import SMBus  # imported lazily so mock mode has no deps

        self.num = bus_num
        self.lock = threading.Lock()
        self._bus = SMBus(bus_num)

    def write_block(self, addr, reg, data):
        with self.lock:
            self._bus.write_i2c_block_data(addr, reg, list(data))

    def read_block(self, addr, reg, length):
        with self.lock:
            return self._bus.read_i2c_block_data(addr, reg, length)

    def probe(self, addr):
        with self.lock:
            try:
                self._bus.read_byte(addr)
                return True
            except OSError:
                return False


def get_bus(bus_num):
    with _buses_lock:
        if bus_num not in _buses:
            _buses[bus_num] = I2CBus(bus_num)
        return _buses[bus_num]


def i2c_available(bus_num):
    try:
        import smbus2  # noqa: F401
    except ImportError:
        return False
    return os.path.exists(f"/dev/i2c-{bus_num}")


# --- XL9535 KxV5 relay board ------------------------------------------------

XL9535_OUTPUT_PORT0 = 0x02
XL9535_INVERSION_PORT0 = 0x04
XL9535_CONFIG_PORT0 = 0x06


class XL9535Relay:
    """16-channel relay board. High bit = relay energised (NC-x-COM<->NO)."""

    kind = "output"

    def __init__(self, bus, address=0x20):
        self._bus = bus
        self._address = address
        self._lock = threading.Lock()
        self._state = 0  # shadow of OUTPUT_PORT0/1
        self.init()

    def check(self):
        if not self._bus.probe(self._address):
            raise OSError(f"XL9535 not found at I2C address {self._address:#x}")
        return True

    def init(self):
        with self._lock:
            # inversion off, all relays off, all circuits (config bits) enabled
            self._bus.write_block(self._address, XL9535_INVERSION_PORT0, [0x00, 0x00])
            self._bus.write_block(self._address, XL9535_OUTPUT_PORT0, [0x00, 0x00])
            self._bus.write_block(self._address, XL9535_CONFIG_PORT0, [0x00, 0x00])
            self._state = 0

    def _write(self):
        self._bus.write_block(
            self._address, XL9535_OUTPUT_PORT0, [self._state & 0xFF, (self._state >> 8) & 0xFF]
        )

    def set(self, channel, value):
        assert 0 <= channel <= 15, "channel should be in range 0-15"
        with self._lock:
            # read-modify-write against the chip so we never clobber other channels
            lo, hi = self._bus.read_block(self._address, XL9535_OUTPUT_PORT0, 2)
            self._state = (hi << 8) | lo
            if value:
                self._state |= 1 << channel
            else:
                self._state &= ~(1 << channel)
            self._write()

    def get(self, channel):
        assert 0 <= channel <= 15, "channel should be in range 0-15"
        with self._lock:
            lo, hi = self._bus.read_block(self._address, XL9535_OUTPUT_PORT0, 2)
            self._state = (hi << 8) | lo
            return bool(self._state & (1 << channel))

    def relays(self):
        with self._lock:
            lo, hi = self._bus.read_block(self._address, XL9535_OUTPUT_PORT0, 2)
            self._state = (hi << 8) | lo
            return self._state

    def all_off(self):
        with self._lock:
            self._state = 0
            self._write()


# --- MCP23017 16-bit GPIO expander (inputs) ---------------------------------

MCP_IODIRA = 0x00
MCP_GPPUA = 0x0C
MCP_GPIOA = 0x12


class MCP23017Input:
    """All 16 pins as inputs. read_all() returns raw levels, bit N = pin N."""

    kind = "input"

    def __init__(self, bus, address=0x20, pullups=True):
        self._bus = bus
        self._address = address
        self._pullups = pullups
        self.init()

    def check(self):
        if not self._bus.probe(self._address):
            raise OSError(f"MCP23017 not found at I2C address {self._address:#x}")
        return True

    def init(self):
        # IOCON defaults to BANK=0, SEQOP=0 so A/B registers are adjacent and
        # a 2-byte block read/write covers both ports.
        self._bus.write_block(self._address, MCP_IODIRA, [0xFF, 0xFF])
        pu = 0xFF if self._pullups else 0x00
        self._bus.write_block(self._address, MCP_GPPUA, [pu, pu])

    def read_all(self):
        lo, hi = self._bus.read_block(self._address, MCP_GPIOA, 2)
        return (hi << 8) | lo


# --- Mocks (no hardware) -----------------------------------------------------


class MockRelay:
    kind = "output"

    def __init__(self, *_, **__):
        self._state = 0

    def check(self):
        return True

    def set(self, channel, value):
        assert 0 <= channel <= 15
        if value:
            self._state |= 1 << channel
        else:
            self._state &= ~(1 << channel)

    def get(self, channel):
        return bool(self._state & (1 << channel))

    def relays(self):
        return self._state

    def all_off(self):
        self._state = 0


class MockInput:
    """Pins idle HIGH (as with pull-ups). Tests / the sim API can set levels."""

    kind = "input"

    def __init__(self, *_, **__):
        self.levels = 0xFFFF

    def check(self):
        return True

    def set_pin(self, pin, level):
        if level:
            self.levels |= 1 << pin
        else:
            self.levels &= ~(1 << pin)

    def read_all(self):
        return self.levels


DRIVERS = {
    "xl9535": (XL9535Relay, MockRelay),
    "mcp23017": (MCP23017Input, MockInput),
}


def build_board(spec, mode):
    """Create a board from its config entry. Returns (driver, is_mock)."""
    real_cls, mock_cls = DRIVERS[spec["driver"]]
    bus_num = spec.get("bus", 1)
    kwargs = {}
    if spec["driver"] == "mcp23017":
        kwargs["pullups"] = spec.get("pullups", True)

    if mode == "mock":
        return mock_cls(), True

    if not i2c_available(bus_num):
        if mode == "real":
            raise RuntimeError(f"I2C bus {bus_num} unavailable (is smbus2 installed and I2C enabled?)")
        log.warning("I2C bus %s unavailable - board %r running in MOCK mode", bus_num, spec["id"])
        return mock_cls(), True

    try:
        board = real_cls(get_bus(bus_num), spec["address"], **kwargs)
        board.check()
        return board, False
    except OSError as e:
        if mode == "real":
            raise
        log.warning("Board %r: %s - running in MOCK mode", spec["id"], e)
        return mock_cls(), True
