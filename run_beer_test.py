#!/usr/bin/env python3
"""Beer Pour Testing — interactive wrapper for ./rfid_gc_live.

Runs the structured A-I x 3-setup test plan from
'Beer Pour Testing(Sheet1).csv'. Both antennas are always active, matching
the real-life dispenser configuration. For each test it:

  1. asks Setup / Scenario (menus)
  2. launches `./rfid_gc_live <power_mW>` at the right power for the scenario
  3. counts reads for a 15-second pour window
  4. computes per-antenna reads & avg RSSI, misreads, overlaps
  5. appends a row to:
       - beer_pour_results.csv          (master, all setups)
       - results/<setup_name>.csv       (one file per test setup)
       - runs/<timestamp>__setupN_...csv (one file per saved test)

The C binary and its launch/compile scripts are NOT modified. This wrapper
just spawns `./rfid_gc_live` and reads its stdout.

Run on the Raspberry Pi:
    cd ~/dual-antenna-setup
    python3 run_beer_test.py

Help:
    python3 run_beer_test.py --help
"""

from __future__ import annotations

import argparse
import csv
import os
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from statistics import mean

# ----------------------------------------------------------------------------
# Constants — the test plan from "Beer Pour Testing(Sheet1).csv"
# ----------------------------------------------------------------------------

DEFAULT_BINARY = "./rfid_gc_live"
DEFAULT_RESULTS_CSV = "beer_pour_results.csv"
DEFAULT_RUNS_DIR = "runs"          # per-test CSVs (one file per saved test)
DEFAULT_RESULTS_DIR = "results"    # one CSV per setup (appended to after each test)
POUR_WINDOW_S = 15.0               # the 8–15s beer-pour window we count reads over
EPC_LEARN_WINDOW_S = 3.0           # how long to sniff during EPC auto-learn
READY_TIMEOUT_S = 15.0             # max time to wait for the reader's "Ready." line
PASS_READ_RATE = 0.80              # expected-antenna reads must hit this fraction

# Setup index -> human label
SETUPS = [
    "Metal Casings",
    "Antennas at an Angle",
    "Flat Antennas with Separation",
]

# Scenario letter -> (cup_state, driptray_state, power_label, power_mW)
# Kept for reverse lookup so the brainstorm CSV's A–I labels still map.
# "Full" always means liquid present (cup contains beer / driptray has liquid).
SCENARIOS: dict[str, tuple[str, str, str, int]] = {
    "A": ("Full",  "Empty", "Low",    30),
    "B": ("Empty", "Full",  "Low",    30),
    "C": ("Full",  "Full",  "Low",    30),
    "D": ("Full",  "Empty", "Medium", 170),
    "E": ("Empty", "Full",  "Medium", 170),
    "F": ("Full",  "Full",  "Medium", 170),
    "G": ("Full",  "Empty", "High",   316),
    "H": ("Empty", "Full",  "High",   316),
    "I": ("Full",  "Full",  "High",   316),
}

# The three cup × driptray fill-state combinations we exercise.
# "Full" indicates liquid is present.
CUP_DRIPTRAY_COMBOS: list[tuple[str, str]] = [
    ("Full",  "Empty"),    # drip tray empty - cup full
    ("Empty", "Full"),     # cup empty       - drip tray full
    ("Full",  "Full"),     # drip tray full  - cup full
]

# Reader power levels we sweep across.
POWER_LEVELS: list[tuple[str, int]] = [
    ("Low",    30),
    ("Medium", 170),
    ("High",   316),
]


def scenario_letter_for(cup_state: str, dt_state: str, power_label: str) -> str:
    for letter, (c, d, lbl, _mw) in SCENARIOS.items():
        if c == cup_state and d == dt_state and lbl == power_label:
            return letter
    return "?"

# Both antennas are always live (production dispenser configuration).
SUB_ANT1_STATE = "ON"
SUB_ANT2_STATE = "ON"
SUB_MISREAD_RULE = "Ant1 reading Cup 2's EPC OR Ant2 reading Cup 1's EPC."

# Sub-scenario id is now driven by the cup × driptray fill state, where
# "Full" indicates liquid is present in that vessel.
#   1: drip tray empty - cup full
#   2: cup empty       - drip tray full
#   3: drip tray full  - cup full
SUB_LAYOUTS: dict[int, str] = {
    1: "Drip tray empty, cup full (liquid in cup). Cup 1 on Ant1, Cup 2 on Ant2.",
    2: "Cup empty, drip tray full (liquid in tray). Cup 1 on Ant1, Cup 2 on Ant2.",
    3: "Drip tray full, cup full (liquid in both).  Cup 1 on Ant1, Cup 2 on Ant2.",
}


def sub_id_for(cup_state: str, dt_state: str) -> int:
    """Map (cup_state, driptray_state) -> sub-scenario id (1/2/3, or 0 if unknown)."""
    if cup_state == "Full" and dt_state == "Empty":
        return 1
    if cup_state == "Empty" and dt_state == "Full":
        return 2
    if cup_state == "Full" and dt_state == "Full":
        return 3
    return 0


def sub_layout_for(cup_state: str, dt_state: str) -> str:
    return SUB_LAYOUTS.get(sub_id_for(cup_state, dt_state), "Unknown sub-scenario.")

CSV_HEADER = [
    "timestamp",
    "setup_idx", "setup_name",
    "scenario", "cup_state", "driptray_state",
    "power_label", "power_mW",
    "sub", "sub_layout",
    "window_s", "total_sweeps",
    "cup1_ant1_reads", "cup1_ant1_avg_rssi",
    "cup1_ant2_reads", "cup1_ant2_avg_rssi",
    "cup2_ant1_reads", "cup2_ant1_avg_rssi",
    "cup2_ant2_reads", "cup2_ant2_avg_rssi",
    "misreads", "overlaps",
    "interrupted",
    "verdict",
    "cup1_epc", "cup2_epc",
    "notes",
]

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

# Strip ANSI colour escape sequences like \x1b[0;33m or \x1b[0m
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# A single tag read inside a sweep: (src)(rssi) EPC
TAG_RE = re.compile(r"\((\d)\)\(\s*(-?\d+(?:\.\d+)?)\s*\)\s*([0-9A-Fa-f]+)")
# The startup banner "Power     : 30 mW (both antennas)"
BANNER_POWER_RE = re.compile(r"Power\s*:\s*(\d+)\s*mW")


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def parse_sweep_line(line: str) -> tuple[int | None, list[tuple[int, float, str]]]:
    """Return (tx_mW or None, [(src, rssi_dBm, epc), ...])."""
    clean = strip_ansi(line).strip()
    if not clean:
        return None, []
    if clean == "[]":
        return None, []
    if clean.startswith("[TX="):
        m = re.match(r"\[TX=(\d+)\s*mW\]", clean)
        tx = int(m.group(1)) if m else None
        reads = [(int(s), float(r), e) for s, r, e in TAG_RE.findall(clean)]
        return tx, reads
    return None, []


# ----------------------------------------------------------------------------
# RFID reader subprocess wrapper
# ----------------------------------------------------------------------------

class RFIDReader:
    """Spawn `./rfid_gc_live <power>` and stream its stdout via a queue."""

    def __init__(self, binary: str, power_mW: int | None = None):
        cmd = [binary] if power_mW is None else [binary, str(power_mW)]
        self.cmd = cmd
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.q: queue.Queue = queue.Queue()
        self.banner: list[str] = []
        self.banner_power_mW: int | None = None

    def __enter__(self) -> "RFIDReader":
        self.proc = subprocess.Popen(
            self.cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
        )
        self.thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.thread.start()
        self._wait_for_ready()
        return self

    def _reader_loop(self) -> None:
        assert self.proc and self.proc.stdout is not None
        for line in self.proc.stdout:
            self.q.put(line)
        self.q.put(None)  # EOF sentinel

    def _wait_for_ready(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                line = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                raise RuntimeError(
                    "rfid_gc_live exited before printing 'Ready.'. "
                    "Banner so far:\n" + "\n".join(self.banner)
                )
            stripped = strip_ansi(line).rstrip()
            self.banner.append(stripped)
            m = BANNER_POWER_RE.search(stripped)
            if m:
                self.banner_power_mW = int(m.group(1))
            if "Ready." in stripped:
                return
        raise RuntimeError(
            "Timed out waiting for 'Ready.' from rfid_gc_live. "
            "Banner so far:\n" + "\n".join(self.banner)
        )

    def collect(self, duration_s: float, allow_interrupt: bool = False,
                echo: bool = False) -> tuple[list[dict], bool]:
        """Read sweep lines for `duration_s` seconds.

        Returns (sweeps, interrupted). When `allow_interrupt` is True a
        Ctrl+C during the window is caught and the partial list is
        returned with interrupted=True; otherwise the KeyboardInterrupt
        propagates normally. When `echo` is True, every sweep line is
        also written to stdout with its original ANSI colour codes,
        mirroring what ./rfid_gc_live prints when run directly.
        """
        sweeps: list[dict] = []
        deadline = time.monotonic() + duration_s
        interrupted = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    line = self.q.get(timeout=remaining)
                except queue.Empty:
                    break
                if line is None:
                    break
                clean = strip_ansi(line).rstrip()
                is_sweep = clean == "[]" or clean.startswith("[TX=")
                if echo and is_sweep:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                if is_sweep:
                    tx, reads = parse_sweep_line(line)
                    sweeps.append({"tx": tx, "reads": reads})
        except KeyboardInterrupt:
            if not allow_interrupt:
                raise
            interrupted = True
        return sweeps, interrupted

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGINT)
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
            except Exception:
                pass
        if self.thread:
            self.thread.join(timeout=2)


# ----------------------------------------------------------------------------
# EPC auto-learn
# ----------------------------------------------------------------------------

def learn_epc(binary: str, label: str, learn_power_mW: int = 30) -> str:
    """Spawn the reader, sniff EPCs for EPC_LEARN_WINDOW_S, return dominant EPC."""
    while True:
        print(f"\n  Place ONLY {label} on the antenna tray, remove the other cup.")
        input(f"  Press Enter to sniff for {EPC_LEARN_WINDOW_S:.0f}s at {learn_power_mW} mW… ")
        with RFIDReader(binary, learn_power_mW) as r:
            print(f"  --- live reader output (sniffing {EPC_LEARN_WINDOW_S:.0f}s) ---")
            sweeps, _ = r.collect(EPC_LEARN_WINDOW_S, echo=True)
            print(f"  --- end of live output ---")
        counts: Counter[str] = Counter()
        for sw in sweeps:
            for _src, _rssi, epc in sw["reads"]:
                counts[epc] += 1
        if not counts:
            print(f"  No tags detected for {label}. Try repositioning and repeat.")
            continue
        # Show top candidates
        top = counts.most_common(3)
        print(f"  Detected EPCs (top 3): {top}")
        best_epc, best_n = top[0]
        confirm = input(
            f"  Use '{best_epc}' as {label}? [Y/n]: "
        ).strip().lower()
        if confirm in {"", "y", "yes"}:
            return best_epc


# ----------------------------------------------------------------------------
# Stats from collected sweeps
# ----------------------------------------------------------------------------

def compute_stats(
    sweeps: list[dict],
    cup1_epc: str,
    cup2_epc: str,
) -> dict:
    total = len(sweeps)
    rssi_lists: dict[tuple[int, str], list[float]] = defaultdict(list)
    overlaps = 0
    misreads = 0

    for sw in sweeps:
        srcs_per_epc: dict[str, set[int]] = defaultdict(set)
        for src, rssi, epc in sw["reads"]:
            rssi_lists[(src, epc)].append(rssi)
            srcs_per_epc[epc].add(src)

        # Overlap: at least one EPC was read by BOTH src=0 and src=1 this sweep
        if any(0 in s and 1 in s for s in srcs_per_epc.values()):
            overlaps += 1

        # Misread: an antenna read the other side's cup.
        for src, _rssi, epc in sw["reads"]:
            if src == 0 and epc != cup1_epc:
                misreads += 1
            elif src == 1 and epc != cup2_epc:
                misreads += 1

    def reads_and_avg(src: int, epc: str) -> tuple[int, float | str]:
        vals = rssi_lists.get((src, epc), [])
        return len(vals), (round(mean(vals), 2) if vals else "")

    c1a1_n, c1a1_r = reads_and_avg(0, cup1_epc)
    c1a2_n, c1a2_r = reads_and_avg(1, cup1_epc)
    c2a1_n, c2a1_r = reads_and_avg(0, cup2_epc)
    c2a2_n, c2a2_r = reads_and_avg(1, cup2_epc)

    return {
        "total_sweeps": total,
        "cup1_ant1_reads": c1a1_n, "cup1_ant1_avg_rssi": c1a1_r,
        "cup1_ant2_reads": c1a2_n, "cup1_ant2_avg_rssi": c1a2_r,
        "cup2_ant1_reads": c2a1_n, "cup2_ant1_avg_rssi": c2a1_r,
        "cup2_ant2_reads": c2a2_n, "cup2_ant2_avg_rssi": c2a2_r,
        "misreads": misreads,
        "overlaps": overlaps,
    }


def verdict(stats: dict) -> str:
    if stats["misreads"] > 0 or stats["overlaps"] > 0:
        return "FAIL"
    total = stats["total_sweeps"] or 1
    rate = (stats["cup1_ant1_reads"] + stats["cup2_ant2_reads"]) / (2 * total)
    return "PASS" if rate >= PASS_READ_RATE else "WEAK"


# ----------------------------------------------------------------------------
# UI helpers
# ----------------------------------------------------------------------------

def menu(title: str, options: list[tuple[str, str]]) -> str:
    """Show a numbered menu. options is a list of (key, label). Returns key."""
    print(f"\n{title}")
    for k, label in options:
        print(f"  {k}) {label}")
    valid = {str(k).lower() for k, _ in options}
    while True:
        choice = input("> ").strip().lower()
        if choice in valid:
            return choice
        print("  Invalid choice — try one of:", ", ".join(k for k, _ in options))


def ask_test_params() -> tuple[int, str, str, str, str, str, int]:
    setup_choice = menu(
        "Setup:",
        [(str(i + 1), name) for i, name in enumerate(SETUPS)],
    )
    setup_idx = int(setup_choice)
    setup_name = SETUPS[setup_idx - 1]

    combo_choice = menu(
        "Cup / Driptray state:",
        [(str(i + 1), f"Cup={c:<5}  Driptray={d}")
         for i, (c, d) in enumerate(CUP_DRIPTRAY_COMBOS)],
    )
    cup_state, dt_state = CUP_DRIPTRAY_COMBOS[int(combo_choice) - 1]

    power_choice = menu(
        "Reader power:",
        [(str(i + 1), f"{lbl:<6}  =  {mw} mW")
         for i, (lbl, mw) in enumerate(POWER_LEVELS)],
    )
    power_label, power_mW = POWER_LEVELS[int(power_choice) - 1]

    scenario_letter = scenario_letter_for(cup_state, dt_state, power_label)
    return setup_idx, setup_name, scenario_letter, cup_state, dt_state, power_label, power_mW


def print_summary(stats: dict, scenario: str, power_mW: int,
                  cup1_epc: str, cup2_epc: str, vrd: str,
                  interrupted: bool = False, elapsed_s: float = POUR_WINDOW_S) -> None:
    rssi = lambda v: f"{v:>7}" if v == "" else f"{v:>7.2f}" if isinstance(v, float) else f"{v:>7}"
    print()
    print("=" * 64)
    print(f"  Result — Scenario {scenario}, Power {power_mW} mW")
    if interrupted:
        print(f"  *** INTERRUPTED at {elapsed_s:.1f}s of {POUR_WINDOW_S:.0f}s ***")
    print(f"  Cup 1 EPC: {cup1_epc}")
    print(f"  Cup 2 EPC: {cup2_epc}")
    window_label = f"{elapsed_s:.1f}s (interrupted)" if interrupted else f"{POUR_WINDOW_S:.0f}s"
    print(f"  Total sweeps in {window_label} window: {stats['total_sweeps']}")
    print("-" * 64)
    print(f"             |   Ant1 (Source_0)         |   Ant2 (Source_1)")
    print(f"  -----------+---------------------------+---------------------------")
    print(f"   Cup 1     |   reads={stats['cup1_ant1_reads']:>4}  rssi={rssi(stats['cup1_ant1_avg_rssi'])}"
          f"   |   reads={stats['cup1_ant2_reads']:>4}  rssi={rssi(stats['cup1_ant2_avg_rssi'])}")
    print(f"   Cup 2     |   reads={stats['cup2_ant1_reads']:>4}  rssi={rssi(stats['cup2_ant1_avg_rssi'])}"
          f"   |   reads={stats['cup2_ant2_reads']:>4}  rssi={rssi(stats['cup2_ant2_avg_rssi'])}")
    print("-" * 64)
    print(f"  Misreads (wrong-antenna reads)       : {stats['misreads']}")
    print(f"  Overlaps (same EPC by both antennas) : {stats['overlaps']}")
    print(f"  Verdict                              : {vrd}")
    print("=" * 64)


def write_csv_row(csv_path: str, row: dict) -> None:
    parent = os.path.dirname(csv_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    new_file = not os.path.exists(csv_path)
    with open(csv_path, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(CSV_HEADER)
        w.writerow([row.get(k, "") for k in CSV_HEADER])


def per_test_filename(end_ts: datetime, setup_idx: int, scenario: str,
                      interrupted: bool) -> str:
    """Filesystem-safe filename for a single test's CSV."""
    stamp = end_ts.strftime("%Y-%m-%d_%H-%M-%S")
    tag = "_interrupted" if interrupted else ""
    return f"{stamp}__setup{setup_idx}_scenario{scenario}{tag}.csv"


def write_per_test_csv(path: str, row: dict) -> None:
    """Write a single-row CSV (header + one data row) for one test."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        w.writerow([row.get(k, "") for k in CSV_HEADER])


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------

def run_one_test(binary: str, cup1_epc: str, cup2_epc: str,
                 csv_path: str, runs_dir: str, results_dir: str) -> str:
    (setup_idx, setup_name, scenario, cup_state, dt_state,
     power_label, power_mW) = ask_test_params()
    sub_id = sub_id_for(cup_state, dt_state)
    sub_layout = sub_layout_for(cup_state, dt_state)

    print("\n" + "-" * 64)
    print("  About to run:")
    print(f"    Setup       : {setup_idx}. {setup_name}")
    print(f"    Cup/Driptray: Cup={cup_state}, Driptray={dt_state}")
    print(f"    Power       : {power_label} = {power_mW} mW   (wrapper will pass this to ./rfid_gc_live)")
    print(f"    Antennas    : Ant1={SUB_ANT1_STATE}, Ant2={SUB_ANT2_STATE}  (both always live)")
    print(f"    Sub-scenario: {sub_id} — {sub_layout}")
    print(f"    Misread =   : {SUB_MISREAD_RULE}")
    print(f"    Scenario tag: {scenario}   (matches '{scenario}' in your brainstorm CSV)")
    print("-" * 64)
    input("\n  Set up the cups/driptray physically, then press Enter to start the 15s pour window… ")

    interrupted = False
    elapsed = 0.0
    sweeps: list[dict] = []
    try:
        with RFIDReader(binary, power_mW) as r:
            if r.banner_power_mW is not None and r.banner_power_mW != power_mW:
                print(f"  WARNING: reader banner says {r.banner_power_mW} mW, "
                      f"this test expects {power_mW} mW.")
            print(f"\n  Reader ready. Counting for {POUR_WINDOW_S:.0f}s — simulate the pour now…")
            print("  (Press Ctrl+C to stop early; you'll be asked to keep or discard the partial result.)")
            print(f"  --- live reader output ---")
            t0 = time.monotonic()
            sweeps, interrupted = r.collect(POUR_WINDOW_S, allow_interrupt=True, echo=True)
            elapsed = time.monotonic() - t0
            end_ts = datetime.now()
            print(f"  --- end of live output ---")
        if interrupted:
            print(f"\n  *** INTERRUPTED *** Captured {len(sweeps)} sweeps in {elapsed:.1f}s "
                  f"(of {POUR_WINDOW_S:.0f}s planned).")
        else:
            print(f"  Captured {len(sweeps)} sweeps in {elapsed:.1f}s.")
    except Exception as e:
        print(f"\n  ERROR running reader: {e}")
        return "error"

    stats = compute_stats(sweeps, cup1_epc, cup2_epc)
    vrd = verdict(stats)
    print_summary(stats, scenario, power_mW, cup1_epc, cup2_epc, vrd,
                  interrupted=interrupted, elapsed_s=elapsed)

    if interrupted:
        prompt_text = (f"\n  Test was interrupted at {elapsed:.1f}s of {POUR_WINDOW_S:.0f}s.\n"
                       "  Keep this partial result, discard, or retry? [k]eep / [d]iscard / [r]etry: ")
    else:
        prompt_text = "  Save this row? [y]es / [n]o / [r]etry: "

    notes = input("\n  Notes (optional, press Enter to skip): ").strip()
    while True:
        choice = input(prompt_text).strip().lower()
        if interrupted and choice in {"k", "keep", "d", "discard", "r", "retry"}:
            break
        if not interrupted and choice in {"y", "n", "r", "yes", "no", "retry"}:
            break
    if choice.startswith("r"):
        return "retry"
    if choice.startswith("d") or choice.startswith("n"):
        print("  Discarded.")
        return "discarded"

    if interrupted and notes:
        notes = f"[interrupted at {elapsed:.1f}s] {notes}"
    elif interrupted:
        notes = f"[interrupted at {elapsed:.1f}s]"

    row = {
        "timestamp": end_ts.isoformat(timespec="seconds"),
        "setup_idx": setup_idx,
        "setup_name": setup_name,
        "scenario": scenario,
        "cup_state": cup_state,
        "driptray_state": dt_state,
        "power_label": power_label,
        "power_mW": power_mW,
        "sub": sub_id,
        "sub_layout": sub_layout,
        "window_s": round(elapsed, 1) if interrupted else int(POUR_WINDOW_S),
        "interrupted": "yes" if interrupted else "no",
        "verdict": vrd,
        "cup1_epc": cup1_epc,
        "cup2_epc": cup2_epc,
        "notes": notes,
        **stats,
    }
    write_csv_row(csv_path, row)
    per_test_path = os.path.join(
        runs_dir,
        per_test_filename(end_ts, setup_idx, scenario, interrupted),
    )
    write_per_test_csv(per_test_path, row)
    setup_csv_path = os.path.join(results_dir, f"{setup_name}.csv")
    write_csv_row(setup_csv_path, row)
    print(f"  Appended to : {csv_path}")
    print(f"  Per-test CSV: {per_test_path}")
    print(f"  Setup CSV   : {setup_csv_path}")
    return "saved"


def main() -> int:
    p = argparse.ArgumentParser(
        description="Beer Pour Testing — interactive wrapper for ./rfid_gc_live.",
    )
    p.add_argument("--binary", default=DEFAULT_BINARY,
                   help=f"Path to the reader binary (default: {DEFAULT_BINARY})")
    p.add_argument("--csv", default=DEFAULT_RESULTS_CSV,
                   help=f"Master append-only CSV (default: {DEFAULT_RESULTS_CSV})")
    p.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR,
                   help=f"Folder where per-test timestamped CSVs are written "
                        f"(default: {DEFAULT_RUNS_DIR}/)")
    p.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR,
                   help=f"Folder where per-setup result CSVs live; each saved "
                        f"row is appended to <results-dir>/<setup_name>.csv "
                        f"(default: {DEFAULT_RESULTS_DIR}/)")
    p.add_argument("--cup1-epc", help="Skip auto-learn: provide Cup 1 EPC directly")
    p.add_argument("--cup2-epc", help="Skip auto-learn: provide Cup 2 EPC directly")
    p.add_argument("--learn-power", type=int, default=30,
                   help="Power (mW) used during EPC auto-learn (default: 30)")
    args = p.parse_args()

    if not os.path.exists(args.binary):
        print(f"Binary not found: {args.binary}\n"
              f"Run this script from the dual-antenna-setup folder on the Pi.",
              file=sys.stderr)
        return 1

    print("=" * 64)
    print("  Beer Pour Testing — wrapper for ./rfid_gc_live")
    print("=" * 64)
    print(f"  Binary       : {args.binary}")
    print(f"  Master CSV   : {args.csv}")
    print(f"  Per-test dir : {args.runs_dir}/")
    print(f"  Per-setup dir: {args.results_dir}/")
    print(f"  Pour window  : {POUR_WINDOW_S:.0f} s   (reader loop ~5 Hz on this hardware, "
          f"so expect ~{int(POUR_WINDOW_S * 5)} sweeps)")
    print(f"  Power map    : Low=30 mW, Medium=170 mW, High=316 mW")
    print()

    try:
        if args.cup1_epc and args.cup2_epc:
            cup1_epc, cup2_epc = args.cup1_epc.upper(), args.cup2_epc.upper()
            print(f"  Using provided EPCs.\n  Cup 1: {cup1_epc}\n  Cup 2: {cup2_epc}")
        else:
            print("  === EPC auto-learn ===")
            print("  You'll place one cup at a time so the script can lock in each tag's EPC.")
            cup1_epc = learn_epc(args.binary, "Cup 1", args.learn_power)
            cup2_epc = learn_epc(args.binary, "Cup 2", args.learn_power)
            if cup1_epc == cup2_epc:
                print("\n  ERROR: Cup 1 and Cup 2 came back with the same EPC. "
                      "Aborting so you can fix the tags.", file=sys.stderr)
                return 2
            print(f"\n  Locked: Cup 1 = {cup1_epc}")
            print(f"          Cup 2 = {cup2_epc}")

        while True:
            result = run_one_test(args.binary, cup1_epc, cup2_epc,
                                  args.csv, args.runs_dir, args.results_dir)
            if result == "retry":
                continue
            again = input("\n  Run another test? [Y/n]: ").strip().lower()
            if again in {"n", "no"}:
                break

    except KeyboardInterrupt:
        print("\nInterrupted. Bye.")
        return 130

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
