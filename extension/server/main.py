"""MCP server (stdio) for FNIRSI USB bench power supplies.

Implements the MCP JSON-RPC stdio transport directly (no SDK) so the bundle's
only dependency is pure-Python pyserial and it runs on any Python >= 3.8.

Configuration (environment, filled from the extension's user_config):
  FNIRSI_PORT       serial port (COM5, /dev/ttyACM0); may also be given to `connect`
  FNIRSI_BAUD       9600 | 19200 | 38400 | 57600 | 115200 (default 9600)
  FNIRSI_ADDRESS    device address 1..255 (default 1)
  FNIRSI_MAX_V      extra software voltage cap, 0 = device limit only
  FNIRSI_MAX_I      extra software current cap, 0 = device limit only
  FNIRSI_SIMULATE   1 = use a built-in simulated PSU instead of hardware
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))
sys.path.insert(0, HERE)
# running from a source checkout: fnirsi_psu.py lives at the repo root and is
# only copied next to this file by scripts/build_mcpb.py
sys.path.append(os.path.normpath(os.path.join(HERE, "..", "..")))

import fnirsi_psu as fp  # noqa: E402

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("fnirsi-mcp")

SERVER_INFO = {"name": "fnirsi-psu", "version": "1.0.0"}
PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]


def _env_num(name, default, cast=float):
    v = os.environ.get(name, "").strip()
    if not v or v.startswith("${"):          # unset / unsubstituted placeholder
        return default
    try:
        return cast(float(v))
    except ValueError:
        return default


def _env_port():
    v = os.environ.get("FNIRSI_PORT", "").strip()
    return None if not v or v.startswith("${") else v


def _env_bool(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


class ToolError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Device session
# --------------------------------------------------------------------------- #

class Session:
    def __init__(self):
        self.psu: fp.FnirsiPSU | None = None
        self.port: str | None = None
        self.max_v = _env_num("FNIRSI_MAX_V", 0.0)
        self.max_i = _env_num("FNIRSI_MAX_I", 0.0)

    def connect(self, port=None, baudrate=None, address=None):
        self.disconnect()
        simulate = _env_bool("FNIRSI_SIMULATE")
        port = port or _env_port()
        if not port and not simulate:
            raise ToolError("No serial port given. Call list_serial_ports, then "
                            "connect with port=..., or set the port in the "
                            "extension settings.")
        baud = int(baudrate or _env_num("FNIRSI_BAUD", 9600, int))
        addr = int(address or _env_num("FNIRSI_ADDRESS", 1, int))
        factory = None
        if simulate:
            from fnirsi_sim import SimulatedPSU
            factory = lambda: SimulatedPSU(address=addr)  # noqa: E731
            port = port or "SIMULATED"
        try:
            self.psu = fp.FnirsiPSU(port, baud, addr, serial_factory=factory)
        except ConnectionError as e:
            raise ToolError(f"Port {port} opened but the PSU did not answer the "
                            f"handshake ({e}). Check the port, baud rate and "
                            f"device address.") from e
        except Exception as e:
            raise ToolError(f"Could not open {port}: {e}") from e
        self.port = port
        return self.psu

    def disconnect(self):
        if self.psu:
            try:
                self.psu.close()
            finally:
                self.psu = None
                self.port = None

    def need(self) -> fp.FnirsiPSU:
        if self.psu is None:
            # lazy auto-connect when a port (or simulation) is configured
            if _env_port() or _env_bool("FNIRSI_SIMULATE"):
                return self.connect()
            raise ToolError("Not connected. Call connect first.")
        return self.psu

    def limit_v(self, psu):
        lims = [x for x in (psu.state.vset_max, self.max_v or None) if x]
        return min(lims) if lims else None

    def limit_i(self, psu):
        lims = [x for x in (psu.state.iset_max, self.max_i or None) if x]
        return min(lims) if lims else None

    def check_v(self, psu, v):
        if v < 0:
            raise ToolError("voltage must be >= 0")
        lim = self.limit_v(psu)
        if lim is not None and v > lim + 1e-6:
            raise ToolError(f"{v} V exceeds the limit of {lim} V")

    def check_i(self, psu, a):
        if a < 0:
            raise ToolError("current must be >= 0")
        lim = self.limit_i(psu)
        if lim is not None and a > lim + 1e-6:
            raise ToolError(f"{a} A exceeds the limit of {lim} A")


S = Session()


def _r(x, n=4):
    return None if x is None else round(float(x), n)


def snapshot(psu: fp.FnirsiPSU, full=False) -> dict:
    s = psu.state
    out = {
        "output_on": s.output_on,
        "mode": None if s.cv_mode is None else ("CV" if s.cv_mode else "CC"),
        "protection": getattr(s.protection, "name", s.protection),
        "output": {"voltage_v": _r(s.vout), "current_a": _r(s.iout), "power_w": _r(s.pout)},
        "setpoint": {"voltage_v": _r(s.vset), "current_a": _r(s.iset)},
        "input_voltage_v": _r(s.vin, 2),
        "temperature_c": _r(s.temp_c, 1),
        "capacity_ah": _r(s.capacity_ah), "energy_wh": _r(s.energy_wh),
        "metering_on": s.metering,
    }
    if full:
        out.update({
            "port": S.port, "model": s.model, "firmware": s.firmware,
            "info_df": s.string_df, "address": s.address,
            "limits": {"voltage_v": _r(S.limit_v(psu)), "current_a": _r(S.limit_i(psu)),
                       "device_max_voltage_v": _r(s.vset_max),
                       "device_max_current_a": _r(s.iset_max)},
            "protection_thresholds": {"ovp_v": _r(s.ovp), "ocp_a": _r(s.ocp),
                                      "opp_w": _r(s.opp), "otp_c": _r(s.otp),
                                      "lvp_v": _r(s.lvp)},
            "presets": {str(n): {"voltage_v": _r(v), "current_a": _r(a)}
                        for n, (v, a) in s.presets.items()},
            "brightness": s.brightness,
        })
    return out


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

def t_list_serial_ports(_):
    from serial.tools import list_ports
    ports = [{"port": p.device, "description": p.description,
              "vid": f"{p.vid:04X}" if p.vid is not None else None,
              "pid": f"{p.pid:04X}" if p.pid is not None else None,
              "manufacturer": p.manufacturer, "serial_number": p.serial_number}
             for p in list_ports.comports()]
    return {"ports": ports, "configured_port": _env_port(),
            "hint": "The PSU appears as a USB serial (CDC) port. Unplug/replug to "
                    "see which entry appears if unsure."}


def t_connect(a):
    psu = S.connect(a.get("port"), a.get("baudrate"), a.get("address"))
    psu.refresh()
    return {"connected": True, **snapshot(psu, full=True)}


def t_disconnect(_):
    was = S.port
    S.disconnect()
    return {"disconnected": was}


def t_get_status(_):
    psu = S.need()
    if not psu.read(fp.Reg.ALL):
        raise ToolError("No reply to read-all request (device unplugged?)")
    return snapshot(psu, full=True)


def t_measure(_):
    psu = S.need()
    # bulk read: also refreshes CV/CC + protection, which C3 alone doesn't carry
    if not psu.read(fp.Reg.ALL):
        psu.measure()
    return snapshot(psu)


def t_set_output(a):
    psu = S.need()
    on = bool(a["on"])
    psu.output(on)
    time.sleep(0.3)
    psu.read(fp.Reg.ALL)
    return {"requested": "on" if on else "off", **snapshot(psu)}


def t_set_voltage(a):
    psu = S.need()
    v = float(a["voltage_v"])
    S.check_v(psu, v)
    psu.write_float(fp.Reg.VSET, v)
    psu.read(fp.Reg.VSET, timeout=0.5)
    return {"setpoint_voltage_v": _r(psu.state.vset if psu.state.vset is not None else v)}


def t_set_current(a):
    psu = S.need()
    i = float(a["current_a"])
    S.check_i(psu, i)
    psu.write_float(fp.Reg.ISET, i)
    psu.read(fp.Reg.ISET, timeout=0.5)
    return {"setpoint_current_a": _r(psu.state.iset if psu.state.iset is not None else i)}


def t_configure(a):
    """Set voltage and/or current and optionally switch output in one call."""
    psu = S.need()
    if "voltage_v" in a:
        S.check_v(psu, float(a["voltage_v"]))
    if "current_a" in a:
        S.check_i(psu, float(a["current_a"]))
    if "voltage_v" in a:
        psu.write_float(fp.Reg.VSET, float(a["voltage_v"]))
    if "current_a" in a:
        psu.write_float(fp.Reg.ISET, float(a["current_a"]))
    if "output_on" in a:
        psu.output(bool(a["output_on"]))
    time.sleep(0.3)
    psu.refresh()
    return snapshot(psu)


def t_set_protection(a):
    psu = S.need()
    keys = {"ovp_v": "ovp", "ocp_a": "ocp", "opp_w": "opp", "otp_c": "otp", "lvp_v": "lvp"}
    kw = {v: float(a[k]) for k, v in keys.items() if k in a}
    if not kw:
        raise ToolError("give at least one of " + ", ".join(keys))
    for k, val in kw.items():
        if val < 0:
            raise ToolError(f"{k} must be >= 0")
        mx = getattr(psu.state, f"{k}_max")
        if mx is not None and val > mx + 1e-6:
            raise ToolError(f"{k} {val} exceeds device maximum {mx}")
    psu.set_protection(**kw)
    psu.refresh()
    return snapshot(psu, full=True)["protection_thresholds"]


def t_set_preset(a):
    psu = S.need()
    n, v, i = int(a["preset"]), float(a["voltage_v"]), float(a["current_a"])
    S.check_v(psu, v)
    S.check_i(psu, i)
    psu.set_preset(n, v, i)
    pv, pa = psu.state.presets[n]
    return {"preset": n, "voltage_v": _r(pv), "current_a": _r(pa)}


def t_apply_preset(a):
    psu = S.need()
    n = int(a["preset"])
    psu.refresh()
    v, i = psu.state.presets[n]
    if v is None or i is None:
        raise ToolError(f"preset {n} unknown")
    S.check_v(psu, v)
    S.check_i(psu, i)
    psu.set_voltage(v)
    psu.set_current(i)
    return {"applied_preset": n, "voltage_v": _r(v), "current_a": _r(i)}


def t_set_display(a):
    psu = S.need()
    done = {}
    if "brightness" in a:
        psu.set_brightness(int(a["brightness"]))
        done["brightness"] = int(a["brightness"])
    if "metering_on" in a:
        psu.set_metering(bool(a["metering_on"]))
        done["metering_on"] = bool(a["metering_on"])
    if not done:
        raise ToolError("give brightness and/or metering_on")
    return done


def t_monitor(a):
    psu = S.need()
    dur = min(max(float(a.get("duration_s", 5)), 0.2), 60.0)
    step = min(max(float(a.get("interval_s", 0.5)), 0.2), dur)
    samples, t0 = [], time.monotonic()
    while True:
        psu.measure()
        s = psu.state
        samples.append({"t_s": round(time.monotonic() - t0, 2), "v": _r(s.vout),
                        "a": _r(s.iout), "w": _r(s.pout),
                        "mode": "CV" if s.cv_mode else "CC" if s.cv_mode is not None else None})
        if time.monotonic() - t0 + step > dur:
            break
        time.sleep(step)

    def stats(k):
        xs = [x[k] for x in samples if x[k] is not None]
        return None if not xs else {"min": min(xs), "max": max(xs),
                                    "mean": round(statistics.fmean(xs), 4)}
    return {"samples": samples, "stats": {k: stats(k) for k in ("v", "a", "w")},
            "protection": getattr(psu.state.protection, "name", psu.state.protection)}


def t_sweep(a):
    psu = S.need()
    mode = a["mode"]
    start, stop, step = float(a["start"]), float(a["stop"]), float(a["step"])
    hold = float(a["hold"])
    dwell = min(max(float(a.get("dwell_s", 0.5)), 0.2), 10.0)
    if step <= 0 or stop < start:
        raise ToolError("need step > 0 and stop >= start")
    pts = list(fp._frange(start, stop, step))
    if len(pts) > 200:
        raise ToolError(f"{len(pts)} points; max 200")
    if len(pts) * dwell > 120:
        raise ToolError("sweep would take > 120 s; reduce points or dwell")
    chk_sweep, chk_hold = (S.check_v, S.check_i) if mode == "voltage" else (S.check_i, S.check_v)
    chk_sweep(psu, stop)
    chk_hold(psu, hold)

    if mode == "voltage":
        psu.write_float(fp.Reg.ISET, hold)
        set_x = lambda x: psu.write_float(fp.Reg.VSET, x)  # noqa: E731
    else:
        psu.write_float(fp.Reg.VSET, hold)
        set_x = lambda x: psu.write_float(fp.Reg.ISET, x)  # noqa: E731
    set_x(pts[0])
    psu.output(True)
    rows = []
    try:
        for x in pts:
            set_x(x)
            time.sleep(dwell)
            psu.read(fp.Reg.ALL)
            s = psu.state
            rows.append({"set": x, "v": _r(s.vout), "a": _r(s.iout), "w": _r(s.pout),
                         "mode": "CV" if s.cv_mode else "CC"})
            if s.protection not in (None, fp.Protection.NORMAL, 0):
                rows[-1]["protection"] = getattr(s.protection, "name", s.protection)
                break
    finally:
        if a.get("output_off_after", True):
            psu.output(False)
    return {"mode": mode, "hold": hold, "points": rows,
            "output_left_on": not a.get("output_off_after", True)}


def t_raw_command(a):
    psu = S.need()
    cmd, reg = int(a["cmd"], 16), int(a["reg"], 16)
    data = bytes.fromhex(a.get("data_hex", "00"))
    t = time.monotonic()
    frame = psu.send(cmd, reg, data)
    got = psu.wait_for(reg, t, timeout=float(a.get("timeout_s", 1.0)))
    return {"sent": frame.hex(" "), "reply_received": got,
            "state": snapshot(psu, full=True) if got else None}


NUM = {"type": "number"}
TOOLS = [
    ("list_serial_ports", t_list_serial_ports,
     "List serial/COM ports on this computer (the PSU appears as a USB CDC port).",
     {}, [], {"readOnlyHint": True}),
    ("connect", t_connect,
     "Connect to the PSU (handshake + full read). Uses the configured port if "
     "none given. Returns model, firmware, limits and current state.",
     {"port": {"type": "string", "description": "e.g. COM5 or /dev/ttyACM0"},
      "baudrate": {"type": "integer", "enum": list(fp.BAUD_RATES)},
      "address": {"type": "integer", "minimum": 1, "maximum": 255}}, [], {}),
    ("disconnect", t_disconnect, "Leave PC-link mode and close the port.", {}, [], {}),
    ("get_status", t_get_status,
     "Full device state: output, set-points, measurements, limits, protection "
     "thresholds, presets, temperature, Ah/Wh counters.", {}, [], {"readOnlyHint": True}),
    ("measure", t_measure,
     "Fresh output voltage / current / power reading plus CV/CC mode and "
     "protection state.", {}, [], {"readOnlyHint": True}),
    ("set_output", t_set_output,
     "Turn the output ON or OFF. Turning ON energizes whatever is connected: "
     "confirm the set-points and load with the user first.",
     {"on": {"type": "boolean"}}, ["on"], {"destructiveHint": True}),
    ("set_voltage", t_set_voltage, "Set the output voltage set-point (V).",
     {"voltage_v": {**NUM, "minimum": 0}}, ["voltage_v"], {"idempotentHint": True}),
    ("set_current", t_set_current, "Set the output current limit (A).",
     {"current_a": {**NUM, "minimum": 0}}, ["current_a"], {"idempotentHint": True}),
    ("configure", t_configure,
     "Set voltage and/or current and optionally switch output, in one call. "
     "Limits are checked before anything is sent.",
     {"voltage_v": {**NUM, "minimum": 0}, "current_a": {**NUM, "minimum": 0},
      "output_on": {"type": "boolean"}}, [], {"destructiveHint": True}),
    ("set_protection", t_set_protection,
     "Set protection thresholds: over-voltage (V), over-current (A), "
     "over-power (W), over-temperature (C), low input voltage (V).",
     {"ovp_v": NUM, "ocp_a": NUM, "opp_w": NUM, "otp_c": NUM, "lvp_v": NUM}, [], {}),
    ("set_preset", t_set_preset, "Store voltage/current into preset group M1..M6.",
     {"preset": {"type": "integer", "minimum": 1, "maximum": 6},
      "voltage_v": {**NUM, "minimum": 0}, "current_a": {**NUM, "minimum": 0}},
     ["preset", "voltage_v", "current_a"], {}),
    ("apply_preset", t_apply_preset, "Load preset M1..M6 into the live set-points.",
     {"preset": {"type": "integer", "minimum": 1, "maximum": 6}}, ["preset"], {}),
    ("set_display", t_set_display, "Set screen brightness (1-14) and/or Ah/Wh metering.",
     {"brightness": {"type": "integer", "minimum": 1, "maximum": 14},
      "metering_on": {"type": "boolean"}}, [], {}),
    ("monitor", t_monitor,
     "Sample V/I/P repeatedly for up to 60 s and return samples with min/max/mean.",
     {"duration_s": {**NUM, "minimum": 0.2, "maximum": 60, "default": 5},
      "interval_s": {**NUM, "minimum": 0.2, "default": 0.5}}, [], {"readOnlyHint": True}),
    ("sweep", t_sweep,
     "Step voltage (or current) from start to stop while holding the other "
     "value, measuring at each step (IV curve, LED/diode test). Turns output "
     "ON for the sweep and OFF afterwards by default. Max 200 points / 120 s.",
     {"mode": {"type": "string", "enum": ["voltage", "current"]},
      "start": NUM, "stop": NUM, "step": {**NUM, "exclusiveMinimum": 0},
      "hold": {**NUM, "description": "current limit (voltage mode) or voltage (current mode)"},
      "dwell_s": {**NUM, "minimum": 0.2, "maximum": 10, "default": 0.5},
      "output_off_after": {"type": "boolean", "default": True}},
     ["mode", "start", "stop", "step", "hold"], {"destructiveHint": True}),
    ("raw_command", t_raw_command,
     "Advanced: send one raw protocol frame (F1 CMD REG LEN DATA SUM) and wait "
     "for a reply on REG. cmd A1=read, B1=write. Bypasses safety limits; use "
     "only when asked for protocol exploration.",
     {"cmd": {"type": "string", "description": "hex, e.g. A1"},
      "reg": {"type": "string", "description": "hex, e.g. C3"},
      "data_hex": {"type": "string", "default": "00"},
      "timeout_s": {**NUM, "default": 1.0}}, ["cmd", "reg"], {"destructiveHint": True}),
]
HANDLERS = {name: fn for name, fn, *_ in TOOLS}


def tool_list():
    out = []
    for name, _, desc, props, req, ann in TOOLS:
        out.append({"name": name, "description": desc,
                    "inputSchema": {"type": "object", "properties": props,
                                    "required": req, "additionalProperties": False},
                    "annotations": ann})
    return out


# --------------------------------------------------------------------------- #
# JSON-RPC / MCP stdio
# --------------------------------------------------------------------------- #

def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:                       # notification
        return None
    p = msg.get("params") or {}
    if method == "initialize":
        want = p.get("protocolVersion")
        ver = want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        res = {"protocolVersion": ver, "capabilities": {"tools": {}},
               "serverInfo": SERVER_INFO,
               "instructions": "Controls a FNIRSI USB bench power supply. "
                               "Call connect (or list_serial_ports) first. Never "
                               "switch the output on without the user's go-ahead."}
    elif method == "ping":
        res = {}
    elif method == "tools/list":
        res = {"tools": tool_list()}
    elif method == "tools/call":
        name, args = p.get("name"), p.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"unknown tool {name}"}}
        try:
            data = fn(args)
            res = {"content": [{"type": "text", "text": json.dumps(data, indent=1)}],
                   "isError": False}
        except ToolError as e:
            res = {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except (KeyError, ValueError, TypeError) as e:
            res = {"content": [{"type": "text", "text": f"bad arguments: {e}"}],
                   "isError": True}
        except Exception as e:
            log.error("tool %s failed:\n%s", name, traceback.format_exc())
            res = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                   "isError": True}
    elif method in ("resources/list", "prompts/list"):
        res = {method.split("/")[0]: []}
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": res}


def main():
    log.info("fnirsi-psu MCP server starting (simulate=%s)", _env_bool("FNIRSI_SIMULATE"))
    stdin = sys.stdin.buffer
    out = sys.stdout.buffer
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                resp = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "parse error"}}
            else:
                resp = handle(msg) if isinstance(msg, dict) else None
            if resp is not None:
                out.write(json.dumps(resp).encode() + b"\n")
                out.flush()
    finally:
        S.disconnect()


if __name__ == "__main__":
    main()
