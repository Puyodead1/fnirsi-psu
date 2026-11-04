# fnirsi-psu

Open-source control for **FNIRSI USB bench power supplies**:

- **[`fnirsi_psu.py`](fnirsi_psu.py)**: a standalone Python driver and command-line tool
- **[`extension/`](extension)**: a Claude Desktop extension (`.mcpb`, an MCP server) that lets Claude read and drive the supply
- **[`docs/PROTOCOL.md`](docs/PROTOCOL.md)**: the serial protocol, reverse-engineered from FNIRSI's official Windows app (V1.0.0.0, 2023-12-21)

> [!NOTE]
> Tested on a **DPS-150** (firmware V1.2). These all work on hardware: the handshake, readings, the push stream, writes (set-points, presets, protection thresholds, brightness, metering) and output on/off. **Behaviour under load (CC mode, Ah/Wh counters, protection trips) is not yet verified** (see the [verification status](docs/PROTOCOL.md#7-hardware-verification-status)). Reports from owners of other models are very welcome.

Not affiliated with or endorsed by Shenzhen FNIRSI Technology Co., Ltd. "FNIRSI" is their trademark.

## Protocol in one minute

The supply enumerates as a **USB CDC virtual COM port** (8N1, RTS on). Frames look like:

```
HDR  CMD  REG  LEN  DATA[LEN]  SUM       HDR = F1 (PC→PSU) / F0 (PSU→PC)
                                         SUM = (REG + LEN + ΣDATA) & 0xFF
```

`CMD`: `A1` read, `B1` write, `B0` baud, `C0` bootloader, `C1` PC-link on/off. Values are little-endian `float32`; registers `C0`–`E3` hold set-points, live V/I/P, presets, protections and status; `FF` returns everything in one block. Full register map, session sequence and quirks are in [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Python driver

Needs Python 3.8+ and `pyserial`:

```bash
pip install pyserial
```

```python
from fnirsi_psu import FnirsiPSU

with FnirsiPSU("COM5") as psu:          # or /dev/ttyACM0
    print(psu.model, psu.firmware)
    psu.set_voltage(5.0)
    psu.set_current(0.5)
    psu.output(True)
    print(psu.measure())                # (volts, amps, watts)
    psu.output(False)
```

Command line:

```bash
python fnirsi_psu.py COM5 --set-v 3.3 --set-i 0.2 --on --monitor 10
```

The driver also has protection thresholds, presets M1–M6, brightness, Ah/Wh metering, voltage and current sweeps, and list-mode sequencing (`run_list`). It can reproduce the original app's buggy baud-rate checksum (`legacy_baud_checksum=True`).

## Claude Desktop extension

1. Download `fnirsi-psu-<version>.mcpb` from [Releases](../../releases) (or [build it](#building-the-extension)).
2. Double-click it, or drag it into **Claude Desktop → Settings → Extensions**.
3. Set the serial port, or leave it empty and let Claude pick one with `list_serial_ports`. You can also set a safety voltage/current cap, or turn on **simulation mode** to try it without hardware.

Python 3.8+ must be on `PATH`. The bundle ships its only dependency (pure-Python `pyserial`), so it works on Windows, macOS and Linux.

| Tool | What it does |
|---|---|
| `list_serial_ports`, `connect`, `disconnect` | Find the port, handshake, close |
| `get_status`, `measure` | Full state, or a fresh V/I/P + CV/CC + protection reading |
| `set_voltage`, `set_current`, `set_output`, `configure` | Set-points and output switching |
| `set_protection` | OVP / OCP / OPP / OTP / LVP thresholds |
| `set_preset`, `apply_preset` | Preset groups M1–M6 |
| `set_display` | Brightness, Ah/Wh metering |
| `monitor` | Sample for up to 60 s with min/max/mean |
| `sweep` | Voltage or current sweep with a reading at each step (IV curves) |
| `raw_command` | Send one raw frame (protocol exploration) |

Safety:
- Set-points above the device maximum or your configured cap are refused before anything is sent.
- Output-energizing tools are marked destructive, and Claude is told to ask before switching the output on.
- `sweep` turns the output off when it finishes and stops if a protection trips.
- The firmware-upgrade command is deliberately not exposed.

The server also works with any other MCP client, e.g. Claude Code:

```bash
claude mcp add fnirsi-psu -e FNIRSI_PORT=COM5 -- python /path/to/fnirsi-psu/extension/server/main.py
```

Environment variables: `FNIRSI_PORT`, `FNIRSI_BAUD`, `FNIRSI_ADDRESS`, `FNIRSI_MAX_V`, `FNIRSI_MAX_I`, `FNIRSI_SIMULATE=1`.

## Development

```bash
pip install pyserial
python -m unittest discover -s tests -v
```

The tests need no hardware:
- `test_fnirsi_psu.py` checks frame encoding against bytes dumped from the original app's own command classes, plus parser resync, checksum handling and decoding of the bulk block.
- `test_mcp_server.py` starts the MCP server over stdio against the built-in simulator ([`extension/server/fnirsi_sim.py`](extension/server/fnirsi_sim.py)) and runs a full session.

### Building the extension

```bash
python scripts/build_mcpb.py     # → dist/fnirsi-psu-<version>.mcpb  (needs Node.js for npx)
```

The script stages `extension/`, copies in `fnirsi_psu.py`, vendors `pyserial` into `server/lib`, then validates and packs with [`@anthropic-ai/mcpb`](https://github.com/modelcontextprotocol/mcpb). CI does the same on every push, tests the packed bundle, and attaches it to `v*` tag releases.

## Repository layout

```
fnirsi_psu.py              driver + CLI (single source, copied into the bundle at build time)
docs/PROTOCOL.md           protocol specification
extension/manifest.json    MCPB manifest
extension/server/main.py   MCP stdio server (no SDK dependency)
extension/server/fnirsi_sim.py   simulated PSU
scripts/build_mcpb.py      bundle builder
tests/                     offline tests
```

## License

[MIT](LICENSE). This repository contains no FNIRSI code or binaries. The protocol was documented by analysing the official app, for interoperability.
