"""Offline tests for fnirsi_psu: no hardware needed.

Run:  python -m unittest discover -s tests
Checks frame bytes against values dumped from the original EXE's own
Command/CmdMoudle classes, and drives the driver against a simulated PSU.
"""
import os
import queue
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fnirsi_psu as fp  # noqa: E402
from fnirsi_psu import Cmd, Reg, build_frame, FrameParser, FnirsiPSU

# Ground truth: bytes produced by the original .NET assembly (via reflection).
GROUND_TRUTH = {
    (Cmd.LINK, 0x00, 1): "F1 C1 00 01 01 02",
    (Cmd.LINK, 0x00, 0): "F1 C1 00 01 00 01",
    (Cmd.READ, Reg.ALL, 0): "F1 A1 FF 01 00 00",
    (Cmd.READ, Reg.VIN, 0): "F1 A1 C0 01 00 C1",
    (Cmd.READ, Reg.MODEL, 0): "F1 A1 DE 01 00 DF",
    (Cmd.READ, Reg.FIRMWARE, 0): "F1 A1 E0 01 00 E1",
    (Cmd.READ, Reg.ADDRESS, 0): "F1 A1 E1 01 00 E2",
    (Cmd.WRITE, Reg.OUTPUT_ON, 0): "F1 B1 DB 01 00 DC",
    (Cmd.WRITE, Reg.OUTPUT_ON, 1): "F1 B1 DB 01 01 DD",
    (Cmd.WRITE, Reg.METERING, 1): "F1 B1 D8 01 01 DA",
    (Cmd.WRITE, Reg.METERING, 0): "F1 B1 D8 01 00 D9",
    (Cmd.READ, Reg.BRIGHTNESS, 0): "F1 A1 D6 01 00 D7",
    (Cmd.SET_BAUD, 0x00, 0): "F1 B0 00 01 00 01",
    (Cmd.READ, Reg.OUTPUT_VIP, 0): "F1 A1 C3 01 00 C4",
    (Cmd.BOOT, 0x00, 1): "F1 C0 00 01 01 02",
    (Cmd.READ, Reg.STRING_DF, 0): "F1 A1 DF 01 00 E0",
    (Cmd.READ, Reg.VSET, 0): "F1 A1 C1 01 00 C2",
    (Cmd.READ, Reg.ISET, 0): "F1 A1 C2 01 00 C3",
}
GT_SETV_12 = "F1 B1 C1 04 00 00 40 41 46"
GT_PRESET6_I = "F1 B1 D0 04 33 33 53 40 CD"
GT_BAUD5_LEGACY = "F1 B0 00 01 05 01"


def hx(b):
    return b.hex(" ").upper()


def dev_frame(reg, data, cmd=0xA1):
    return build_frame(cmd, reg, data)


def make_bulk():
    d = bytearray(139)
    struct.pack_into("<7f", d, 0, 19.5, 12.0, 1.5, 11.98, 0.75, 8.985, 36.0)
    for n in range(6):
        struct.pack_into("<2f", d, 28 + 8 * n, n + 1.0, 0.1 * (n + 1))
    struct.pack_into("<5f", d, 76, 25.0, 5.5, 120.0, 80.0, 6.0)
    d[96], d[97], d[98] = 7, 0, 1
    struct.pack_into("<2f", d, 99, 0.123, 1.456)
    d[107], d[108], d[109], d[110] = 1, 0, 1, 1
    struct.pack_into("<2f", d, 111, 30.0, 5.0)
    struct.pack_into("<5f", d, 119, 31.0, 5.1, 150.0, 85.0, 30.0)
    return bytes(d)


class FakePSU:
    """Minimal device simulator behind a pyserial-like interface."""

    def __init__(self, address=1, chunk=3):
        self.address = address
        self.rx = queue.Queue()
        self.tx_log = []
        self.chunk = chunk          # deliver replies in small pieces
        self.parser = FrameParser()
        self.regs = {Reg.VSET: 0.0, Reg.ISET: 0.0}
        self.rts = False

    # pyserial API
    def write(self, b):
        self.tx_log.append(bytes(b))
        for fr in self.parser.feed(bytes(b)):
            self._on(fr)

    def read(self, n):
        try:
            return self.rx.get(timeout=0.05)
        except queue.Empty:
            return b""

    def close(self):
        pass

    def _reply(self, frame):
        for i in range(0, len(frame), self.chunk):
            self.rx.put(frame[i:i + self.chunk])

    def _on(self, fr):
        if fr.cmd == Cmd.READ:
            if fr.reg == Reg.ADDRESS:
                self._reply(dev_frame(Reg.ADDRESS, self.address))
            elif fr.reg == Reg.MODEL:
                self._reply(dev_frame(Reg.MODEL, b"DPS-150\x00"))
            elif fr.reg == Reg.FIRMWARE:
                self._reply(dev_frame(Reg.FIRMWARE, b"V1.2"))
            elif fr.reg == Reg.STRING_DF:
                self._reply(dev_frame(Reg.STRING_DF, b"HW1.0"))
            elif fr.reg == Reg.ALL:
                self._reply(b"\x00\x13" + dev_frame(Reg.ALL, make_bulk()))  # junk prefix
            elif fr.reg == Reg.OUTPUT_VIP:
                self._reply(dev_frame(Reg.OUTPUT_VIP, struct.pack(
                    "<3f", self.regs[Reg.VSET], 0.25, self.regs[Reg.VSET] * 0.25)))
        elif fr.cmd == Cmd.WRITE and len(fr.data) == 4:
            self.regs[fr.reg] = struct.unpack("<f", fr.data)[0]


class FrameTests(unittest.TestCase):
    def test_ground_truth(self):
        for (cmd, reg, data), want in GROUND_TRUTH.items():
            self.assertEqual(hx(build_frame(cmd, reg, data)), want)
        self.assertEqual(hx(build_frame(Cmd.WRITE, Reg.VSET, fp.f32(12.0))), GT_SETV_12)
        _, ireg = fp.preset_regs(6)
        self.assertEqual(hx(build_frame(Cmd.WRITE, ireg, fp.f32(3.3))), GT_PRESET6_I)

    def test_preset_regs(self):
        self.assertEqual(fp.preset_regs(1), (0xC5, 0xC6))
        self.assertEqual(fp.preset_regs(6), (0xCF, 0xD0))

    def test_parser_fragmented_and_resync(self):
        p = FrameParser()
        stream = b"\xAA" + dev_frame(Reg.TEMP, fp.f32(40.0)) + dev_frame(Reg.CV_CC, 1)
        frames = []
        for b in stream:
            frames += p.feed(bytes([b]))
        self.assertEqual([f.reg for f in frames], [Reg.TEMP, Reg.CV_CC])
        self.assertTrue(all(f.checksum_ok for f in frames))

    def test_parser_bad_checksum(self):
        bad = bytearray(dev_frame(Reg.CV_CC, 1)); bad[-1] ^= 0xFF
        self.assertFalse(FrameParser().feed(bytes(bad))[0].checksum_ok)
        self.assertEqual(FrameParser(strict=True).feed(bytes(bad)), [])

    def test_bulk_decode(self):
        st = fp.State()
        fp.apply_bulk(st, make_bulk())
        self.assertAlmostEqual(st.vin, 19.5)
        self.assertAlmostEqual(st.iout, 0.75)
        self.assertEqual(st.presets[6][0], 6.0)
        self.assertAlmostEqual(st.lvp, 6.0)
        self.assertAlmostEqual(st.ocp, 5.5)
        self.assertEqual(st.brightness, 7)
        self.assertTrue(st.output_on and st.cv_mode and st.metering)
        self.assertEqual(st.protection, fp.Protection.NORMAL)
        self.assertAlmostEqual(st.iset_max, 5.0)
        self.assertAlmostEqual(st.lvp_max, 30.0)


class DriverTests(unittest.TestCase):
    def test_handshake_and_control(self):
        dev = FakePSU()
        psu = FnirsiPSU("SIM", baudrate=115200, serial_factory=lambda: dev)
        try:
            self.assertEqual(psu.model, "DPS-150")
            self.assertEqual(psu.firmware, "V1.2")
            self.assertAlmostEqual(psu.state.vset_max, 30.0)
            sent = [hx(f) for f in dev.tx_log]
            self.assertEqual(sent[0], "F1 C1 00 01 01 02")
            self.assertIn("F1 B0 00 01 05 06", sent)          # corrected checksum
            psu.set_voltage(5.0)
            v, i, w = psu.measure()
            self.assertAlmostEqual(v, 5.0)
            self.assertAlmostEqual(w, 1.25)
            with self.assertRaises(ValueError):
                psu.set_voltage(31.0)
            psu.output(True)
            self.assertEqual(hx(dev.tx_log[-1]), "F1 B1 DB 01 01 DD")
        finally:
            psu.close()
        self.assertEqual(hx(dev.tx_log[-1]), "F1 C1 00 01 00 01")

    def test_legacy_baud(self):
        dev = FakePSU()
        psu = FnirsiPSU("SIM", baudrate=115200, legacy_baud_checksum=True,
                        serial_factory=lambda: dev)
        psu.close()
        self.assertIn(GT_BAUD5_LEGACY, [hx(f) for f in dev.tx_log])

    def test_read_waits_for_new_reply_with_coarse_clock(self):
        """Regression: Windows/Python<3.13 time.monotonic() ticks every 15.6 ms.
        A read issued in the same tick as an earlier reply must still wait."""
        class CoarseTime:
            sleep = staticmethod(time.sleep)

            @staticmethod
            def monotonic():
                return (time.monotonic() // 0.015625) * 0.015625

        class SlowPSU(FakePSU):
            """Replies ~5 ms late (inside one clock tick); every read-all
            reply reports a new input voltage so stale state is detectable."""
            n = 0

            def _on(self, fr):
                if fr.cmd == Cmd.READ and fr.reg == Reg.ALL:
                    SlowPSU.n += 1
                    d = bytearray(make_bulk())
                    struct.pack_into("<f", d, 0, float(SlowPSU.n))
                    frame = dev_frame(Reg.ALL, bytes(d))
                    threading.Timer(0.005, FakePSU._reply, (self, frame)).start()
                else:
                    FakePSU._on(self, fr)

        dev = SlowPSU()
        psu = FnirsiPSU("SIM", serial_factory=lambda: dev)
        real_time, fp.time = fp.time, CoarseTime
        try:
            for _ in range(20):
                self.assertTrue(psu.read(Reg.ALL))
                self.assertEqual(psu.state.vin, float(SlowPSU.n))   # fresh, not stale
        finally:
            fp.time = real_time
            psu.close()

    def test_wrong_address(self):
        dev = FakePSU(address=7)
        t = time.monotonic()
        with self.assertRaises(ConnectionError):
            FnirsiPSU("SIM", address=1, serial_factory=lambda: dev)
        self.assertLess(time.monotonic() - t, 3.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
