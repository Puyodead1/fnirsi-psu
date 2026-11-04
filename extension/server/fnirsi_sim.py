"""In-memory simulated FNIRSI PSU with a pyserial-like interface.

Used when FNIRSI_SIMULATE=1 (demo / tests without hardware). It speaks the
same wire protocol as the real device, so the whole driver stack is exercised.
"""
import queue
import struct
import threading
import time

from fnirsi_psu import Cmd, Reg, FrameParser, build_frame, DEVICE_HEADER


class SimulatedPSU:
    def __init__(self, address=1, load_ohms=10.0):
        self.address = address
        self.load = load_ohms
        self.rx = queue.Queue()
        self.parser = FrameParser()
        self.rts = False
        self.vin, self.temp = 24.0, 32.0
        self.vset, self.iset = 5.0, 1.0
        self.vmax, self.imax = 30.0, 5.0
        self.presets = [[float(n), 0.5] for n in range(1, 7)]
        self.prot = [31.0, 5.1, 150.0, 80.0, 10.0]   # OVP OCP OPP OTP LVP
        self.brightness, self.metering, self.on = 7, 1, 0
        self.ah = self.wh = 0.0
        self.online = False
        # one lock for state + reply queueing, so a periodic push can never be
        # computed from old state and queued after a newer reply
        self._lock = threading.RLock()
        self._stop = threading.Event()
        threading.Thread(target=self._push_loop, daemon=True).start()

    # electrical model: resistive load, CV/CC crossover
    def _vip(self):
        if not self.on:
            return 0.0, 0.0, 0.0, 1
        i = self.vset / self.load
        if i > self.iset:
            i, v, cv = self.iset, self.iset * self.load, 0
        else:
            v, cv = self.vset, 1
        return v, i, v * i, cv

    def _f(self, reg, data):
        self.rx.put(build_frame(0xA1, reg, data, header=DEVICE_HEADER))

    def _push_loop(self):
        while not self._stop.wait(0.5):
            with self._lock:
                if self.online:
                    v, i, p, cv = self._vip()
                    self.ah += i * 0.5 / 3600
                    self.wh += p * 0.5 / 3600
                    self._f(Reg.OUTPUT_VIP, struct.pack("<3f", v, i, p))
                    self._f(Reg.CV_CC, cv)

    def _bulk(self):
        v, i, p, cv = self._vip()
        d = bytearray(139)
        struct.pack_into("<7f", d, 0, self.vin, self.vset, self.iset, v, i, p, self.temp)
        for n, (pv, pa) in enumerate(self.presets):
            struct.pack_into("<2f", d, 28 + 8 * n, pv, pa)
        struct.pack_into("<5f", d, 76, *self.prot)
        d[96], d[97], d[98] = self.brightness, 0, self.metering
        struct.pack_into("<2f", d, 99, self.ah, self.wh)
        d[107], d[108], d[109], d[110] = self.on, 0, cv, self.address
        struct.pack_into("<2f", d, 111, self.vmax, self.imax)
        struct.pack_into("<5f", d, 119, 31.0, 5.1, 150.0, 85.0, 30.0)
        return bytes(d)

    # pyserial API
    def write(self, b):
        with self._lock:
            for fr in self.parser.feed(bytes(b)):
                self._on(fr)
        return len(b)

    def read(self, n):
        try:
            return self.rx.get(timeout=0.05)
        except queue.Empty:
            return b""

    def close(self):
        self._stop.set()

    def _on(self, fr):
        r = fr.reg
        if fr.cmd == Cmd.LINK:
            self.online = bool(fr.data[0])
        elif fr.cmd == Cmd.READ:
            v, i, p, cv = self._vip()
            table = {
                Reg.ADDRESS: bytes([self.address]),
                Reg.MODEL: b"SIM-PSU",
                Reg.FIRMWARE: b"V0.0-sim",
                Reg.STRING_DF: b"SIM",
                Reg.ALL: self._bulk(),
                Reg.OUTPUT_VIP: struct.pack("<3f", v, i, p),
                Reg.VIN: struct.pack("<f", self.vin),
                Reg.VSET: struct.pack("<f", self.vset),
                Reg.ISET: struct.pack("<f", self.iset),
                Reg.BRIGHTNESS: bytes([self.brightness]),
            }
            if r in table:
                self._f(r, table[r])
        elif fr.cmd == Cmd.WRITE:
            if len(fr.data) == 4:
                val = struct.unpack("<f", fr.data)[0]
                if r == Reg.VSET:
                    self.vset = val
                elif r == Reg.ISET:
                    self.iset = val
                elif 0xC5 <= r <= 0xD0:
                    n, k = divmod(r - 0xC5, 2)
                    self.presets[n][k] = val
                elif 0xD1 <= r <= 0xD5:
                    self.prot[r - 0xD1] = val
            else:
                b = fr.data[0]
                if r == Reg.OUTPUT_ON:
                    self.on = b
                    self._f(Reg.OUTPUT_ON, b)
                elif r == Reg.BRIGHTNESS:
                    self.brightness = b
                elif r == Reg.METERING:
                    self.metering = b
