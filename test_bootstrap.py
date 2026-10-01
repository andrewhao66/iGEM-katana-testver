#!/usr/bin/env python3
"""Tests for bootstrap.py — the one-command setup.

Standard library only, same shape as test_determinism.py and kagami/tests.py, because CI
runs these on a bare image and a test suite that needs installing cannot check an installer.

The pure functions are tested here. Creating a venv and running pip are thin I/O wrappers
around them and are exercised by actually running bootstrap.py on a clean clone, which is a
manual step recorded in the commit rather than something this file does.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bootstrap as B  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    mark = "ok  " if cond else "FAIL"
    print(f"  {mark} {name}" + (f"   [{detail}]" if detail and not cond else ""))


# ── the Python floor ────────────────────────────────────────────────────────
print("1. Python version floor")
ok, _ = B.python_ok((3, 9, 0))
check("3.9.0 is accepted", ok)
ok, _ = B.python_ok((3, 12, 10))
check("3.12.10 is accepted", ok)
ok, msg = B.python_ok((3, 8, 18))
check("3.8.18 is refused", not ok)
check("the refusal names the floor", "3.9" in msg, msg)
ok, msg = B.python_ok((2, 7, 18))
check("2.7 is refused", not ok)

# ── the dependency plan comes from requirements.txt, not a second copy ──────
# A hardcoded pin here would be exactly the drift this repository exists to prevent: two
# places claiming a version, with nothing making them agree. bootstrap parses the one file.
print("\n2. Dependency plan is parsed from requirements.txt")
plan = B.dependency_plan(HERE / "requirements.txt")
by_name = {d["name"]: d for d in plan}
check("three dependencies found", len(plan) == 3, f"got {len(plan)}: {sorted(by_name)}")
check("PyYAML is present", "PyYAML" in by_name, sorted(by_name))
check("PyYAML is marked required", by_name.get("PyYAML", {}).get("required") is True)
check("PyYAML pin read from the file", by_name.get("PyYAML", {}).get("pin") == "6.0.2",
      by_name.get("PyYAML", {}).get("pin"))
check("python-codon-tables is optional",
      by_name.get("python-codon-tables", {}).get("required") is False)
check("sbol3 is optional", by_name.get("sbol3", {}).get("required") is False)
check("sbol3 keeps its .post0 pin", by_name.get("sbol3", {}).get("pin") == "1.2.0.post0",
      by_name.get("sbol3", {}).get("pin"))
check("every entry names the module it imports as",
      all(d.get("module") for d in plan),
      [d.get("module") for d in plan])
check("every optional entry states a consequence",
      all(d.get("consequence") for d in plan if not d["required"]))

# ── what is missing, given a probe ──────────────────────────────────────────
print("\n3. Missing dependencies, from an injected probe")
only_yaml = B.missing(plan, probe=lambda m: m == "yaml")
check("with only yaml importable, two are missing", len(only_yaml) == 2,
      [d["name"] for d in only_yaml])
check("PyYAML is not among them", "PyYAML" not in [d["name"] for d in only_yaml])
nothing = B.missing(plan, probe=lambda m: False)
check("with nothing importable, all three are missing", len(nothing) == 3)
check("the required one is flagged",
      any(d["required"] for d in nothing))
everything = B.missing(plan, probe=lambda m: True)
check("with everything importable, none are missing", everything == [])

# ── BLAST+ guidance is per-platform and does not duplicate the README ───────
print("\n4. BLAST+ guidance")
check("macOS mentions brew", "brew" in B.blast_hint("darwin").lower())
check("linux mentions apt", "apt" in B.blast_hint("linux").lower())
# The Windows routes are a 136 MB download and differ on whether an administrator password
# is available. Restating those one-liners here would be a second copy that rots — and the
# copy in README.md has already been corrupted once (6e13f8c, restored in 77923ca). Point
# at the README instead.
_win = B.blast_hint("win32")
check("windows points at the README section", "README" in _win, _win)
check("windows does not restate the download one-liner",
      "ftp.ncbi.nlm.nih.gov" not in _win, _win)
check("an unknown platform still returns guidance", bool(B.blast_hint("freebsd13")))

# ── required pins, for anything that must not hardcode a second copy ────────
# .gitlab-ci.yml's minimal-deps job installed "PyYAML==6.0.2" as a literal, so bumping the
# pin in requirements.txt would have left CI quietly testing the old one. Same defect as two
# seq_sha256 functions with nothing asserting they agree: one source, or a guard.
print("\n5. Required pins, derived not duplicated")
specs = B.required_specs(plan)
check("exactly one required spec", len(specs) == 1, specs)
check("it is the PyYAML pin from the file", specs == ["PyYAML==6.0.2"], specs)
check("no optional package leaks in",
      not any(n in " ".join(specs) for n in ("sbol3", "codon")), specs)

# ── verdict ────────────────────────────────────────────────────────────────
n = sum(1 for _, c in results if c)
print(f"\n{n}/{len(results)} passed")
sys.exit(0 if n == len(results) else 1)
