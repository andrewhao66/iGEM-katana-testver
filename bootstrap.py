#!/usr/bin/env python3
"""One command to get this repository runnable, and to say what is still missing.

    python3 bootstrap.py

No arguments needed, and nothing is installed outside a virtual environment in this folder.

Why this exists. A fresh clone has no .venv, requirements.txt mixes one hard requirement
with two optional ones, and the launchers check that *a* Python exists but not which
version or whether anything is installed. So the first run failed in three different
places depending on the machine, and none of them said what to do next.

What it will not do: it never installs into a system-wide Python. requirements.txt is a set
of exact pins, and run against the system interpreter they silently downgrade packages for
every other project on the machine. The README says so; this refuses to be the tool that
does it anyway.

It ends by running verify.py, because "installed" is a claim and 8/8 SEALED is evidence.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MIN_PY = (3, 9)

# Distribution name on PyPI -> the name you import it by. Only the ones that differ need an
# entry; the fallback handles the rest.
_MODULE_OF = {
    "pyyaml": "yaml",
    "python-codon-tables": "python_codon_tables",
}


# ── pure functions (tested by test_bootstrap.py) ────────────────────────────

def python_ok(version: tuple) -> "tuple[bool, str]":
    """Is this interpreter new enough? version is a sys.version_info-like tuple."""
    floor = ".".join(str(n) for n in MIN_PY)
    got = ".".join(str(n) for n in version[:3])
    if tuple(version[:2]) >= MIN_PY:
        return True, f"Python {got}"
    return False, (
        f"Python {got} is too old — this project needs {floor} or newer.\n"
        f"  On macOS:   see the python.org line in kagami/run_kagami_gui.command\n"
        f"  On Windows: winget install --id Python.Python.3.12 -e --source winget\n"
        f"  On Linux:   your distribution's python3 package"
    )


def module_name_for(dist: str) -> str:
    """The import name for a distribution name."""
    return _MODULE_OF.get(dist.lower(), dist.lower().replace("-", "_"))


def dependency_plan(req_path: Path) -> "list[dict]":
    """Read requirements.txt and return one entry per pinned dependency.

    Parsed, never duplicated. A second copy of these pins living in this file is the same
    defect this repository exists to prevent: two places asserting a version with nothing
    making them agree. requirements.txt is the single source, including the note that
    sbol3's .post0 matters because plain 1.2.0 resolves to a yanked wheel.
    """
    plan: "list[dict]" = []
    context: "list[str]" = []
    for raw in req_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            context = []                      # a blank line ends a comment block
            continue
        if line.startswith("#"):
            context.append(line.lstrip("#").strip())
            continue
        m = re.match(r"^([A-Za-z0-9._-]+)==([^\s#]+)\s*(?:#\s*(.*))?$", line)
        if not m:
            continue
        dist, pin, note = m.group(1), m.group(2), (m.group(3) or "").strip()
        kind, _, feature = note.partition(":")
        required = kind.strip().lower() == "required"
        plan.append({
            "name": dist,
            "pin": pin,
            "module": module_name_for(dist),
            "required": required,
            "feature": feature.strip() or note,
            # The consequence of going without is written in the comment block above the
            # pin ("Without these the gate WARNS and is not enforced"), not beside it.
            "consequence": " ".join(context).strip() or note,
        })
        context = []
    return plan


def missing(plan: "list[dict]", probe=None) -> "list[dict]":
    """Entries whose module cannot be imported. probe is injected by the tests."""
    if probe is None:
        import importlib.util

        def probe(mod: str) -> bool:
            try:
                return importlib.util.find_spec(mod) is not None
            except (ImportError, ValueError):
                return False
    return [d for d in plan if not probe(d["module"])]


def required_specs(plan: "list[dict]") -> "list[str]":
    """The pip specifiers for the hard requirements only.

    Exposed on the command line as --print-required so nothing else has to hardcode a pin.
    .gitlab-ci.yml's minimal-deps job used to install the literal "PyYAML==6.0.2", so a bump
    in requirements.txt would have left CI testing the old version without saying so.
    """
    return [f"{d['name']}=={d['pin']}" for d in plan if d["required"]]


def engine_smoke_cmd(py: str, root) -> "list[str]":
    """The command that proves the build engine can be imported at all.

    Separate from the version check on purpose. katana_build.py annotated a default as
    `str | None` without `from __future__ import annotations`, so under Python 3.9 — the
    floor this project promises, and what macOS still ships — it raised TypeError at IMPORT,
    before even --help. The version number was fine; the engine did not run. Checking the
    number and reporting success would be the same mistake a8d8b78 removed from the audit:
    never report a clean verdict for a check that did not run.
    """
    return [str(py), "-c", "import sys; sys.path.insert(0, %r); import katana_build" % str(root)]


def engine_smoke(py: str, root) -> "tuple[bool, str]":
    """Run engine_smoke_cmd and report whether the engine imported, with the reason if not."""
    r = subprocess.run(engine_smoke_cmd(py, root), capture_output=True, text=True)
    if r.returncode == 0:
        return True, "katana_build imports"
    tail = (r.stderr or r.stdout or "").strip().splitlines()
    return False, (tail[-1] if tail else f"exit {r.returncode}")


def blast_hint(platform_key: str) -> str:
    """Per-platform BLAST+ guidance.

    The Windows routes are a 136 MB download and differ on whether an administrator
    password is available, so they are long and easy to mistype. They live in README.md
    under "Installing BLAST+" and are NOT restated here — that copy has already been
    corrupted once (backslash escapes eaten in 6e13f8c, restored in 77923ca), and a second
    copy would be a second thing to corrupt.
    """
    key = (platform_key or "").lower()
    if key.startswith("darwin"):
        return ("With Homebrew:  brew install blast\n"
                "Without it, see README.md -> \"Installing BLAST+\" for the .dmg route.")
    if key.startswith("linux"):
        return "sudo apt update && sudo apt install -y ncbi-blast+"
    if key.startswith("win"):
        return ("See README.md -> \"Installing BLAST+\". It has two Windows routes: the\n"
                "official installer if you have an administrator password, and a route that\n"
                "needs none — which is the one to use on a school machine.")
    return "See README.md -> \"Installing BLAST+\" for your platform."


# ── the installer itself ───────────────────────────────────────────────────

def venv_python(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin") / \
        ("python.exe" if os.name == "nt" else "python")


def run_inheriting(cmd: "list[str]") -> int:
    """Run a child that writes to our stdout, flushing ours first.

    Without the flush our prints sit in a buffer while the child writes straight through, so
    piped into a file or a CI log verify.py's 8/8 appeared ABOVE the setup header that
    introduces it. stdout is line-buffered at a terminal and block-buffered otherwise, which
    is why this only showed up when the output was redirected.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    return subprocess.run(cmd).returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="Set this repository up, then prove it works.")
    ap.add_argument("--with-optional", action="store_true",
                    help="also install the two optional packages")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the closing verify.py run")
    ap.add_argument("--print-required", action="store_true",
                    help="print the hard-requirement pip specifiers and exit "
                         "(so CI need not hardcode a pin)")
    args = ap.parse_args()

    if args.print_required:
        req = HERE / "requirements.txt"
        if not req.exists():
            print(f"Missing: {req.name}", file=sys.stderr)
            return 2
        for spec in required_specs(dependency_plan(req)):
            print(spec)
        return 0

    print("Katana — setup\n" + "─" * 42)

    ok, msg = python_ok(sys.version_info)
    print(msg if ok else msg)
    if not ok:
        return 2

    req = HERE / "requirements.txt"
    if not req.exists():
        print(f"\nMissing: {req.name}. Run this from the folder that contains it.")
        return 2
    plan = dependency_plan(req)

    venv = HERE / ".venv"
    py = venv_python(venv)
    if py.exists():
        print(f".venv     already here ({py.relative_to(HERE)})")
    else:
        print(".venv     creating…")
        if run_inheriting([sys.executable, "-m", "venv", str(venv)]) != 0 or not py.exists():
            print("\nCould not create .venv. On Debian/Ubuntu this usually means the\n"
                  "python3-venv package is not installed:  sudo apt install -y python3-venv")
            return 1

    wanted = [d for d in plan if d["required"] or args.with_optional]
    specs = [f"{d['name']}=={d['pin']}" for d in wanted]  # same shape as required_specs()
    print(f"packages  installing {len(specs)}: {', '.join(specs)}")
    # --disable-pip-version-check: a fresh venv on an older Python ships an older pip, which
    # then advises upgrading itself in the middle of our output. That advice is not wrong, but
    # it is not this script's business and it reads like a problem with the setup.
    if run_inheriting([str(py), "-m", "pip", "install", "--quiet",
                       "--disable-pip-version-check", *specs]) != 0:
        print("\npip failed. The output above says why.")
        return 1

    # Report what is still off, and what that costs. The engine warns loudly rather than
    # silently skipping these, so a user should know before a build tells them.
    gone = missing(plan, probe=lambda m: subprocess.run(
        [str(py), "-c", f"import {m}"], capture_output=True).returncode == 0)
    if gone:
        print("\nOptional features not installed:")
        for d in gone:
            print(f"  {d['name']:<22} {d['feature']}")
            print(f"  {'':<22} → {d['consequence']}")
        print("  Install them with:  python3 bootstrap.py --with-optional")
    else:
        print("packages  all three present")

    have_blast = all(shutil.which(x) for x in ("blastn", "makeblastdb"))
    print(f"\nBLAST+    {'found' if have_blast else 'NOT found'}")
    if not have_blast:
        print("  The off-target check and Kagami's search path need it. Without it the\n"
              "  gate warns and is not enforced — it never silently passes.")
        for line in blast_hint(sys.platform).splitlines():
            print(f"  {line}")

    if args.no_verify:
        print("\nSkipped verify.py (--no-verify).")
        return 0

    print("\n" + "─" * 42)
    # Two pieces of evidence, not one. verify.py proves the library is intact; it imports no
    # part of the build engine, so it would have passed on a machine where katana_build could
    # not even be imported — which is exactly what Python 3.9 did until that was fixed.
    eng_ok, eng_why = engine_smoke(py, HERE)
    print(f"engine    {'imports' if eng_ok else 'DOES NOT IMPORT'}")
    if not eng_ok:
        print(f"  {eng_why}")
        print("  The library check below may still pass — it does not touch the engine — so\n"
              "  this would otherwise have looked like a working setup. It is not one.")

    print("\nProving it works — running verify.py…\n")
    if run_inheriting([str(py), str(HERE / "verify.py")]) != 0:
        print("\nSetup finished but verify.py did not pass. Do not use the library until\n"
              "you know why; the output above says what did not match.")
        return 1
    if not eng_ok:
        print("\nThe library verifies, but the build engine does not import, so this is NOT\n"
              "ready. The reason is above.")
        return 1
    print(f"\nReady. Use {py.relative_to(HERE)} , or activate the venv.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
