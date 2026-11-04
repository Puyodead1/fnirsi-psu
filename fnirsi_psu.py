"""
fnirsi_psu - Python driver for FNIRSI USB-controlled bench power supplies.

Re-implementation of the protocol used by the official "FNIRSI Power supply"
Windows app (V1.0.0.0). See PROTOCOL.md for the full write-up.

The device is a USB CDC virtual COM port; frames are:
    F1 CMD REG LEN DATA[LEN] SUM      SUM = (REG + LEN + sum(DATA)) & 0xFF

Requires: pyserial  (pip install pyserial)

Example:
    from fnirsi_psu import FnirsiPSU
    with FnirsiPSU("COM5") as psu:
        print(psu.model, psu.firmware)
        psu.set_voltage(5.0)
        psu.set_current(0.5)
        psu.output(True)
        print(psu.state.vout, psu.state.iout)
"""
from __future__ import annotations

import logging
import struct
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Iterable, Optional

log = logging.getLogger("fnirsi_psu")

HOST_HEADER = 0xF1      # PC -> device
DEVICE_HEADER = 0xF0    # device -> PC (confirmed on a DPS-150; the app never checks it)
HEADER = HOST_HEADER    # backwards-compatible alias
HEADERS = (HOST_HEADER, DEVICE_HEADER)
BAUD_RATES = (9600, 19200, 38400, 57600, 115200)


class Cmd(IntEnum):
    READ = 0xA1
    WRITE = 0xB1
    SET_BAUD = 0xB0
    BOOT = 0xC0
    LINK = 0xC1


class Reg(IntEnum):
    VIN = 0xC0
    VSET = 0xC1
    ISET = 0xC2
    OUTPUT_VIP = 0xC3        # 3x f32: V, I, P
    TEMP = 0xC4
    # presets M1..M6: V = 0xC3 + 2n, I = 0xC4 + 2n (n=1..6) -> C5/C6 .. CF/D0
    OVP = 0xD1
    OCP = 0xD2
    OPP = 0xD3
    OTP = 0xD4
    LVP = 0xD5
    BRIGHTNESS = 0xD6
    UNKNOWN_D7 = 0xD7
    METERING = 0xD8
    CAPACITY_AH = 0xD9
    ENERGY_WH = 0xDA
    OUTPUT_ON = 0xDB
    PROTECTION = 0xDC
    CV_CC = 0xDD
    MODEL = 0xDE
    STRING_DF = 0xDF
    FIRMWARE = 0xE0
    ADDRESS = 0xE1
    VSET_MAX = 0xE2
    ISET_MAX = 0xE3
    ALL = 0xFF


class Protection(IntEnum):
    NORMAL = 0
    OVP = 1
    OCP = 2
    OPP = 3
    OTP = 4
    LVP = 5


def preset_regs(n: int) -> tuple[int, int]:
    """(voltage_reg, current_reg) for preset group n (1..6)."""
    if not 1 <= n <= 6:
        raise ValueError("preset must be 1..6")
    return 0xC3 + 2 * n, 0xC4 + 2 * n


# --------------------------------------------------------------------------- #
# Framing
# --------------------------------------------------------------------------- #

def checksum(reg: int, data: bytes) -> int:
    return (reg + len(data) + sum(data)) & 0xFF


def build_frame(cmd: int, reg: int, data: bytes | int = b"\x00",
                header: int = HOST_HEADER) -> bytes:
    if isinstance(data, int):
        data = bytes([data])
    if not 1 <= len(data) <= 255:
        raise ValueError("data length must be 1..255")
    return bytes([header, cmd, reg, len(data)]) + data + bytes([checksum(reg, data)])


def f32(value: float) -> bytes:
    return struct.pack("<f", value)


@dataclass
class Frame:
    cmd: int
    reg: int
    data: bytes
    checksum_ok: bool = True

    def __repr__(self) -> str:
        try:
            name = Reg(self.reg).name
        except ValueError:
            name = f"0x{self.reg:02X}"
        return (f"Frame(cmd=0x{self.cmd:02X}, reg={name}, data={self.data.hex(' ')}"
                f"{'' if self.checksum_ok else ', BAD SUM'})")


class FrameParser:
    """Streaming parser: feed() arbitrary chunks, get complete frames back.

    Accepts both headers (0xF0 device->PC, 0xF1 PC->device), resynchronises on
    them and checks the checksum. On a bad checksum the candidate's length byte
    can't be trusted, so only the header byte is dropped and parsing resumes at
    the next header; nothing is returned for it.
    """

    def __init__(self, strict: bool = True):
        self.buf = bytearray()
        self.strict = strict          # kept for API compatibility
        self.bad_frames = 0

    def _find_header(self) -> int:
        for i, b in enumerate(self.buf):
            if b in HEADERS:
                return i
        return -1

    def feed(self, chunk: bytes) -> list[Frame]:
        self.buf += chunk
        out: list[Frame] = []
        while True:
            start = self._find_header()
            if start < 0:
                self.buf.clear()
                break
            if start:
                log.debug("discarding %d junk bytes", start)
                del self.buf[:start]
            if len(self.buf) < 4:
                break
            total = 5 + self.buf[3]
            if len(self.buf) < total:
                break
            raw = bytes(self.buf[:total])
            reg, data = raw[2], raw[4:-1]
            if checksum(reg, data) != raw[-1]:
                self.bad_frames += 1
                log.debug("bad checksum, resyncing: %s", raw[:8].hex(" "))
                del self.buf[:1]
                continue
            del self.buf[:total]
            out.append(Frame(raw[1], reg, data, True))
        return out


# --------------------------------------------------------------------------- #
# Device state / decoding
# --------------------------------------------------------------------------- #

@dataclass
class State:
    vin: Optional[float] = None
    vset: Optional[float] = None
    iset: Optional[float] = None
    vout: Optional[float] = None
    iout: Optional[float] = None
    pout: Optional[float] = None
    temp_c: Optional[float] = None
    presets: dict[int, list[Optional[float]]] = field(
        default_factory=lambda: {n: [None, None] for n in range(1, 7)})
    ovp: Optional[float] = None
    ocp: Optional[float] = None
    opp: Optional[float] = None
    otp: Optional[float] = None
    lvp: Optional[float] = None
    brightness: Optional[int] = None
    unknown_d7: Optional[int] = None
    metering: Optional[bool] = None
    capacity_ah: Optional[float] = None
    energy_wh: Optional[float] = None
    output_on: Optional[bool] = None
    protection: Optional[Protection] = None
    cv_mode: Optional[bool] = None        # True = CV, False = CC
    model: Optional[str] = None
    string_df: Optional[str] = None
    firmware: Optional[str] = None
    address: Optional[int] = None
    vset_max: Optional[float] = None
    iset_max: Optional[float] = None
    ovp_max: Optional[float] = None
    ocp_max: Optional[float] = None
    opp_max: Optional[float] = None
    otp_max: Optional[float] = None
    lvp_max: Optional[float] = None


def _uf(b: bytes, off: int = 0) -> float:
    return struct.unpack_from("<f", b, off)[0]


def _str(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("ascii", "replace").strip()


def apply_frame(st: State, fr: Frame) -> None:
    """Update `st` from a device frame (mirrors SerialData.setData)."""
    r, d = fr.reg, fr.data
    try:
        if r == Reg.VIN:
            st.vin = _uf(d)
        elif r == Reg.VSET:
            st.vset = _uf(d)
        elif r == Reg.ISET:
            st.iset = _uf(d)
        elif r == Reg.OUTPUT_VIP:
            st.vout, st.iout, st.pout = struct.unpack_from("<3f", d)
        elif r == Reg.TEMP:
            st.temp_c = _uf(d)
        elif r == Reg.CAPACITY_AH:
            st.capacity_ah = _uf(d)
        elif r == Reg.ENERGY_WH:
            st.energy_wh = _uf(d)
        elif r == Reg.OUTPUT_ON:
            st.output_on = bool(d[0])
        elif r == Reg.PROTECTION:
            st.protection = _prot(d[0])
        elif r == Reg.CV_CC:
            st.cv_mode = d[0] == 1
        elif r == Reg.MODEL:
            st.model = _str(d)
        elif r == Reg.STRING_DF:
            st.string_df = _str(d)
        elif r == Reg.FIRMWARE:
            st.firmware = _str(d)
        elif r == Reg.ADDRESS:
            st.address = d[0]
        elif r == Reg.VSET_MAX:
            st.vset_max = _uf(d)
        elif r == Reg.ISET_MAX:
            st.iset_max = _uf(d)
        elif r == Reg.BRIGHTNESS:
            st.brightness = d[0]
        elif r == Reg.METERING:
            st.metering = bool(d[0])
        elif r in (Reg.OVP, Reg.OCP, Reg.OPP, Reg.OTP, Reg.LVP):
            setattr(st, Reg(r).name.lower(), _uf(d))
        elif 0xC5 <= r <= 0xD0:
            n, is_i = divmod(r - 0xC5, 2)
            st.presets[n + 1][is_i] = _uf(d)
        elif r == Reg.ALL:
            apply_bulk(st, d)
        else:
            log.debug("unhandled register %r", fr)
    except (struct.error, IndexError):
        log.warning("short payload for %r", fr)


def _prot(v: int):
    try:
        return Protection(v)
    except ValueError:
        return v


def apply_bulk(st: State, d: bytes) -> None:
    """Decode the 0xFF 'read all' block (mirrors SerialData.setAllData)."""
    st.vin, st.vset, st.iset, st.vout, st.iout, st.pout, st.temp_c = \
        struct.unpack_from("<7f", d, 0)
    pres = struct.unpack_from("<12f", d, 28)
    for n in range(6):
        st.presets[n + 1] = [pres[2 * n], pres[2 * n + 1]]
    st.ovp, st.ocp, st.opp, st.otp, st.lvp = struct.unpack_from("<5f", d, 76)
    st.brightness, st.unknown_d7, met = d[96], d[97], d[98]
    st.metering = bool(met)
    st.capacity_ah, st.energy_wh = struct.unpack_from("<2f", d, 99)
    st.output_on = bool(d[107])
    st.protection = _prot(d[108])
    st.cv_mode = d[109] == 1
    st.address = d[110]                     # inferred (app doesn't read it)
    st.vset_max, st.iset_max = struct.unpack_from("<2f", d, 111)
    if len(d) >= 139:
        (st.ovp_max, st.ocp_max, st.opp_max,
         st.otp_max, st.lvp_max) = struct.unpack_from("<5f", d, 119)


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

class FnirsiPSU:
    """High-level driver. Opens the port, starts a reader thread, handshakes."""

    def __init__(self, port: str, baudrate: int = 9600, address: int = 1,
                 connect: bool = True, legacy_baud_checksum: bool = False,
                 strict_checksum: bool = False, timeout: float = 2.0,
                 serial_factory: Optional[Callable] = None):
        if baudrate not in BAUD_RATES:
            raise ValueError(f"baudrate must be one of {BAUD_RATES}")
        self.baudrate = baudrate
        self.address = address
        self.timeout = timeout
        self.legacy_baud_checksum = legacy_baud_checksum
        self.state = State()
        self.parser = FrameParser(strict=strict_checksum)
        self.listeners: list[Callable[[Frame], None]] = []
        self._cv = threading.Condition()
        # per-register sequence number of the last frame received. A counter,
        # not a timestamp: time.monotonic() ticks only every ~15.6 ms on Windows
        # before Python 3.13, so an old reply could look as new as the request.
        self._seq = 0
        self._seen: dict[int, int] = {}
        self._stop = threading.Event()

        if serial_factory is None:
            import serial  # pyserial
            serial_factory = lambda: serial.Serial(
                port, baudrate, bytesize=8, parity="N", stopbits=1, timeout=0.1)
        self.ser = serial_factory()
        try:
            self.ser.rts = True
        except Exception:
            pass
        self._rx = threading.Thread(target=self._reader, daemon=True)
        self._rx.start()
        if connect:
            self.connect()

    # -- context manager -------------------------------------------------- #
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- low level -------------------------------------------------------- #
    def send(self, cmd: int, reg: int, data: bytes | int = b"\x00") -> bytes:
        frame = build_frame(cmd, reg, data)
        log.debug("TX %s", frame.hex(" "))
        self.ser.write(frame)
        return frame

    def send_raw(self, frame: bytes) -> None:
        log.debug("TX %s", frame.hex(" "))
        self.ser.write(frame)

    def _reader(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self.ser.read(256)
            except Exception as e:  # port gone
                log.error("serial read failed: %s", e)
                break
            if not chunk:
                continue
            log.debug("RX %s", chunk.hex(" "))
            self._handle(chunk)

    def _handle(self, chunk: bytes) -> None:
        for fr in self.parser.feed(chunk):
            with self._cv:
                apply_frame(self.state, fr)
                self._seq += 1
                self._seen[fr.reg] = self._seq
                self._cv.notify_all()
            for cb in self.listeners:
                try:
                    cb(fr)
                except Exception:
                    log.exception("listener failed")

    def mark(self) -> int:
        """Sequence position to pass to wait_for(); take it BEFORE sending."""
        with self._cv:
            return self._seq

    def wait_for(self, reg: int, since: int, timeout: Optional[float] = None) -> bool:
        """Wait until a frame for `reg` arrives after sequence mark `since`."""
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        with self._cv:
            while self._seen.get(reg, 0) <= since:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(left)
        return True

    def read(self, reg: int, timeout: Optional[float] = None) -> bool:
        """Send a read request and wait for the reply. Returns True if it arrived."""
        m = self.mark()
        self.send(Cmd.READ, reg)
        return self.wait_for(reg, m, timeout)

    def write_float(self, reg: int, value: float) -> None:
        self.send(Cmd.WRITE, reg, f32(value))

    def write_byte(self, reg: int, value: int) -> None:
        self.send(Cmd.WRITE, reg, int(value) & 0xFF)

    # -- session ---------------------------------------------------------- #
    def connect(self, retries: int = 2) -> None:
        """Replicates the app's handshake. Raises ConnectionError on failure."""
        self.send(Cmd.LINK, 0x00, 1)
        for _ in range(retries):
            if self.read(Reg.ADDRESS, timeout=1.0) and self.state.address == self.address:
                break
        else:
            raise ConnectionError(
                f"no matching address reply (got {self.state.address!r}, "
                f"want {self.address})")
        self.set_baud_index(BAUD_RATES.index(self.baudrate) + 1)
        self.read(Reg.MODEL)
        self.read(Reg.FIRMWARE)
        self.read(Reg.STRING_DF)
        self.read(Reg.ALL)

    def disconnect(self) -> None:
        self.send(Cmd.LINK, 0x00, 0)

    def close(self) -> None:
        try:
            self.disconnect()
            time.sleep(0.05)
        except Exception:
            pass
        self._stop.set()
        self._rx.join(timeout=1.0)
        self.ser.close()

    def set_baud_index(self, idx: int) -> None:
        if self.legacy_baud_checksum:   # reproduce the app's stale-checksum bug
            self.send_raw(bytes([HEADER, Cmd.SET_BAUD, 0x00, 0x01, idx & 0xFF, 0x01]))
        else:
            self.send(Cmd.SET_BAUD, 0x00, idx)

    def enter_bootloader(self) -> None:
        """Switch device into firmware-upgrade (USB drive) mode. Port will drop."""
        self.send(Cmd.BOOT, 0x00, 1)

    # -- convenience ------------------------------------------------------ #
    @property
    def model(self) -> Optional[str]:
        return self.state.model

    @property
    def firmware(self) -> Optional[str]:
        return self.state.firmware

    def refresh(self) -> State:
        self.read(Reg.ALL)
        return self.state

    def measure(self) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """Request a fresh V/I/P reading (app relies on pushes; this polls)."""
        self.read(Reg.OUTPUT_VIP)
        return self.state.vout, self.state.iout, self.state.pout

    def set_voltage(self, volts: float) -> None:
        if self.state.vset_max is not None and volts > self.state.vset_max:
            raise ValueError(f"{volts} V exceeds device max {self.state.vset_max} V")
        self.write_float(Reg.VSET, volts)

    def set_current(self, amps: float) -> None:
        if self.state.iset_max is not None and amps > self.state.iset_max:
            raise ValueError(f"{amps} A exceeds device max {self.state.iset_max} A")
        self.write_float(Reg.ISET, amps)

    def output(self, on: bool) -> None:
        self.write_byte(Reg.OUTPUT_ON, 1 if on else 0)

    def set_metering(self, on: bool) -> None:
        self.write_byte(Reg.METERING, 1 if on else 0)

    def set_brightness(self, level: int) -> None:
        if not 1 <= level <= 14:
            raise ValueError("brightness must be 1..14")
        self.write_byte(Reg.BRIGHTNESS, level)

    def set_protection(self, ovp: float = None, ocp: float = None, opp: float = None,
                       otp: float = None, lvp: float = None) -> None:
        for reg, val in ((Reg.OVP, ovp), (Reg.OCP, ocp), (Reg.OPP, opp),
                         (Reg.OTP, otp), (Reg.LVP, lvp)):
            if val is not None:
                self.write_float(reg, val)

    def set_preset(self, n: int, volts: float, amps: float) -> None:
        """Store preset group n (1..6). App follows with a read-all to resync."""
        vr, ir = preset_regs(n)
        self.write_float(vr, volts)
        self.write_float(ir, amps)
        self.read(Reg.ALL)

    def apply_preset(self, n: int) -> None:
        """Like the app's 'Start': load preset n into the live set-points."""
        v, a = self.state.presets[n]
        if v is None or a is None:
            self.refresh()
            v, a = self.state.presets[n]
        self.set_voltage(v)
        self.set_current(a)

    # -- host-side sequencing (ports of the app's scan / list features) ---- #
    def voltage_sweep(self, start: float, stop: float, step: float, amps: float,
                      dwell: float = 1.0) -> None:
        self.output(True)
        self.set_current(amps)
        for v in _frange(start, stop, step):
            self.set_voltage(v)
            time.sleep(dwell)

    def current_sweep(self, start: float, stop: float, step: float, volts: float,
                      dwell: float = 1.0) -> None:
        self.output(True)
        self.set_voltage(volts)
        for a in _frange(start, stop, step):
            self.set_current(a)
            time.sleep(dwell)

    def run_list(self, steps: Iterable[tuple[float, float, float]], cycles: int = 1) -> None:
        """steps = [(volts, amps, delay_s), ...] like the app's auto-mode table."""
        steps = list(steps)
        for _ in range(cycles):
            for v, a, dt in steps:
                self.set_voltage(v)
                self.set_current(a)
                time.sleep(dt)


def _frange(start: float, stop: float, step: float):
    if step <= 0:
        raise ValueError("step must be > 0")
    v = start
    while v < stop - 1e-9:
        yield round(v, 4)
        v += step
    yield round(stop, 4)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _fmt(x, nd) -> str:
    return "-" if x is None else f"{x:.{nd}f}"


def _prot_name(p) -> str:
    return getattr(p, "name", str(p))


def _main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="FNIRSI power supply control")
    p.add_argument("port", help="COM port, e.g. COM5 or /dev/ttyACM0")
    p.add_argument("-b", "--baud", type=int, default=9600)
    p.add_argument("-a", "--address", type=int, default=1)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--set-v", type=float)
    p.add_argument("--set-i", type=float)
    p.add_argument("--on", action="store_true")
    p.add_argument("--off", action="store_true")
    p.add_argument("--monitor", type=float, metavar="SECONDS",
                   help="print readings for N seconds")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO)
    with FnirsiPSU(a.port, a.baud, a.address) as psu:
        s = psu.state
        print(f"model={s.model} fw={s.firmware} hw={s.string_df} addr={s.address}")
        print(f"Vin={_fmt(s.vin, 3)} V  Vset={_fmt(s.vset, 3)} V  Iset={_fmt(s.iset, 3)} A  "
              f"max={_fmt(s.vset_max, 3)} V / {_fmt(s.iset_max, 3)} A  "
              f"output={'ON' if s.output_on else 'OFF'}  protection={_prot_name(s.protection)}")
        if a.set_v is not None:
            psu.set_voltage(a.set_v)
        if a.set_i is not None:
            psu.set_current(a.set_i)
        if a.on:
            psu.output(True)
        if a.off:
            psu.output(False)
        if a.monitor:
            end = time.monotonic() + a.monitor
            while time.monotonic() < end:
                v, i, w = psu.measure()
                print(f"{_fmt(v, 3):>7} V {_fmt(i, 4):>7} A {_fmt(w, 3):>7} W  "
                      f"T={_fmt(s.temp_c, 1)} C  {'CV' if s.cv_mode else 'CC'}  "
                      f"{_prot_name(s.protection)}")
                time.sleep(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
