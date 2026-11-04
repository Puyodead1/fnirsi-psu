# FNIRSI Power Supply — PC Protocol (reverse engineered)

Source: `FNIRSI Power supply.exe` (V1.0.0.0, 2023/12/21), a .NET Framework 4 WinForms
app (internal name `数控电源上位机`, "CNC power supply host software"). It is not
obfuscated; the third-party DLLs (Newtonsoft.Json, EPPlus for Excel export) are packed
in with Costura and play no part in device I/O. Decompiled with ILSpy.

Key classes: `Serial` (transport), `CmdMoudle` (frame builder), `Command` (fixed frames),
`SerialData` (RX parser and register map), `UserSelectBox2Data1..4` (setters that build
frames on the fly).

## 1. Transport

There is no raw USB, HID, libusb or WinUSB code. The device enumerates as a **USB CDC
virtual COM port**, and the app talks to it through `System.IO.Ports.SerialPort`:

| Setting | Value |
|---|---|
| Baud | user-selected from 9600 / 19200 / 38400 / 57600 / 115200 (UI default 9600) |
| Format | 8N1, no flow control |
| RTS | asserted (`RtsEnable = true`) |
| Read timeout | 500 ms |

On a CDC device the baud rate usually doesn't matter, but the app also sends the chosen
baud to the device (cmd `0xB0`, below).

## 2. Frame format

Host and device use the same frame layout:

```
+------+------+------+------+----------------+------+
| HDR  | CMD  | REG  | LEN  | DATA[LEN]      | SUM  |
+------+------+------+------+----------------+------+
```

* `HDR` depends on direction: **`0xF1` host → device, `0xF0` device → host**.
  This was confirmed on a DPS-150. The original app never checks byte 0, so the
  difference doesn't show up in static analysis.
* `SUM = (REG + LEN + sum(DATA)) & 0xFF`. The header and CMD bytes are **not** included.
* Multi-byte values are **little-endian**. Analog values are IEEE-754 `float32`.
* Every host frame carries at least one data byte. Reads send `LEN=1, DATA=00`.
* Total frame length = `5 + LEN`.

### Command bytes (host → device)

| CMD | Meaning |
|---|---|
| `0xA1` | Read register (data = `00`) |
| `0xB1` | Write register |
| `0xB0` | Set baud rate (REG `00`, data = index 1..5 into the baud list) |
| `0xC0` | Enter bootloader / firmware-upgrade (USB mass-storage) mode (REG `00`, data `01`) |
| `0xC1` | PC-link session: data `01` = online, `00` = offline |

### Device → host

Replies and unsolicited pushes use the same layout, with header `0xF0` and CMD `0xA1`.
The device uses the same checksum formula. The app ignores bytes 0 and 1 and the checksum,
and dispatches only on `REG` (byte 2) and `LEN` (byte 3).

Observed on a DPS-150 (fw V1.2): once online, the device pushes **`C3` (V/I/P), `C0`
(Vin), `E2` (max Vset), `E3` (max Iset) and `C4` (temperature)** about every 0.55 s.
It also answers individual reads of `C0`/`C1`/`C2`/`C3`/`D6`, which the app never uses.
Mode, protection and output state (`DB`…`DD`) were not pushed while idle. Read them with
`FF`.

## 3. Register map

`R` = readable via `A1`, `W` = writable via `B1`, `P` = arrives in pushes/replies.
Offsets are positions inside the `0xFF` bulk block (§4).

| REG | Type | Access | Off | Meaning |
|---|---|---|---|---|
| `C0` | f32 | R P | 0 | Input voltage (V) |
| `C1` | f32 | R W | 4 | Voltage set-point (V) |
| `C2` | f32 | R W | 8 | Current set-point (A) |
| `C3` | 3×f32 | R P | 12 | Output V (V), I (A), P (W) |
| `C4` | f32 | P | 24 | System temperature (°C) |
| `C5`/`C6` … `CF`/`D0` | f32 | W | 28…72 | Preset groups M1…M6: V then I. `V reg = 0xC3 + 2·n`, `I reg = 0xC4 + 2·n`, n = 1..6 |
| `D1` | f32 | W | 76 | OVP: over-voltage protection threshold |
| `D2` | f32 | W | 80 | OCP: over-current protection threshold |
| `D3` | f32 | W | 84 | OPP: over-power protection threshold |
| `D4` | f32 | W | 88 | OTP: over-temperature protection threshold |
| `D5` | f32 | W | 92 | LVP: low (input) voltage protection threshold |
| `D6` | u8 | R W | 96 | Screen brightness (1..14) |
| `D7` | u8 | — | 97 | Unknown (parsed but never used; `10` on a DPS-150, same as brightness) |
| `D8` | u8 | W | 98 | Energy metering on (1) / off (0) |
| `D9` | f32 | P | 99 | Accumulated capacity (Ah) |
| `DA` | f32 | P | 103 | Accumulated energy (Wh) |
| `DB` | u8 | W P | 107 | Output on (1) / off (0) |
| `DC` | u8 | P | 108 | Protection state: 0 normal, 1 OVP, 2 OCP, 3 OPP, 4 OTP, 5 LVP |
| `DD` | u8 | P | 109 | Regulation mode: 0 CC, 1 CV |
| `DE` | ASCII | R | — | Product model string |
| `DF` | ASCII | R | — | Hardware version string (`V1.0` on a DPS-150; read at connect, never displayed) |
| `E0` | ASCII | R | — | Firmware version string |
| `E1` | u8 | R | 110 | Device address (1..255). Offset 110 confirmed on hardware (the app never reads it) |
| `E2` | f32 | P | 111 | Max voltage set-point (V). Live value that follows the input voltage minus the regulator drop: 4.73 V with a 4.93 V input |
| `E3` | f32 | P | 115 | Max current set-point (A) |
| — | f32 | P | 119…135 | Max limits for OVP, OCP, OPP, OTP, LVP (in that order) |
| `FF` | block | R | — | Read **all** (bulk dump, §4) |

Meaning of the protection thresholds comes from the UI layout in `UserPage12` (label row ↔
input box ↔ register). Strings are decoded with the Windows ANSI codepage in the app;
treat them as ASCII.

## 4. Bulk read (`F1 A1 FF 01 00 00`)

The reply's DATA (≥139 bytes) is a packed image of registers `C0`…`E3` in register order,
with the strings (`DE`…`E0`) left out, followed by the five protection maxima. The offsets
are the `Off` column above. A reply of exactly 139 bytes would have `LEN = 0x8B`.

## 5. Session sequence (as done by the app)

1. Open the COM port, then send `F1 C1 00 01 01 02` (go online).
2. Up to 2× at 1 s intervals: `F1 A1 E1 01 00 E2` (read device address).
3. When the `E1` reply equals the address the user typed (default 1), the link is
   considered good. Then the app sends:
   * `F1 B0 00 01 <baudIdx> <sum>` (set baud)
   * `F1 A1 DE 01 00 DF` (model)
   * `F1 A1 E0 01 00 E1` (firmware)
   * `F1 A1 DF 01 00 E0` (string `DF`)
   * `F1 A1 FF 01 00 00` (dump everything)
4. While connected, the device pushes measurements. Every second, the app re-requests `DE`/`E0`
   if they haven't arrived yet.
5. On close: `F1 C1 00 01 00 01` (go offline).

### Fixed frames (`Command.cs`)

| Name | Bytes | Purpose |
|---|---|---|
| CMD_1 | `F1 C1 00 01 01 02` | online |
| CMD_2 | `F1 C1 00 01 00 01` | offline |
| CMD_3 | `F1 A1 FF 01 00 00` | read all |
| CMD_4 | `F1 A1 C0 01 00 C1` | read input V (unused) |
| CMD_5 | `F1 A1 DE 01 00 DF` | read model |
| CMD_6 | `F1 A1 E0 01 00 E1` | read firmware |
| CMD_7 | `F1 A1 E1 01 00 E2` | read address |
| CMD_8 | `F1 B1 DB 01 00 DC` | output OFF |
| CMD_9 | `F1 B1 DB 01 01 DD` | output ON |
| CMD_10 | `F1 B1 D8 01 01 DA` | metering ON |
| CMD_11 | `F1 B1 D8 01 00 D9` | metering OFF |
| CMD_12 | `F1 A1 D6 01 00 D7` | read brightness (unused) |
| CMD_13 | `F1 B0 00 01 xx ..` | set baud (see bug below) |
| CMD_14 | `F1 A1 C3 01 00 C4` | read V/I/P (unused) |
| CMD_15 | `F1 C0 00 01 01 02` | enter BOOT (firmware upgrade) |
| CMD_16 | `F1 A1 DF 01 00 E0` | read `DF` string |
| CMD_17 | `F1 A1 C1 01 00 C2` | read V set (unused) |
| CMD_18 | `F1 A1 C2 01 00 C3` | read I set (unused) |

Example write: set voltage to 12.0 V → `F1 B1 C1 04 00 00 40 41 46`.

## 6. Quirks and bugs in the original app

* **Baud command checksum bug.** `CmdMoudle.setContent()` replaces the data but does not
  recompute LEN or SUM, so the app always sends `F1 B0 00 01 <idx> 01`. The checksum stays
  `01` (the value for data `00`), which is wrong for every real index 1..5. Either the
  firmware doesn't check checksums on this command, or the command silently fails. The
  Python port sends a correct checksum by default and has `legacy_baud_checksum=True` to
  reproduce the original bytes.
* The RX handler assumes each `DataReceived` chunk starts on a frame boundary and never
  checks the checksum. The Python port resyncs on either header (`0xF0`/`0xF1`). On a
  bad checksum it drops only the header byte and resyncs, since the length byte can't be
  trusted.
* Brightness writes truncate the float to a byte; metering on/off is driven by the UI
  toggle state, so `D8 01` = on.

## 7. Hardware verification status

Tested on a **DPS-150** (USB `VID:PID 2E3C:5740`, an Artery AT32 CDC device; firmware
`V1.2`, hardware `V1.0`), powered from USB 5 V with no load attached.

**Confirmed:**
* the handshake
* device→host header `0xF0` and the shared checksum formula
* the 139-byte `FF` block layout, including byte 110 = address
* the push set and its cadence
* individual reads
* writes, each read back exactly through `FF`: `C1`/`C2` set-points, all six preset pairs
  (`C5`…`D0`), all five protection thresholds (`D1`…`D5`), `D6` brightness, `D8` metering
* `DB` output on/off: at 1.0 V / 0.1 A the output read 1.000 V in CV mode, then went back
  to 0 V when switched off
* `B0` baud command (index 1) with either checksum: the link stays up
* **the device checks checksums.** A write with a wrong checksum is silently ignored
* protection code `5` = LVP, while Vin (4.93 V) was below the `D5` LVP threshold (5.0 V).
  The flag **latches**: lowering `D5` below Vin doesn't clear it, but switching the output on
  does (it then reads `0` = normal)

**Not yet verified:**
* CC mode and the `D9`/`DA` Ah/Wh counters (both need a load)
* OVP/OCP/OPP/OTP trips
* whether `B0` with a different index changes anything on a CDC link
* the meaning of `D7`
