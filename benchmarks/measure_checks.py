"""A/B one ESBMC check at a time on real code.

Method: generate veripp harnesses once, then run raw ESBMC on each harness
twice -- baseline, and baseline plus exactly one flag. Anything that flips
from SUCCESSFUL to not-SUCCESSFUL is attributable to that flag alone.

Isolating one flag matters: an earlier attempt put --overflow-check in the
baseline, so arithmetic-heavy functions failed for overflow reasons and the
failure was wrongly charged to the check under test.
"""
import json, re, subprocess, sys, tempfile
from pathlib import Path

sys.path.insert(0, "src")
from veripp.harness import generate, HarnessOptions, HarnessError
from veripp.cppsig import function_definitions

ESBMC = "/opt/homebrew/bin/esbmc"
SRC = Path(sys.argv[1])
INC = Path("src/veripp/include").resolve()
OUT = Path(sys.argv[2])
CHECKS = ["--struct-fields-check", "--unchecked-return-value-check", "--dead-store-check"]
# Deliberately minimal: no overflow, no bounds. Only the flag under test can
# explain a flip.
BASE = ["--unwind", "8", "--timeout", "45s", "-I", str(INC), "-D", "__ESBMC__"]

def verdict(harness, extra):
    try:
        p = subprocess.run([ESBMC, str(harness), *BASE, *extra],
                           capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    # ESBMC prints the verdict on stderr, not stdout.
    m = re.search(r"VERIFICATION (SUCCESSFUL|FAILED|UNKNOWN)", p.stdout + p.stderr)
    return m.group(1) if m else "NORESULT"

names = [n for n in function_definitions(SRC.read_text(errors="replace")) if n != "main"]
print(f"{len(names)} functions in {SRC.name}", flush=True)
rows = []
skipped = []
tmp = Path(tempfile.mkdtemp())
for i, name in enumerate(names):
    try:
        h = generate(SRC, name, HarnessOptions()).write(tmp, tag="fp")
    except Exception as e:
        skipped.append((name, type(e).__name__))
        continue
    base = verdict(h, [])
    if base != "SUCCESSFUL":
        continue                      # only clean baselines can show a flip
    row = {"function": name, "base": base}
    for c in CHECKS:
        row[c] = verdict(h, [c])
    rows.append(row)
    print(f"[{i+1}/{len(names)}] {name}: " +
          " ".join(f"{c.split('--')[1][:12]}={row[c]}" for c in CHECKS), flush=True)
OUT.write_text(json.dumps(rows, indent=2))
print(f"\nharness generation failed for {len(skipped)}")
print("\n=== flips from a clean baseline ===")
print(f"clean baselines: {len(rows)}")
for c in CHECKS:
    bad = [r["function"] for r in rows if r[c] != "SUCCESSFUL"]
    pct = 100*len(bad)/len(rows) if rows else 0
    print(f"{c:34} {len(bad):3}/{len(rows)} ({pct:.0f}%) flip  {bad[:4]}")
