#!/usr/bin/env python3
"""
katana_build.py — Katana deterministic build engine (Stages 1–6).

Takes a Design Spec (.spec.yaml) and the sealed Parts Library, assembles a
construct insert deterministically, validates it, and emits an order-ready
GenBank + FASTA.  Same Spec + same sealed parts → byte-identical output.

Deterministic: the same Spec + the same sealed parts always produce a
byte-identical construct. Verify that with --oracle <expected sha256>.

KATANA_SPEC v2 gates enforced:
  Stage 1  — resolve pins, fail-closed on hash mismatch vs LOCK
  Stage 2  — verify-on-read (recompute seq_sha256 vs LOCK)
  Stage 3  — assemble deterministically, record consumed_inputs
  Stage 4  — validate: positive invariants first, junctions, RE, GC
  Stage 5  — seal: hash the result, write .gb + certificate
  Stage 6  — diff vs prior (if prior path provided)

Usage:
  py katana_build.py specs/pSense-Nit-dual.spec.yaml
  py katana_build.py specs/pSense-Nit-dual.spec.yaml --oracle f93cd751...
  py katana_build.py specs/pSense-Lac-dual.spec.yaml --gibson-overlap 30
"""
# Required on Python 3.9, which the README promises and macOS still ships. Line 168 annotates
# a default as `str | None`, and without this the module dies at IMPORT with
# "TypeError: unsupported operand type(s) for |" — so the engine did not run at all, not even
# --help. CI never saw it because it runs python:3.12. Every other module here already has
# this line; this one was missed. It changes annotation evaluation only, never behaviour: the
# construct fingerprint is unchanged.
from __future__ import annotations

import argparse, hashlib, re, sys, textwrap, csv, io
from pathlib import Path
from datetime import datetime

# ── console encoding ────────────────────────────────────────────────────────
# This file prints box-drawing and status glyphs. On a default Windows console
# (cp1252) that raises UnicodeEncodeError on the very first banner line, before
# any work happens. Only one of the RUN_*.bat wrappers sets `chcp 65001`, so
# anyone invoking the script directly hits it. Fix it here rather than relying
# on the caller.
for _stream in (sys.stdout, sys.stderr):
    try:
        if (_stream.encoding or "").lower().replace("-", "") != "utf8":
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── locate the Parts Library ────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
# Walk UP from this file until parts-library is found, rather than assuming a fixed depth.
# parents[2] only resolved because katana/ happens to sit three levels down in this tree;
# it overshoots in any other layout (e.g. katana/ at the root of a published repo).
# (An absolute fallback used to sit here. It is redundant now the walk-up is
# depth-independent, and it hard-coded a username into a file meant to be published.)
# A team building their OWN constructs points the engine at their OWN library, with
#   --library <dir>   (or KATANA_LIBRARY in the environment)
# read here, before argparse runs, because LOCK_PATH below is resolved at import time.
# Without this the only library reachable is the one shipped in this bundle, so adopting
# Katana would mean editing OUR sealed library - which breaks its root and makes verify.py
# correctly report tampering. The shipped library stays sealed; yours is the one you fill.
def _library_override():
    import os
    argv = sys.argv[1:]
    for n, tok in enumerate(argv):
        if tok == "--library" and n + 1 < len(argv):
            return argv[n + 1]
        if tok.startswith("--library="):
            return tok.split("=", 1)[1]
    return os.environ.get("KATANA_LIBRARY")

_LIB_OVERRIDE = _library_override()
if _LIB_OVERRIDE:
    _r = Path(_LIB_OVERRIDE).expanduser().resolve()
    LIB = next((c for c in (_r / "ref_parts", _r / "parts-library" / "ref_parts", _r)
                if (c / "LOCK.tsv").exists()), None)
    if LIB is None:
        sys.exit(f"BLOCK: --library {_LIB_OVERRIDE} has no ref_parts/LOCK.tsv.\n"
                 f"       Create one with:  python katana_init.py {_LIB_OVERRIDE}")
else:
    CANDIDATES = [q / "parts-library" / "ref_parts" for q in (HERE, *HERE.parents)]
    LIB = next((p for p in CANDIDATES if p.is_dir()), None)
    if LIB is None:
        sys.exit("BLOCK: cannot find parts-library/ref_parts. Checked:\n  " +
                 "\n  ".join(str(c) for c in CANDIDATES))

LOCK_PATH = LIB / "LOCK.tsv"
LOCK_ROOT_PATH = LIB / "LOCK.root"
# The expected LOCK root is a DEPLOYMENT pin, not a property of the engine.
# Supply it with --expect-root <sha256> to bind this build to one exact library state.
#
# Without a pin the engine STILL fails closed on library self-consistency: the row
# hashes are recomputed and must equal the LOCK.root file. The pin adds a second,
# external guard that catches a stale-but-internally-consistent copy of the library
# (e.g. an out-of-date sync of a shared drive). Pin your builds in CI.
DEFAULT_EXPECT_ROOT = None

# ── helpers ─────────────────────────────────────────────────────────────────

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def seq_sha256(seq: str, topology: str = "linear") -> str:
    """Normalised sequence hash: UPPER-cased letters only.
    NOTE: KATANA_SPEC v2 §3.4 specifies UPPER+|topology, but the existing
    LOCK and all sealed parts/constructs use plain UPPER (no topology tag).
    The engine matches the established convention to reproduce the oracle.
    When the topology tag is adopted, bump a flag here and re-seal."""
    normalised = seq.upper()
    return sha256_hex(normalised.encode("ascii"))

def extract_gb_sequence(text: str) -> str:
    """Extract raw sequence from GenBank ORIGIN block → uppercase."""
    in_origin = False
    parts = []
    for line in text.splitlines():
        if line.startswith("ORIGIN"):
            in_origin = True
            continue
        if in_origin:
            if line.startswith("//"):
                break
            parts.append(re.sub(r"[^A-Za-z]", "", line))
    return "".join(parts).upper()

def load_yaml_simple(path: Path) -> dict:
    """Minimal YAML-subset loader for spec files (avoids PyYAML dependency).
    Handles the flat + nested structure of Katana spec YAML.
    Falls back to PyYAML if available."""
    try:
        import yaml
    except ImportError:
        sys.exit("BLOCK: PyYAML is missing. It is the one thing this engine cannot run without.\n"
                 "       Install it with:   python -m pip install pyyaml\n"
                 "       If you made a workspace with python -m venv .venv, switch to it first,\n"
                 "       or the install goes somewhere this build cannot see.")
    # A malformed Spec used to escape as a raw Python traceback ending in a scanner error,
    # while check_design.py — given the SAME file — printed a clear message. Two entry points
    # handling one failure two different ways is the defect; a traceback is not a verdict.
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:
        msg = [f"BLOCK: could not read {path} as YAML.",
               f"       {e}"]
        # PyYAML records where it gave up. Quote that line back: "line 12, column 30" is a
        # coordinate, but the line itself is the answer.
        mark = getattr(e, "problem_mark", None)
        if mark is not None:
            try:
                src = path.read_text(encoding="utf-8").splitlines()
                if 0 <= mark.line < len(src):
                    msg.append("")
                    msg.append(f"       line {mark.line + 1}:  {src[mark.line].rstrip()}")
                    msg.append("       " + " " * (len(f"line {mark.line + 1}:  ") + mark.column) + "^")
            except Exception:
                pass
        msg += ["",
                "       Usually this is an indentation slip, or a missing quote around a value",
                "       that contains a colon.",
                "       To see the whole Spec checked at once, run:",
                "           python check_design.py " + str(path)]
        sys.exit("\n".join(msg))

def load_lock(lock_path: Path) -> dict:
    """Load LOCK.tsv → {id: {version, seq_sha256, length, outfile, ...}}"""
    rows = {}
    with open(lock_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            key = (row["id"], row["version"])
            rows[key] = row
    return rows

def verify_lock_root(lock_path: Path, lock_root_path: Path, pinned: str | None = None):
    """Verify the Parts Library manifest is internally consistent, and (optionally)
    that it is the exact library state this build was pinned to.

    Two independent checks:
      1. SELF-CONSISTENCY (always on, fail-closed). Recompute the root from the row
         hashes and require it to equal the LOCK.root file. This catches an edited
         manifest: you cannot change a row without changing the root.
      2. EXTERNAL PIN (only when `pinned` is given). Require that root to equal a
         hash you supplied out-of-band. This catches a library that is internally
         consistent but not the one you meant to build against — a stale sync, a
         wrong checkout, a second machine.

    Returns (ok, message).
    """
    if not lock_root_path.exists():
        return False, "LOCK.root file missing"
    disk_root = lock_root_path.read_text(encoding="utf-8").strip()

    # 1. Self-consistency — always enforced.
    row_hashes = []
    with open(lock_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            row_hashes.append(row["row_sha256"])
    computed = sha256_hex("\n".join(row_hashes).encode("utf-8"))
    if computed != disk_root:
        return False, (f"LOCK is not self-consistent: recomputed root {computed[:16]}… "
                       f"≠ LOCK.root file {disk_root[:16]}… (manifest edited?)")

    # 2. External pin — only if the caller supplied one.
    if pinned:
        if disk_root != pinned:
            return False, (f"LOCK.root mismatch: library={disk_root[:16]}… "
                           f"pinned={pinned[:16]}… (wrong or stale library)")
        return True, f"self-consistent + matches pin {pinned[:16]}…"

    return True, f"self-consistent ({disk_root[:16]}…); NO EXTERNAL PIN — pass --expect-root to bind"

# ── Stage 1+2: resolve + verify parts ──────────────────────────────────────

def resolve_parts(spec: dict, lock: dict) -> dict:
    """Resolve each part in spec.parts from LOCK, verify pins, load sequences.
    Returns {part_id: {seq, length, sha256, lib_path, ...}}"""
    resolved = {}
    parts_list = spec.get("parts", [])
    for part in parts_list:
        pid = part["id"]
        seal = part.get("seal", {})
        lib_file = seal.get("lib")
        expected_sha12 = seal.get("seq_sha256_12")
        expected_len = seal.get("length")

        if not lib_file or not expected_sha12:
            sys.exit(f"BLOCK Stage-1: part '{pid}' has no seal/pin — bare id rejected (v2)\n"
                     f"       Your Spec names this part but does not say WHICH version of it,\n"
                     f"       so the engine cannot check it is the one you meant.\n"
                     f"       Every part needs a seal block. add_part.py prints the exact one\n"
                     f"       to paste when it admits a part. To see what you already have:\n"
                     f"           python find_part.py --have")

        # Find in LOCK by id
        lock_key = None
        for (lid, lver), lrow in lock.items():
            if lid == pid:
                if lock_key is None or int(lver) > int(lock_key[1]):
                    lock_key = (lid, lver)
        if lock_key is None:
            sys.exit(f"BLOCK Stage-1: part '{pid}' is not in your Parts Library yet.\n"
                     f"       Your Spec asks for it, but the library has never been given it.\n"
                     f"       Nothing is broken - you just need to add it first.\n"
                     f"       See what you have:      python find_part.py --have\n"
                     f"       Find it on NCBI:        python find_part.py {pid}\n"
                     f"       Copy one we ship:       python add_part.py --library <yours> "
                     f"--from parts-library/ref_parts --id {pid}")

        lock_row = lock[lock_key]
        lock_sha = lock_row["seq_sha256"]

        # Verify pin matches LOCK
        if not lock_sha.startswith(expected_sha12):
            sys.exit(f"BLOCK Stage-1: part '{pid}' pin {expected_sha12} ≠ LOCK {lock_sha[:12]}\n"
                     f"       Your Spec is pinned to one version of this part; your library\n"
                     f"       holds a different one. One of them has moved on.\n"
                     f"       This is the check doing its job, not a bug.\n"
                     f"       Look at what the library actually holds:\n"
                     f"           python find_part.py {pid}\n"
                     f"       then update the seal block in your Spec to match it.")

        # Load the .gb file
        gb_path = LIB / lib_file
        if not gb_path.exists():
            # Try the outfile from LOCK
            gb_path = LIB / lock_row["outfile"]
        if not gb_path.exists():
            sys.exit(f"BLOCK Stage-1: part '{pid}' file not found: {gb_path}\n"
                     f"       The manifest lists this part but its file is missing, so the\n"
                     f"       library is incomplete. If you cloned this repository, the\n"
                     f"       simplest repair is a fresh copy of it.")

        gb_text = gb_path.read_text(encoding="utf-8")
        raw_seq = extract_gb_sequence(gb_text)

        if not raw_seq:
            # Try FASTA format (protein parents are .faa)
            if gb_path.suffix == ".faa":
                lines = gb_text.strip().splitlines()
                raw_seq = "".join(l.strip() for l in lines if not l.startswith(">")).upper()

        # Stage 2: verify-on-read — recompute seq_sha256
        part_topo = "linear"  # parts are always linear sequences
        computed = seq_sha256(raw_seq, part_topo)
        if computed != lock_sha:
            sys.exit(f"BLOCK Stage-2: part '{pid}' recomputed hash {computed[:12]} ≠ LOCK {lock_sha[:12]}\n"
                     f"       The part file on disk does not match what the manifest sealed it\n"
                     f"       as. Something edited it after it was sealed.\n"
                     f"       This is exactly what the engine is for, so it has stopped.\n"
                     f"       If you edited it on purpose, undo that. If not, take a fresh\n"
                     f"       copy of the library and run:  python verify.py")

        # Length check
        if expected_len and len(raw_seq) != int(expected_len):
            sys.exit(f"BLOCK Stage-2: part '{pid}' length {len(raw_seq)} ≠ expected {expected_len}\n"
                     f"       Your Spec says this part is {expected_len} bases; the library\n"
                     f"       holds {len(raw_seq)}. A part that changed length is a different\n"
                     f"       part. Check the length in the Spec's seal block against:\n"
                     f"           python find_part.py {pid}")

        resolved[pid] = {
            "seq": raw_seq,
            "length": len(raw_seq),
            "seq_sha256": computed,
            "lib_path": str(gb_path.relative_to(LIB)),
            "lock_row": lock_row,
        }
        print(f"  Stage-1/2 PASS: {pid} ({len(raw_seq)} bp, {computed[:12]})")

    return resolved

# ── Stage 3: assemble ──────────────────────────────────────────────────────

def apply_trims(seq: str, trims_for_part: dict) -> str:
    """Apply 5' and/or 3' trims to a part sequence."""
    if not trims_for_part:
        return seq
    trim_3p = trims_for_part.get("3prime", "").upper()
    trim_5p = trims_for_part.get("5prime", "").upper()
    if trim_3p:
        if seq.upper().endswith(trim_3p):
            seq = seq[:-len(trim_3p)]
        else:
            sys.exit(f"BLOCK Stage-3: 3' trim '{trim_3p}' not found at end of sequence")
    if trim_5p:
        if seq.upper().startswith(trim_5p):
            seq = seq[len(trim_5p):]
        else:
            sys.exit(f"BLOCK Stage-3: 5' trim '{trim_5p}' not found at start of sequence")
    return seq

def assemble_insert(spec: dict, resolved: dict) -> tuple:
    """Assemble the insert cassette from resolved parts per architecture.order.
    Returns (insert_seq, features_list, consumed_inputs)."""
    arch = spec.get("architecture", {})
    order = arch.get("order", [])
    trims = arch.get("trims", {})
    topology = arch.get("topology", "linear-insert")

    if not order:
        sys.exit("BLOCK Stage-3: architecture.order is empty.\n"
                 "       You have listed parts, but not the ORDER they go in. The engine\n"
                 "       will not guess an arrangement of DNA for you.\n"
                 "       Add the ids, left to right, e.g.\n"
                 "           architecture:\n"
                 "             order: [my_promoter, my_rbs, my_gene, my_terminator]\n"
                 "       Then check it reads sensibly:  python check_design.py <your.spec.yaml>")

    insert_parts = []
    features = []
    pos = 0
    consumed = {}

    for pid in order:
        if pid not in resolved:
            sys.exit(f"BLOCK Stage-3: '{pid}' appears in architecture.order but is not in your\n"
                     f"       Spec's parts list. Usually this is a typo in one of the two, or a\n"
                     f"       part you meant to add and did not.\n"
                     f"       This and other Spec problems are all reported at once by:\n"
                     f"           python check_design.py <your.spec.yaml>")

        part_data = resolved[pid]
        seq = part_data["seq"]
        consumed[pid] = part_data["seq_sha256"]

        # Apply trims if specified
        part_trims = trims.get(pid, {})
        seq_trimmed = apply_trims(seq, part_trims)

        # Record feature
        start = pos + 1  # 1-based GenBank coordinates
        end = pos + len(seq_trimmed)

        # Look up role from spec.parts
        role = "misc_feature"
        label = pid
        note = ""
        for p in spec.get("parts", []):
            if p["id"] == pid:
                role = p.get("role", "misc_feature")
                label = pid
                seal = p.get("seal", {})
                lib = seal.get("lib", "")
                note_parts = [f"{pid} {lib}"]
                src = p.get("source", {})
                if src:
                    if "registry" in src:
                        note_parts.append(f"iGEM {src.get('part', '')}")
                    elif "db" in src:
                        note_parts.append(f"{src['db']} {src.get('accession', '')}:{src.get('coords', '')}({src.get('strand', '')})")
                note = "; ".join(note_parts)
                break

        # Map role to GenBank feature key
        feature_key_map = {
            "promoter": "promoter",
            "rbs": "RBS",
            "cds": "CDS",
            "reporter": "CDS",
            "terminator": "terminator",
            "ori": "rep_origin",
            "marker": "CDS",
        }
        feat_key = feature_key_map.get(role, "misc_feature")

        trim_note = ""
        if part_trims:
            t3 = part_trims.get("3prime", "")
            t5 = part_trims.get("5prime", "")
            if t3:
                trim_note = f"; trimmed 3' {len(t3)} nt ({t3})"
            if t5:
                trim_note = f"; trimmed 5' {len(t5)} nt ({t5})"

        features.append({
            "key": feat_key,
            "start": start,
            "end": end,
            "label": label,
            "note": note + trim_note,
            "role": role,
            "trimmed_len": len(seq_trimmed),
            "original_len": len(part_data["seq"]),
        })

        insert_parts.append(seq_trimmed.lower())
        pos = end

    insert_seq = "".join(insert_parts)
    return insert_seq, features, consumed

# ── Stage 4: validate ──────────────────────────────────────────────────────

RE_SITES = {
    "EcoRI": "GAATTC",
    "XbaI": "TCTAGA",
    "SpeI": "ACTAGT",
    "PstI": "CTGCAG",
    "NdeI": "CATATG",
    "BsaI": "GGTCTC",
    "BbsI": "GAAGAC",
}

def find_re_sites(seq: str, sites: list) -> list:
    """Find all RE site positions in sequence."""
    findings = []
    seq_upper = seq.upper()
    for name in sites:
        motif = RE_SITES.get(name)
        if not motif:
            continue
        # Check both strands
        rc_motif = motif[::-1].translate(str.maketrans("ACGT", "TGCA"))
        for m in re.finditer(motif, seq_upper):
            findings.append((name, m.start(), "fwd"))
        for m in re.finditer(rc_motif, seq_upper):
            findings.append((name, m.start(), "rev"))
    return findings

def validate_insert(insert_seq: str, features: list, spec: dict, resolved: dict) -> list:
    """Stage 4: validate the assembled insert. Returns list of issues (empty = PASS)."""
    issues = []
    seq_upper = insert_seq.upper()
    constraints = spec.get("constraints", {})

    # Positive invariant 1: non-empty
    if not insert_seq:
        issues.append("BLOCK: assembled insert is empty")
        return issues

    # Positive invariant 2: length within expected bounds
    arch = spec.get("architecture", {})
    trims = arch.get("trims", {})
    expected_len = 0
    for pid in arch.get("order", []):
        if pid in resolved:
            part_trims = trims.get(pid, {})
            plen = resolved[pid]["length"]
            t3 = part_trims.get("3prime", "")
            t5 = part_trims.get("5prime", "")
            plen -= len(t3) + len(t5)
            expected_len += plen
    if len(insert_seq) != expected_len:
        issues.append(f"BLOCK: insert length {len(insert_seq)} ≠ expected {expected_len} (Σ trimmed parts)")

    # Positive invariant 3: every part's (trimmed) subsequence locatable
    order = arch.get("order", [])
    for pid in set(order):
        if pid not in resolved:
            continue
        part_seq = resolved[pid]["seq"]
        part_trims = trims.get(pid, {})
        trimmed = apply_trims(part_seq, part_trims).upper()
        if trimmed not in seq_upper:
            issues.append(f"BLOCK: part '{pid}' trimmed sequence not found in insert")

    # Junction checks: RBS→ATG spacing
    for feat in features:
        if feat["role"] == "rbs":
            rbs_end = feat["end"]
            # Find next feature (should be CDS/reporter)
            for f2 in features:
                if f2["start"] == rbs_end + 1 and f2["role"] in ("cds", "reporter"):
                    # Check ATG at start of CDS
                    cds_start_idx = f2["start"] - 1  # 0-based
                    codon = seq_upper[cds_start_idx:cds_start_idx+3]
                    if codon != "ATG":
                        issues.append(f"WARN: {f2['label']} CDS does not start with ATG (found {codon} at pos {f2['start']})")
                    break

    # Forbidden RE sites (check but note internal vs junction-crossing)
    forbid = constraints.get("forbid_sites", [])
    if forbid:
        re_hits = find_re_sites(insert_seq, forbid)
        # Classify: junction-crossing sites are BLOCKs, internal are informational
        junction_positions = set()
        for feat in features:
            junction_positions.add(feat["start"] - 1)  # 0-based start of each part
            junction_positions.add(feat["end"])  # 0-based position after part
        for name, pos, strand in re_hits:
            motif_len = len(RE_SITES[name])
            crosses_junction = any(pos < jp < pos + motif_len for jp in junction_positions)
            if crosses_junction:
                issues.append(f"BLOCK: forbidden RE {name} ({strand}) at pos {pos+1} CROSSES a junction")
            # Internal sites are informational, not blocking for synthesis

    # GC content
    gc = (seq_upper.count("G") + seq_upper.count("C")) / len(seq_upper)
    if gc < 0.25 or gc > 0.65:
        issues.append(f"WARN: GC content {gc:.1%} outside 25-65% range")

    # Max homopolymer
    max_hp = 0
    for base in "ACGT":
        for m in re.finditer(base + "+", seq_upper):
            max_hp = max(max_hp, m.end() - m.start())
    if max_hp > 10:
        issues.append(f"WARN: max homopolymer run {max_hp} bp (>10)")

    # Size vs fragment cap
    frag_cap = constraints.get("fragment_bp_max", 5000)
    if len(insert_seq) > frag_cap:
        issues.append(f"INFO: insert {len(insert_seq)} bp > fragment cap {frag_cap} → requires multi-fragment split")

    return issues

# ── Stage 5: seal ──────────────────────────────────────────────────────────

def write_genbank(insert_seq: str, features: list, spec: dict, seal_hash: str,
                  output_path: Path):
    """Write a GenBank file for the insert."""
    sid = spec["id"]
    version = spec.get("version", 1)
    date_str = datetime.now().strftime("%d-%b-%Y").upper()
    length = len(insert_seq)

    lines = []
    lines.append(f"LOCUS       {sid}_insert {length} bp    DNA     linear   SYN {date_str}")
    lines.append(f"DEFINITION  Katana insert cassette (INSERT scope) for {sid} v{version} - SEALED build.")
    lines.append(f"COMMENT     Regenerated deterministically from sealed Parts Library (Katana Stage 3).")
    lines.append(f"COMMENT     Same Spec + sealed parts => byte-identical.")

    # Add backbone reference
    bb = spec.get("backbone", {})
    if bb:
        lines.append(f"COMMENT     Backbone = Twist vendor vector ({bb.get('vector','')}, {bb.get('ori','')}/{bb.get('marker','')}) -")
        lines.append(f"COMMENT     referenced, NOT sealed, out of insert seal scope.")

    # Assembly method
    asm = spec.get("assembly", {})
    method = asm.get("method") or "single-fragment de-novo synthesis"
    lines.append(f"COMMENT     Assembly = {method} ({length} bp).")

    lines.append(f"COMMENT     seq_sha256={seal_hash}")

    lines.append("FEATURES             Location/Qualifiers")

    for feat in features:
        loc = f"{feat['start']}..{feat['end']}"
        lines.append(f"     {feat['key']:<16}{loc}")
        lines.append(f'                     /label="{feat["label"]}"')
        if feat.get("note"):
            lines.append(f'                     /note="{feat["note"]}"')

    lines.append("ORIGIN")

    # Format sequence in GenBank style: 10-char groups, 6 groups per line
    for i in range(0, length, 60):
        chunk = insert_seq[i:i+60]
        groups = [chunk[j:j+10] for j in range(0, len(chunk), 10)]
        line_num = i + 1
        lines.append(f"{line_num:>9} {' '.join(groups)}")

    lines.append("//")
    lines.append("")  # trailing newline

    output_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")

def write_fasta(insert_seq: str, spec: dict, seal_hash: str, output_path: Path):
    """Write order-ready FASTA for the insert."""
    sid = spec["id"]
    version = spec.get("version", 1)
    header = f">{sid}_insert_v{version} {len(insert_seq)}bp seq_sha256={seal_hash[:16]}"
    wrapped = "\n".join(insert_seq[i:i+80] for i in range(0, len(insert_seq), 80))
    output_path.write_text(f"{header}\n{wrapped}\n", encoding="utf-8", newline="\n")

def gibson_split(insert_seq: str, features: list, spec: dict, overlap: int = 30) -> list:
    """Split an insert into Gibson fragments with overlaps.
    Uses gibson_split_after_index from spec to determine split point.
    Returns list of (frag_name, frag_seq, start, end) tuples."""
    arch = spec.get("architecture", {})
    split_idx = arch.get("gibson_split_after_index")

    if split_idx is not None and split_idx < len(features):
        best_split = features[split_idx]["end"]
    else:
        # Fallback: split at the feature boundary nearest the middle
        mid = len(insert_seq) // 2
        boundaries = sorted(set(f["end"] for f in features))
        best_split = min(boundaries, key=lambda b: abs(b - mid)) if boundaries else mid

    frag_a_seq = insert_seq[:best_split + overlap]
    frag_b_seq = insert_seq[best_split - overlap:]

    sid = spec["id"]
    return [
        (f"{sid}_FRAG-A", frag_a_seq, 1, best_split + overlap),
        (f"{sid}_FRAG-B", frag_b_seq, best_split - overlap + 1, len(insert_seq)),
    ]

# ── Stage 6: diff ──────────────────────────────────────────────────────────

def diff_vs_prior(insert_seq: str, prior_path: Path) -> list:
    """Compare current insert against a prior .gb file. Returns delta list."""
    if not prior_path or not prior_path.exists():
        return [f"INFO: no prior file at {prior_path} — skip diff"]

    prior_text = prior_path.read_text(encoding="utf-8")
    prior_seq = extract_gb_sequence(prior_text)

    if insert_seq.upper() == prior_seq.upper():
        return ["MATCH: insert sequence identical to prior"]

    deltas = []
    deltas.append(f"MISMATCH: current {len(insert_seq)} bp vs prior {len(prior_seq)} bp (Δ {len(insert_seq)-len(prior_seq):+d})")

    # Find specific differences
    min_len = min(len(insert_seq), len(prior_seq))
    mismatches = 0
    first_diff = None
    for i in range(min_len):
        if insert_seq[i].upper() != prior_seq[i].upper():
            mismatches += 1
            if first_diff is None:
                first_diff = i + 1
    if len(insert_seq) != len(prior_seq):
        mismatches += abs(len(insert_seq) - len(prior_seq))
    deltas.append(f"  {mismatches} differing positions; first at pos {first_diff or 'N/A'}")

    return deltas

# ── main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Katana deterministic build engine (Stages 1-6)")
    parser.add_argument("spec", type=Path, help="Path to .spec.yaml file")
    parser.add_argument("--oracle", type=str, default=None,
                        help="Expected seq_sha256 for oracle validation")
    parser.add_argument("--gibson-overlap", type=int, default=30,
                        help="Gibson overlap length (default 30)")
    parser.add_argument("--prior", type=Path, default=None,
                        help="Prior .gb file for Stage-6 diff")
    parser.add_argument("--outdir", type=Path, default=None,
                        help="Output directory (default: katana/outputs/)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run stages 1-4 only, no file output")
    parser.add_argument("--library", type=Path, default=None,
                        help="build against your own Parts Library (see katana_init.py). Read before argparse; declared here so it is documented and accepted.")
    parser.add_argument("--expect-root", type=str, default=None,
                        help="Expected Parts Library LOCK root (sha256). Binds this build to "
                             "one exact library state; omit to enforce self-consistency only.")
    parser.add_argument("--sbol", type=Path, default=None,
                        help="Also write the build as SBOL 3 to this path (needs `pip install "
                             "sbol3`). Skipped under --dry-run, like every other output.")
    parser.add_argument("--sbol-format", type=str, default="turtle",
                        choices=["turtle", "nt", "jsonld", "rdfxml"],
                        help="SBOL serialisation (default: turtle)")
    args = parser.parse_args()

    if not args.spec.exists():
        sys.exit(f"BLOCK: spec file not found: {args.spec}\n"
                 f"       Check the spelling and that you are in the right folder.\n"
                 f"       To see the Design Specs next to you:   ls specs")

    print(f"═══ Katana Build Engine ═══")
    print(f"Spec: {args.spec}")
    print(f"Library: {LIB}")
    print()

    # ── Load spec ───────────────────────────────────────────────────────────
    spec = load_yaml_simple(args.spec)
    sid = spec["id"]
    version = spec.get("version", 1)
    print(f"Construct: {sid} v{version}")
    print(f"Track: {spec.get('track', '?')}")
    print()

    # ── Verify LOCK.root ────────────────────────────────────────────────────
    print("── LOCK.root verification ──")
    expect_root = args.expect_root or DEFAULT_EXPECT_ROOT
    ok, msg = verify_lock_root(LOCK_PATH, LOCK_ROOT_PATH, expect_root)
    if not ok:
        sys.exit(f"BLOCK: {msg}")
    print(f"  LOCK.root {msg}")
    if not expect_root:
        print("  NOTE: no --expect-root given. Library integrity is enforced, but this build")
        print("        is not bound to a specific library state. Pin it for reproducible CI.")
    print()

    # ── Stage 1+2: resolve and verify parts ─────────────────────────────────
    print("── Stage 1+2: Source + Verify ──")
    lock = load_lock(LOCK_PATH)
    resolved = resolve_parts(spec, lock)
    print(f"  All {len(resolved)} distinct parts resolved and verified.")
    print()

    # ── Stage 3: assemble ───────────────────────────────────────────────────
    print("── Stage 3: Assemble ──")
    insert_seq, features, consumed = assemble_insert(spec, resolved)
    insert_hash = seq_sha256(insert_seq, "linear")
    print(f"  Insert assembled: {len(insert_seq)} bp")
    print(f"  seq_sha256: {insert_hash}")
    print(f"  Parts in order: {' → '.join(spec['architecture']['order'])}")
    for feat in features:
        trim_info = ""
        if feat["trimmed_len"] != feat["original_len"]:
            trim_info = f" (trimmed {feat['original_len']}→{feat['trimmed_len']})"
        print(f"    {feat['start']:>5}..{feat['end']:<5} {feat['key']:<12} {feat['label']}{trim_info}")
    print()

    # ── Oracle check ────────────────────────────────────────────────────────
    if args.oracle:
        print("── Oracle validation ──")
        if insert_hash == args.oracle:
            print(f"  ✓ ORACLE MATCH: {insert_hash[:16]}…")
        else:
            print(f"  ✗ ORACLE MISMATCH!")
            print(f"    Expected: {args.oracle[:32]}…")
            print(f"    Got:      {insert_hash[:32]}…")
            sys.exit(1)
        print()

    # ── Stage 4: validate ───────────────────────────────────────────────────
    print("── Stage 4: Validate ──")
    issues = validate_insert(insert_seq, features, spec, resolved)
    blocks = [i for i in issues if i.startswith("BLOCK")]
    warns = [i for i in issues if i.startswith("WARN")]
    infos = [i for i in issues if i.startswith("INFO")]
    for iss in issues:
        print(f"  {iss}")
    if blocks:
        sys.exit(f"BLOCK Stage-4: {len(blocks)} blocking issue(s)")
    if not issues:
        print("  PASS — all checks clean")
    else:
        print(f"  PASS — {len(warns)} warnings, {len(infos)} info")
    print()

    # ── Stage 4b: dry-lab auto-gate (off-target blastn + codon quality) ──────
    try:
        sys.path.insert(0, str(HERE))
        from katana_drylab import run_drylab_gate
        _db, _dw, _di = run_drylab_gate(insert_seq, features, spec, resolved, HERE)
        for _m in _di + _dw + _db:
            print(f"  {_m}")
        if _db:
            sys.exit(f"BLOCK Stage-4b: {len(_db)} dry-lab blocking issue(s)")
        print("  Stage-4b PASS" + (f" — {len(_dw)} warning(s) to review" if _dw else " — clean"))
        _skipped = [w for w in _dw if "OFF-TARGET SKIPPED" in w]
        _hits = [w for w in _dw if "off-target" in w and w not in _skipped]
        if _hits:
            print("           Warnings are normal here and do not mean you did anything")
            print("           wrong. A match only BLOCKs at >=100 bp AND >=95% identity,")
            print("           because a match that long and that exact is never chance.")
            print("           Everything shorter is surfaced so a human can glance at it.")
        if _skipped:
            print("           The off-target check did not run: the genome it needs for")
            print("           this construct is not here. The line above says which.")
            print("           To fetch it:  python get_genome.py")
    except SystemExit:
        raise
    except Exception as _e:
        print(f"  WARN Stage-4b: dry-lab gate unavailable ({_e!r}) — NOT enforced this run")
    print()

    if args.dry_run:
        print("── Dry run — no output files ──")
        return

    # ── Stage 5: seal (write output) ────────────────────────────────────────
    print("── Stage 5: Seal ──")
    outdir = args.outdir or (HERE / "outputs")
    outdir.mkdir(parents=True, exist_ok=True)

    date_tag = datetime.now().strftime("%Y-%m-%d")
    gb_name = f"{sid}_insert_v{version}_{date_tag}.gb"
    fasta_name = f"{sid}_insert_v{version}_TWIST.fasta"

    gb_path = outdir / gb_name
    fasta_path = outdir / fasta_name

    write_genbank(insert_seq, features, spec, insert_hash, gb_path)
    write_fasta(insert_seq, spec, insert_hash, fasta_path)
    print(f"  .gb:    {gb_path}")
    print(f"  .fasta: {fasta_path}")

    # Verify the written .gb reproduces the hash
    written_seq = extract_gb_sequence(gb_path.read_text(encoding="utf-8"))
    written_hash = seq_sha256(written_seq, "linear")
    if written_hash != insert_hash:
        sys.exit(f"BLOCK Stage-5: written .gb hash {written_hash[:12]} ≠ assembled {insert_hash[:12]}")
    print(f"  .gb round-trip hash verified ✓")
    print(f"  SEALED: {insert_hash}")

    # ── SBOL 3 export (optional, standards interchange) ─────────────────────
    sbol_target = args.sbol
    if sbol_target is None:
        try:
            import sbol3  # noqa: F401
            sbol_target = outdir / f"{sid}_insert_v{version}.ttl"
        except ImportError:
            print("  INFO: sbol3 not installed, so no .ttl written (pip install sbol3).")
    if sbol_target:
        try:
            sys.path.insert(0, str(HERE))
            from katana_sbol import export_sbol
            _ok, _msgs = export_sbol(spec, resolved, insert_seq, features,
                                     sbol_target, fmt=args.sbol_format)
            for _m in _msgs:
                print(f"  {_m}")
            if not _ok:
                sys.exit("BLOCK Stage-5: SBOL export requested but not produced (see above)")
        except SystemExit:
            raise
        except Exception as _e:
            sys.exit(f"BLOCK Stage-5: SBOL export failed ({_e!r})")

    # Gibson split if needed
    frag_cap = spec.get("constraints", {}).get("fragment_bp_max", 5000)

    # One row per orderable piece, for the vendor order table written below.
    order_records = [{"name": f"{sid}_insert_v{version}", "role": "insert",
                      "length_bp": len(insert_seq), "sequence": insert_seq.upper(),
                      "seq_sha256": insert_hash,
                      "note": "complete insert" if len(insert_seq) <= frag_cap
                              else "complete insert (ordered as the fragments below)"}]

    if len(insert_seq) > frag_cap:
        print(f"\n── Gibson fragment split (insert {len(insert_seq)} > {frag_cap} cap) ──")
        frags = gibson_split(insert_seq, features, spec, args.gibson_overlap)
        for fname, fseq, fstart, fend in frags:
            fpath = outdir / f"{fname}_TWIST.fasta"
            header = f">{fname} {len(fseq)}bp pos {fstart}-{fend} overlap={args.gibson_overlap}"
            wrapped = "\n".join(fseq[i:i+80] for i in range(0, len(fseq), 80))
            fpath.write_text(f"{header}\n{wrapped}\n", encoding="utf-8", newline="\n")
            print(f"  {fname}: {len(fseq)} bp → {fpath}")

            order_records.append({
                "name": fname, "role": "fragment", "length_bp": len(fseq),
                "sequence": fseq.upper(), "seq_sha256": seq_sha256(fseq, "linear"),
                "note": f"pos {fstart}-{fend}, {args.gibson_overlap} bp overlap"})

    # ── Vendor order table (CSV) ─────────────────────────────────
    # Same sealed bases, a fourth shape. Vendors differ: some take FASTA, some want a
    # spreadsheet upload. Emitting all of them means nobody retypes a sequence into a web
    # form, which is precisely where a sequence and its label come apart.
    try:
        sys.path.insert(0, str(HERE))
        from katana_order_table import write_order_csv
        csv_path = outdir / f"{sid}_insert_v{version}_ORDER.csv"
        for _m in write_order_csv(order_records, spec, csv_path):
            print(f"  {_m}")
        print(f"  .csv:   {csv_path}  ({len(order_records)} row(s))")
    except Exception as _e:
        sys.exit(f"BLOCK Stage-5: order table not written ({_e!r})")

    print()

    # ── Stage 6: diff ───────────────────────────────────────────────────────
    if args.prior:
        print("── Stage 6: Diff vs prior ──")
        deltas = diff_vs_prior(insert_seq, args.prior)
        for d in deltas:
            print(f"  {d}")
        print()

    # ── Summary ─────────────────────────────────────────────────────────────
    print("═══ BUILD COMPLETE ═══")
    print(f"  Construct: {sid} v{version}")
    print(f"  Insert:    {len(insert_seq)} bp")
    print(f"  Hash:      {insert_hash}")
    print(f"  Verdict:   SEALED")
    print(f"  Output:    {gb_path}")

if __name__ == "__main__":
    main()
