# Design: a forward-direction tab in the Kagami window

**Date:** 2026-09-30
**Status:** approved design, not yet implemented
**Branch:** `feature/gui-forward-tab`
**Baseline commit:** `302836e`

---

## Why

Katana's reverse direction has a window. Its forward direction has none, and the
step that hurts most is one no tool performs at all.

Measured on this repository at `302836e`:

| Measurement | Value |
|---|---|
| Executable entry points | 14 |
| `--flag` options across them | 55 |
| Entry points with a GUI | 1 (`kagami/kagami_gui.py`, 510 lines) |
| Shipped Specs | 7, holding 63 parts |
| `seal:` blocks transcribed by hand | 63 |

`add_part.py` prints the exact `seal:` block to paste, and the README says this is
"so you never copy a fingerprint by hand." The printing is automated. The *pasting*
is not. Those 63 blocks were moved by a person, and `pAP-Logic.spec.yaml` alone
accounts for 14 of them.

That is the whole target of this work. It is also, by the project's own argument,
the step that most deserves a tool: the library exists because a name and its bases
drift apart silently, and hand-carrying a hash between two files is exactly that
risk performed on purpose.

## A discrepancy found while designing this — explicitly out of scope

`kg_bridge.draft_spec` emits `pin: TBD@v1@TBD` for every part. Both downstream
consumers read a different key:

```
katana_build.py:216   seal = part.get("seal", {})
check_design.py:161   seal = p.get("seal")
```

Neither reads `pin`. Measured, feeding a Kagami-drafted Spec to each:

- `check_design.py` — 4 of 4 parts reported `has no seal block`, exit **1**, closing
  line "None of this stops you building."

  **CORRECTION (2026-10-01).** An earlier revision of this document said this run exited
  **0**, and the claim was repeated in the commit messages of 59dd23f and 1d23943. It is
  wrong. `check_design.py:116` is `return 1 if probs else 0`, and measured directly it
  exits 1 on this draft and 0 on `specs/pSense-Nit.spec.yaml`. The original reading came
  from `$?` after a pipeline — `check_design.py … | head -30` reports head's status, not
  the tool's. The exit code was never the defect. The closing line is: it tells the reader
  "None of this stops you building" while the problems it has just listed are exactly the
  ones that make `katana_build.py` BLOCK at Stage-1.
- `katana_build.py --dry-run` — Stage-1 **BLOCK**,
  `part 'J23116' has no seal/pin — bare id rejected (v2)`.

Three separate observations, which should not be collapsed:

1. **The BLOCK is correct.** A drafted Spec's parts have not been through intake, and
   rule 5 forbids sealing them from the construct. Refusing to build is right.
2. **`pin:` has no consumer.** In the shipped Specs `pin:` and `seal:` coexist on the
   same part with matching hashes (`pSense-Lac-lacZ`: 9 parts, 9 seals, 9 pins;
   `J23116` carries `d2a88442f986` in both), so `pin:` is a human-readable restatement.
   A Kagami draft carries *only* `pin:`, so after intake there is no correct field to
   fill and the user must hand-author a `seal:` block.
3. **Two messages mislead.** `has no seal/pin` implies `pin` is accepted; it is not.
   And `check_design.py` advises `python find_part.py UNRESOLVED_55_708` for a block
   that by definition has no reference match, which cannot succeed.

Per rule 4 this design **reports and does not resolve** it. Changing `kg_bridge`,
`check_design` or the engine is a separate decision for a human, and is not part of
this work. The relevance here is only that it confirms where the seam is: the gap
between "intake finished" and "a correct `seal:` block exists in the Spec" is
unbridged by any current tool.

## Goals

1. A second tab in the existing Kagami window covering the forward flow.
2. One new piece of logic only: writing a correct `seal:` block into a Spec.
3. Every other step shows the exact command it will run, then runs the shipped CLI
   tool unchanged.
4. The three library rules are enforced in the UI, not merely documented.

## Non-goals

- A unified shell over all 14 tools.
- Tabs for `blast_offtarget.py`, `katana_sbol.py`, `katana_order_table.py` — these run
  after a build and are a single CLI line each.
- A full-field Spec editor. This touches the `seal:` key inside `parts:` and nothing else.
- Fixing the `pin:`/`seal:` discrepancy above.
- Automated UI tests driving tkinter widgets.

## Architecture

```
kagami/kagami_gui.py   MODIFIED  introduce ttk.Notebook; move the existing UI into tab 1
kagami/gui_forward.py  NEW       tab 2: the build flow                      (~250 lines)
kagami/gui_spec.py     NEW       read LOCK.tsv, write seal: blocks          (~120 lines)
kagami/gui_run.py      NEW       subprocess: show command, stream, cancel   (~80 lines)
```

`kagami_gui.py` currently has no `ttk.Notebook` (confirmed: 0 occurrences). Adding the
tab is therefore a structural change to a 510-line file, so its edit is deliberately
confined to three things: construct the Notebook, place the existing `App` in tab 1,
place `ForwardTab` in tab 2. None of `App`'s 20 methods (`_work`, `_drain`, `rebuild`,
`save_report`, …) change.

**Repository root.** The launcher runs `exec "$PY" kagami_gui.py` with the working
directory inside `kagami/`, while the forward tools live one level up. Root is resolved
as `Path(__file__).resolve().parent.parent` and never from the current directory.

**Interpreter.** Subprocesses are launched with `sys.executable`, so they run under the
same interpreter that is already running the GUI. Re-resolving `python` from `PATH` would
discard the launchers' validation work — on Windows, `run_kagami_gui.bat` rejects any
candidate under `WindowsApps` because the Microsoft Store placeholder `python.exe` is on
`PATH` by default and answers "installed" on a machine with no Python at all.

**Concurrency.** The existing GUI runs work on a `threading.Thread` and polls a
`queue.Queue` from `_drain`. `ForwardTab` follows that pattern with its own thread and
its own queue; no queue is shared with `App`.

**Library selection.** `ForwardTab` owns an independent library picker. No state is
shared with tab 1, keeping coupling to `App` at zero.

## `gui_spec.py` — the one piece of new logic

### Reading the library

`parts-library/.../LOCK.tsv` has the columns needed, in this order:

```
id  version  seq_sha256  file_sha256  length  source  date  class  outfile  row_sha256
```

The mapping to a `seal:` block is direct: `outfile` → `lib`, `seq_sha256[:12]` →
`seq_sha256_12`, `length` → `length`.

The part list displays **id, version, length, class, source**. It does not display
bases, and has no control that reveals them.

### Writing the block

The emitted text matches `add_part.py:524-525` character for character:

```yaml
    seal:   { status: SEALED, lib: "J23116__v1__d2a88442f986.gb",
              seq_sha256_12: d2a88442f986, length: 35 }
```

so that what the window writes and what the CLI prints are the same artifact.

### Why not PyYAML round-trip

The Specs carry load-bearing comments. `pAP-Report-dual.spec.yaml` records on its
`version:` line why the backbone moved from pSC101 to pMB1, which Twist vector that
resolved to, and the date. `yaml.safe_load` followed by `yaml.dump` discards all of it.

So insertion is **line-oriented and targeted**: locate the `- id: <pid>` entry, find the
end of that part's key block (the next line at or below the entry's indentation, or the
end of `parts:`), and insert the two `seal:` lines using the indentation already used by
that part's sibling keys.

After writing, the file is re-read with `yaml.safe_load` purely to confirm it still
parses. The parsed object is discarded and never written back.

`ruamel.yaml` would round-trip comments but is a new dependency. `requirements.txt`
lists PyYAML as the only required package, and this project spent real effort letting a
school machine run the tools without installing anything; a comment-preserving
dependency is not worth that cost.

### The three cases when a part already has a `seal:`

Decided explicitly, because this is where a GUI's instincts are wrong:

| Existing `seal:` | Action |
|---|---|
| Absent | Insert the block. |
| Present and identical to the recomputed values | No write. Report "already sealed, identical". |
| Present and different | **Stop.** Show both readings side by side. No write. |

The third row never offers to overwrite. See *Error handling*.

## Data flow — tab 2

```
1  Create a library    → katana_init.py
2  Find a part         → find_part.py <name>        candidates listed, none auto-selected
3  Admit it            → add_part.py --library …    command editable before it runs
4  Write into Spec     → gui_spec.py                ★ the only step that is not a CLI call
5  Check the design    → check_design.py <spec>
6  Build               → katana_build.py            --dry-run and real, as separate buttons
```

Step 2 deliberately does not choose for the user. `find_part.py` warns when several
strains match a common gene name, because "a part from the wrong strain survives all the
way to a synthesis order." That is a judgement. The UI orders the candidates and
presents them; the person picks.

Step 3 shows the assembled `add_part.py` command in an editable field before running it,
so a coordinate or `--strand` can be corrected in place and the command stays the thing
the README documents.

## Error handling

| Rule | How this design satisfies it |
|---|---|
| **2** — no raw-sequence field in a design | Tab 2 contains no multi-line text input. A part can only be chosen from the `LOCK.tsv` list. Tab 1's `Paste sequence…` is unchanged: it accepts a construct *to be audited*, which is legitimate. The two tabs are visually distinguished so the difference is apparent, not merely documented. |
| **3** — recompute before use, every time | Before writing a `seal:` block, `gui_spec.py` re-reads the part's `.gb` file from disk, recomputes its sha256, and compares against that part's `LOCK.tsv` row. A check performed earlier in the session is not reused. |
| **4** — on mismatch, stop and tell the human | On any disagreement the panel shows both readings, labelled which source said which. The only controls offered are **Copy readings** and **Stop**. There is no Fix, no Re-seal, no Merge, and no control that writes. |

Rule 4's row is the one most at risk of being designed away. On 2026-09-08 the obvious
ten-second fix — merging a prepared manifest row — would have written a false claim that
verified clean forever afterwards. A GUI's reflex is to put a remediation button beside
an error. This design withholds it on purpose.

**Reporting other tools' verdicts.** `check_design.py` prints `PROBLEM` lines and still
exits 0 ("None of this stops you building"). The UI surfaces both the problem count and
the exit code as the tool reported them. It does not translate either into a pass.

**Subprocess failure.** A non-zero exit shows the tool's own stderr verbatim. The UI adds
no interpretation of messages it did not generate.

**Cancel.** Cancelling terminates the child process and reports that the step was
cancelled, never that it finished.

## Testing

Following `kagami/tests.py` — standard library only, no pytest, since CI already runs
that file on a bare image.

1. **Comments survive.** Insert into a Spec containing inline comments; assert every
   comment line is byte-identical afterwards, the file still parses under
   `yaml.safe_load`, and the inserted `seal:` text equals `add_part.py`'s format exactly.
2. **Insertion position.** Assert the block lands inside the correct part when several
   parts share a role, including the last part in `parts:`.
3. **Hash mismatch writes nothing.** Point a row at a file whose bases were altered;
   assert the function returns a stop result and the Spec file's mtime and bytes are
   unchanged.
4. **Already-sealed cases.** Assert identical → no write; different → stop, no write.
5. **Root resolution.** Assert the tools resolve when launched from `kagami/` and from
   the repository root.
6. **`LOCK.tsv` parsing.** Assert a row missing a column, or a malformed header, is
   refused rather than partially read.

The tkinter widgets themselves are not automated; the logic above is reachable without
them, which is why it lives in `gui_spec.py` rather than inside the tab.

## Milestones

**M1 — `gui_spec.py` plus its tests.** No UI changes. The seal-block writer, exercised
only by `kagami/tests.py`. This is the 70% of the measured pain and carries no risk to
the existing window.

**M2 — the Notebook and `ForwardTab` shell.** Tab 1 holds the current UI unchanged; tab 2
holds step 4 wired to M1. Verifies the structural change to `kagami_gui.py` in isolation.

**M3 — the CLI-driven steps.** `gui_run.py`, then steps 1, 2, 3, 5, 6.

M1 is useful on its own: run from `kagami/tests.py`, it removes the hand-transcription
even before a tab exists.

## Verification before this is called done

- `python verify.py` — SEALED, 8/8
- `python kagami/tests.py` — all pass, including the new assertions
- `python test_determinism.py` — 11/11, construct fingerprint still `1d99b7be2c513b19`

The fingerprint is the evidence that none of this altered a built sequence. It must be
unchanged; this work touches no part file, no `LOCK.tsv`, and no build logic.

Note on `test_seal_gaps.py`: running it with no argument raises
`FileNotFoundError: …/LOCK.tsv`. This is **not** a broken test. It is a helper that takes
the library directory as `argv[1]`, and `verify.py:83` already invokes it correctly as
`run(SUITE, str(LIB))` with `LIB = parts-library/ref_parts`. Invoked that way it reports
8/8. Since CI runs `verify.py`, this suite is already covered there — an earlier note in
this document claimed it was not in CI, which was wrong; it was inferred from the absence
of the literal string `test_seal_gaps` in `.gitlab-ci.yml` without following the
indirection. Its only real defect is the misleading `else "."` default on line 7, which
makes a bare run look like library corruption. That is addressed in sub-project S0, not here.
