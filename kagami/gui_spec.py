#!/usr/bin/env python3
"""Write a seal: block into a Design Spec, by part ID.

This is the one piece of new logic behind the forward tab. Everything else that tab does is
a shipped CLI tool run with its command shown first; this is the step no tool performed.

Why it exists. The seven shipped Specs hold 63 seal: blocks, and every one was carried there
by hand — 14 in pAP-Logic alone. add_part.py prints the exact block so that no fingerprint is
copied by hand, and the README says as much. The printing was automated. The pasting was not,
and hand-carrying a hash between two files is precisely the risk this repository exists to
remove.

Three library rules shape every function here, and none of them is a comment:

  Rule 2  A design names parts by ID. read_library() returns no bases, so nothing built from
          it can show or accept a sequence.
  Rule 3  Recompute before use, every time. verify_entry() re-reads the part file from disk
          and re-hashes it; a row in LOCK.tsv is a claim, not evidence, and a check done
          earlier in the session is not reused.
  Rule 4  On any disagreement, stop and report both readings. There is deliberately no
          argument, flag or code path here that resolves a conflict. On 2026-09-08 the
          obvious ten-second fix would have written a false claim that verified clean
          forever after.

Imports nothing third-party. kagami/ runs on a machine with nothing installed, and the YAML
re-parse that confirms the written file is still valid is skipped when PyYAML is absent
rather than being required.
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# The canonical hash and the engine's own GenBank extraction, not a fourth copy of either.
# Both modules are stdlib-only at import time (katana_build's `import yaml` is inside a
# function), so this stays safe on a bare machine. test_determinism.py section 7 asserts
# add_part.seq_sha256 and katana_build.seq_sha256 still agree.
from add_part import seq_sha256                      # noqa: E402
from katana_build import extract_gb_sequence         # noqa: E402

FIELDS = ("id", "version", "seq_sha256", "file_sha256", "length",
          "source", "date", "class", "outfile", "row_sha256")

# add_part.py:524-525 prints the block at these two indents. Matching them means the window
# and the CLI emit the same artifact rather than two dialects of it.
_KEY_INDENT = 4
_CONT_EXTRA = 10


def read_library(lock_path) -> "list[dict]":
    """Every row of LOCK.tsv, as the part picker needs it — and with no bases in it.

    The picker shows id, version, length, class and source. It cannot show a sequence,
    because a sequence never enters this structure (rule 2).
    """
    with open(lock_path, encoding="utf-8") as fh:
        lines = [l.rstrip("\n") for l in fh if l.strip()]
    if not lines:
        return []
    header = lines[0].split("\t")
    if header != list(FIELDS):
        raise ValueError(f"unexpected LOCK.tsv header: {header}")
    out = []
    for raw in lines[1:]:
        cells = raw.split("\t")
        if len(cells) != len(FIELDS):
            raise ValueError(f"LOCK.tsv row has {len(cells)} columns, expected {len(FIELDS)}")
        row = dict(zip(FIELDS, cells))
        out.append({
            "id": row["id"],
            "version": row["version"],
            "length": row["length"],
            "cls": row["class"],
            "source": row["source"],
            "date": row["date"],
            "outfile": row["outfile"],
            "seq_sha256": row["seq_sha256"],
        })
    return out


def seal_block(entry: dict, indent: int = _KEY_INDENT) -> str:
    """The two lines add_part.py prints, at the requested indent.

    Only the first twelve characters of the hash appear, which is what the Specs record and
    what the engine compares. Writing the full 64 would be a second, longer claim to keep in
    step with the first.
    """
    pad, cont = " " * indent, " " * (indent + _CONT_EXTRA)
    return (f'{pad}seal:   {{ status: SEALED, lib: "{entry["outfile"]}",\n'
            f'{cont}seq_sha256_12: {entry["seq_sha256"][:12]}, '
            f'length: {entry["length"]} }}\n')


def verify_entry(lib_dir, entry: dict) -> dict:
    """Re-read the part file and re-hash it. Rule 3, at the point of use.

    Returns both readings whether or not they agree, so a caller that stops can say which
    source said what instead of just refusing.
    """
    path = os.path.join(lib_dir, entry["outfile"])
    recorded = entry["seq_sha256"]
    if not os.path.isfile(path):
        return {"ok": False, "computed": None, "recorded": recorded,
                "detail": f"sealed file is missing from the library: {entry['outfile']}"}
    text = open(path, encoding="utf-8").read()
    seq = extract_gb_sequence(text)
    if not seq:                                  # .faa parents are FASTA, not GenBank
        seq = "".join(l.strip() for l in text.splitlines()
                      if not l.startswith(">")).upper()
    computed = seq_sha256(seq)
    return {"ok": computed == recorded, "computed": computed, "recorded": recorded,
            "length_on_disk": len(seq), "length_recorded": entry["length"]}


def _part_span(lines: "list[str]", part_id: str):
    """(start, end, key_indent) for the `- id: <part_id>` entry, or None.

    end is the index one past the part's last key line, which is where a new key goes.
    """
    marker = re.compile(r"^(\s*)-\s+id:\s*([^\s#]+)")
    start = dash_indent = None
    for i, line in enumerate(lines):
        m = marker.match(line)
        if m and m.group(2) == part_id:
            start, dash_indent = i, len(m.group(1))
            break
    if start is None:
        return None

    # Sibling keys sit one level in from the dash. Read the indent the file actually uses
    # rather than assuming two spaces.
    key_indent = dash_indent + 2
    for line in lines[start + 1:]:
        if line.strip() and not line.lstrip().startswith("#"):
            ind = len(line) - len(line.lstrip())
            if ind > dash_indent:
                key_indent = ind
            break

    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not line.strip():
            continue
        ind = len(line) - len(line.lstrip())
        if ind <= dash_indent:                   # next list item, or a new top-level key
            end = j
            break
    else:
        end = len(lines)
    # Do not swallow trailing blank lines into the part.
    while end - 1 > start and not lines[end - 1].strip():
        end -= 1
    return start, end, key_indent


def _existing_seal(lines: "list[str]", start: int, end: int):
    """The seq_sha256_12 and length already recorded for this part, if any."""
    chunk = "\n".join(lines[start:end])
    if not re.search(r"^\s*seal:", chunk, re.M):
        return None
    sha = re.search(r"seq_sha256_12:\s*([0-9a-fA-F]+)", chunk)
    length = re.search(r"length:\s*(\d+)", chunk)
    return {"seq_sha256_12": sha.group(1) if sha else None,
            "length": length.group(1) if length else None}


def insert_seal(spec_path, part_id: str, entry: dict, lib_dir) -> dict:
    """Write the seal: block for part_id into the Spec at spec_path.

    Returns {"action": "inserted" | "unchanged" | "stopped", "readings": {...},
             "message": str}. "stopped" never writes, and nothing here resolves a
    disagreement — that is rule 4, and it is the whole reason this returns readings
    rather than a repaired file.
    """
    # Rule 3 first: a Spec is never edited on the strength of a manifest row alone.
    v = verify_entry(lib_dir, entry)
    if not v["ok"]:
        return {"action": "stopped",
                "readings": {"computed": v["computed"], "recorded": v["recorded"],
                             "file": entry["outfile"]},
                "message": (
                    f"{part_id}: the part file on disk and LOCK.tsv do not agree.\n"
                    f"  the file hashes to   {str(v['computed'])[:12]}\n"
                    f"  the manifest records {str(v['recorded'])[:12]}\n"
                    f"{v.get('detail', 'Nothing was written. Two sources disagree; a person decides which is right.')}")}

    text = open(spec_path, encoding="utf-8").read()
    lines = text.split("\n")
    span = _part_span(lines, part_id)
    if span is None:
        return {"action": "stopped", "readings": {},
                "message": (f"{part_id} is not listed in {os.path.basename(spec_path)}. "
                            f"Nothing was written — add the part to parts: first, so the "
                            f"order of the construct stays something a person chose.")}
    start, end, key_indent = span

    found = _existing_seal(lines, start, end)
    if found is not None:
        same = (found["seq_sha256_12"] or "").lower() == entry["seq_sha256"][:12].lower() \
            and str(found["length"]) == str(entry["length"])
        if same:
            return {"action": "unchanged",
                    "readings": {"spec_says": found["seq_sha256_12"],
                                 "manifest_says": entry["seq_sha256"][:12]},
                    "message": f"{part_id} already carries this seal. Nothing to do."}
        return {"action": "stopped",
                "readings": {"spec_says": found["seq_sha256_12"],
                             "spec_length": found["length"],
                             "manifest_says": entry["seq_sha256"][:12],
                             "manifest_length": entry["length"]},
                "message": (
                    f"{part_id} already has a seal, and it is not this one.\n"
                    f"  the Spec says     {found['seq_sha256_12']} (length {found['length']})\n"
                    f"  the manifest says {entry['seq_sha256'][:12]} (length {entry['length']})\n"
                    f"Nothing was written. These are two different claims about the same "
                    f"part; which one is right is a question for a person, and answering it "
                    f"here would destroy the evidence that they differed.")}

    block = seal_block(entry, indent=key_indent)
    new_lines = lines[:end] + block.rstrip("\n").split("\n") + lines[end:]
    new_text = "\n".join(new_lines)

    # Confirm the result is still YAML before it replaces the file. Optional on purpose:
    # kagami/ imports nothing third-party, so on a bare machine this check is skipped rather
    # than blocking the write. The parsed object is discarded — it is never written back,
    # because a round-trip through PyYAML would discard every comment in the file.
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml is not None:
        try:
            yaml.safe_load(new_text)
        except Exception as e:
            return {"action": "stopped", "readings": {},
                    "message": (f"Inserting the block would have left {os.path.basename(spec_path)} "
                                f"unparseable, so nothing was written: {e}")}

    with open(spec_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(new_text)
    return {"action": "inserted",
            "readings": {"seq_sha256_12": entry["seq_sha256"][:12],
                         "length": entry["length"], "lib": entry["outfile"]},
            "message": (f"{part_id}: sealed as {entry['seq_sha256'][:12]} "
                        f"({entry['length']} bp) from {entry['outfile']}.")}
