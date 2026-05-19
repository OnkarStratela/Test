# Dual-Antenna RFID Live Scanner

Minimal CAEN RFID setup that continuously scans **two antennas**
(`Source_0` and `Source_1`) at a **single global TX power**. After every
sweep it prints **`[]`** while nothing is visible, then a fixed-slot
**`[(0) tag, (1) tag]`** line once tags appear.

## Output format

**Continuous stream:** one line after each full Src0→Src1 sweep (~every **`GC_SCAN_MS`** ms).

The output uses **fixed slots** so tags never shift between positions —
slot 0 is always antenna 0, slot 1 is always antenna 1.

When **no tags** — only brackets (no Tx prefix):

```
[]
[]
[]
```

Each slot is padded to a fixed visible width, so the comma and the
closing `]` never shift columns. An antenna that misses renders as pure
whitespace — its `(N)` marker is **not** printed, so an empty slot can't
be mistaken for a half-shown entry.

Each tag shows its own **RSSI in dBm** (one decimal place) in brackets
right after the antenna number, in the form `(N)(rssi) <epc>`. The
reader stores RSSI internally as tenths of dBm and the scanner divides
by 10.0 for display. The scanner prints every tag exactly as the reader
returns it for each antenna — there is no filtering or cross-read
arbitration in this version.

When **both antennas** see a tag:

```
[TX=30 mW] [(0)(-63.1) E2801160600002054E1A1234 ,   (1)(-65.0) E2801160600002054E1A5678 ]
```

When **only antenna 0** sees a tag (slot 1 is blank, columns unchanged):

```
[TX=30 mW] [(0)(-63.1) E2801160600002054E1A1234 ,                                       ]
```

When **only antenna 1** sees a tag (slot 0 is blank, columns unchanged):

```
[TX=30 mW] [                                    ,   (1)(-65.0) E2801160600002054E1A5678 ]
```

Line rate defaults to **100 ms** between sweeps (≈ 10 `[ ]`/s idle). Tune
with `GC_SCAN_MS` in `rfid_gc_live.c`.

Colours on **non-empty** lines:

- **`[TX …]`** → cyan *(omitted entirely on empty `[ ]` lines)*
- **`(antenna)`** → yellow · Src0 tag → green · Src1 tag → red

## Files

| File | Purpose |
|------|---------|
| `rfid_gc_live.c` | The scanner program |
| `compile_gc.sh`  | Compiles `rfid_gc_live.c` against the CAEN library |
| `run_gc.sh`      | Fixes USB perms, compiles, then runs the scanner |
| `run_beer_test.py` | Interactive wrapper that runs the structured test plan |
| `SRC/`           | CAEN light library sources/headers (do not modify) |
| `results/`       | One CSV per test setup; every saved test row is appended to the matching file |
| `runs/`          | One CSV per individual saved test (timestamped) |

## Test plan

The `run_beer_test.py` wrapper sweeps three test setups against nine
scenarios (A–I = three fill states × three power levels).

**Setups** (top-level test scenarios):

1. Metal Casings
2. Antennas at an Angle
3. Flat Antennas with Separation

**Sub-scenarios** (cup × driptray fill state, where *Full* means liquid is
present):

| Sub | Cup | Driptray | Description |
|-----|------|---------|-------------|
| 1   | Full  | Empty  | Drip tray empty - cup full |
| 2   | Empty | Full   | Cup empty - drip tray full |
| 3   | Full  | Full   | Drip tray full - cup full  |

Each saved test row is written to **three** places:

- `beer_pour_results.csv` — master append-only log (all setups in one file)
- `results/<setup_name>.csv` — one CSV per setup (e.g. `results/Metal Casings.csv`)
- `runs/<timestamp>__setupN_scenarioX.csv` — one CSV per individual test

## How to run (Linux)

1. Connect the CAEN reader via USB (it will appear as `/dev/ttyACM0`).
2. From this folder:

   ```bash
   chmod +x run_gc.sh
   ./run_gc.sh
   ```

   This will set `/dev/ttyACM0` permissions if needed, compile, and run
   the scanner with the **default 30 mW** on both antennas.

3. Press `Ctrl+C` to stop.

## Tuning TX power (no rebuild required)

Power is a **single global value** applied to both antennas. Pass it as a
command-line argument (in **mW**, range 1–316). After compiling once,
run the binary directly:

```bash
./rfid_gc_live              # default: 30 mW (both antennas)
./rfid_gc_live 50           # 50 mW (both antennas)
./rfid_gc_live --help       # show usage
```

### Recommended starting point for a 150 mm two-antenna setup

- Boundary at the 75 mm midpoint → target read range ≈ 70 mm per antenna.
- Start at **30 mW** (the default). If the reader rejects that value,
  try `50`. If tags are missed within 6–7 cm, raise to `60–80`.
- If the same tag flickers between `(0)` and `(1)` near the midpoint,
  drop the global power until each antenna only sees its own half.

## Other configuration

Edit the macros at the top of `rfid_gc_live.c` to change:

- `GC_PORT` — serial port (e.g. `/dev/ttyUSB0`)
- `DEFAULT_POWER_MW` — default power when no CLI args are given
- `GC_SCAN_MS` — milliseconds between scan cycles

## Troubleshooting

If the reader fails to connect:
- `sudo chmod 666 /dev/ttyACM0`
- Or add yourself to the dialout group:
  `sudo usermod -a -G dialout $USER` (then log out/in)
