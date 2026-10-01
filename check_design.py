#!/usr/bin/env python3
"""
check_design.py — read your Design Spec and tell you what is wrong with it, in plain words.

Who this is for. A fourteen-year-old on an iGEM team who has been told to "make a construct" and
does not yet know that a gene needs a ribosome binding site in front of it, or that without a
terminator the cell keeps transcribing into whatever comes next. The build engine will happily
assemble a construct with those problems and seal it, because it is checking whether the DNA is
what you SAID - not whether what you said makes biological sense.

This is the other half. It reads the Spec before you build and says, for each thing it finds:
what it saw, why it matters, and what to do about it.

    python check_design.py my-project/specs/example.spec.yaml

It reports. It does not edit your Spec and it does not decide anything for you: there are real
constructs that break every rule below on purpose, and you may be building one. What it will not
let happen is you breaking a rule WITHOUT KNOWING, which is the only kind of mistake that is
actually expensive.

Exit codes: 0 nothing to say, 1 problems worth fixing, 2 the Spec could not be read at all.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# The part roles this understands. Anything else is passed over rather than guessed at.
PROMOTER, RBS, CDS, TERM = "promoter", "rbs", "cds", "terminator"

# A reporter is a protein-coding gene like any other - it needs a ribosome binding site in front
# of it. Not treating it as coding would blind this to the single most common beginner construct
# there is, which is a promoter driving GFP.
CODING = {CDS, "reporter"}

# The origin of replication and the resistance marker live on the BACKBONE - the circular carrier
# the insert is dropped into - so they are named in the Spec but deliberately absent from
# architecture.order. Flagging them as "listed but never used" is a false alarm, and false alarms
# are how a checker teaches people to ignore it.
BACKBONE = {"ori", "marker"}


def load_spec(path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        sys.exit("BLOCK: PyYAML is missing, and this reads YAML.\n"
                 "       Install it with:   python -m pip install pyyaml")
    try:
        # Read from the handle, not from a string: PyYAML names its source in the error, and
        # from a string that name is the useless "<unicode string>" rather than the file.
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception as e:
        msg = [f"BLOCK: could not read {path} as YAML.",
               f"       {e}"]
        # Quote the offending line back. PyYAML gives "line 12, column 30", which is a
        # coordinate; the line itself is the answer. katana_build.py prints the same block.
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
                "       that contains a colon."]
        sys.exit("\n".join(msg))


class Report:
    """Collects findings so they can all be shown at once.

    Showing one problem, being fixed, and then showing the next is how a beginner loses an
    afternoon. Everything that can be said is said in one pass.
    """

    def __init__(self):
        self.items: list[tuple[str, str, str, str, bool]] = []

    def add(self, level: str, what: str, why: str, fix: str, blocking: bool = False) -> None:
        self.items.append((level, what, why, fix, blocking))

    # blocking=True means katana_build.py will REFUSE this Spec, not merely disagree with it.
    # Only the seal problems are marked: a missing or placeholder seal stops the engine at
    # Stage-1, measured. A missing promoter or RBS is a design opinion and builds fine.
    def problem(self, what, why, fix, blocking=False):
        self.add("PROBLEM", what, why, fix, blocking)

    def check(self, what, why, fix):    self.add("WORTH A LOOK", what, why, fix)

    def render(self, spec_name: str) -> int:
        if not self.items:
            print()
            print(f"  {spec_name}: nothing to report.")
            print("  Every gene has a ribosome binding site, every transcription unit ends in a")
            print("  terminator, and every part you named exists. Build it when you are ready.")
            print()
            return 0

        probs = [i for i in self.items if i[0] == "PROBLEM"]
        looks = [i for i in self.items if i[0] != "PROBLEM"]
        print()
        print(f"  {spec_name}")
        print(f"  {len(probs)} problem(s), {len(looks)} worth a look.")
        print()
        for level, what, why, fix, blocking in probs + looks:
            print(f"  {level}{' (STOPS THE BUILD)' if blocking else ''}: {what}")
            print(f"      why it matters   {why}")
            print(f"      what to do       {fix}")
            print()
        # This line used to be unconditional, and it was false whenever a seal was missing:
        # katana_build.py BLOCKs at Stage-1 on exactly those, so the reader was told the
        # opposite of what would happen next. Saying "nothing here stops you" about something
        # that does is the same failure the audit had before a8d8b78.
        n_block = sum(1 for i in probs if i[4])
        if n_block:
            print(f"  {n_block} of these WILL stop the build — katana_build.py refuses a part it")
            print("  cannot pin to one exact version. The rest are what a lab-mate would say")
            print("  reading over your shoulder, and you are allowed to disagree with those.")
        else:
            print("  None of this stops you building. It is what a lab-mate would say if they read")
            print("  your design over your shoulder, and you are allowed to disagree with it.")
        print()
        return 1 if probs else 0


def role_of(p: dict) -> str:
    return str(p.get("role", "")).strip().lower()


def analyse(spec: dict, rep: Report) -> None:
    parts = spec.get("parts") or []
    arch = spec.get("architecture") or {}
    order = list(arch.get("order") or [])
    by_id = {str(p.get("id")): p for p in parts if p.get("id")}

    # ---- the Spec has to hang together before the biology is worth discussing ----
    if not parts:
        rep.problem("The Spec lists no parts at all.",
                    "There is nothing to build.",
                    "Add a parts list to the Spec. add_part.py prints the block to paste.")
        return

    for pid in order:
        if str(pid) not in by_id:
            rep.problem(f"'{pid}' is in architecture.order but not in the parts list.",
                        "The engine will stop at Stage 3. Usually a typo in one of the two.",
                        f"Either add a parts entry for '{pid}', or correct the spelling in order.")

    unused = [pid for pid in by_id
              if pid not in [str(o) for o in order]
              and role_of(by_id[pid]) not in BACKBONE]
    if unused:
        rep.check(f"Listed but never used: {', '.join(unused)}.",
                  "A part in the parts list that is missing from architecture.order is not built into "
                  "anything. Often it was meant to be in the order and was forgotten.",
                  "Add it to architecture.order, or drop it from the parts list, to keep the Spec honest.")

    dupes = {pid for pid in by_id if [str(o) for o in order].count(pid) > 1}
    if dupes:
        rep.check(f"Used more than once: {', '.join(sorted(dupes))}.",
                  "Repeating a part is legal and sometimes intended, but an exact repeat longer "
                  "than about 40 bases can recombine in the cell and is often flagged by "
                  "synthesis vendors.",
                  "If the repeat is deliberate, ignore this. If not, use a different part for "
                  "one of the two positions.")

    for pid, p in by_id.items():
        seal = p.get("seal")
        if not seal:
            # UNRESOLVED_* ids come from Kagami: a block it could not match to any reference.
            # Telling the reader to run find_part.py on one is advice that cannot work — there
            # is no public record under that name, because the name was invented to say so.
            if pid.startswith("UNRESOLVED_"):
                rep.problem(f"'{pid}' has no seal block, and no primary source either.",
                            "Kagami could not match this block to any reference part, so it was "
                            "given a placeholder name. A part with no independent source cannot "
                            "be sealed, and the engine refuses to build without a seal.",
                            "Identify what this stretch actually is, fetch it from its primary "
                            "source with add_part.py, then replace this id with the real one. "
                            "find_part.py cannot help here — there is no record under this name.",
                            blocking=True)
            else:
                rep.problem(f"'{pid}' has no seal block.",
                            "Without it the engine cannot tell which version of the part you "
                            "mean, so it refuses to build.",
                            f"Run  python find_part.py {pid}  and paste the seal block it "
                            f"prints.",
                            blocking=True)
        elif isinstance(seal, dict):
            # Look INSIDE the block. Checking only that one exists let the starter template -
            # the first Spec a beginner opens - report "nothing to report" while carrying
            # PASTE_FROM_add_part and length 0. A placeholder that passes every check turns
            # "not filled in yet" into "checked and fine", which is the worst possible reading.
            pin = str(seal.get("seq_sha256_12", "")).strip()
            lib = str(seal.get("lib", "")).strip()
            length = seal.get("length", None)
            placeholder = any("PASTE" in s.upper() or "CHANGE" in s.upper() or "TODO" in s.upper()
                              for s in (pin, lib))
            if placeholder:
                rep.problem(f"'{pid}' still has the placeholder seal block from the template.",
                            "The Spec has not been pointed at a real part yet, so the engine will "
                            "stop at Stage 1. Nothing is broken - this part just has not been "
                            "filled in.",
                            f"Add the part with add_part.py, or if you already hold it run  "
                            f"python find_part.py {pid} --seal  and paste what it "
                            f"prints.", blocking=True)
            else:
                if not (len(pin) == 12 and all(c in "0123456789abcdefABCDEF" for c in pin)):
                    rep.problem(f"'{pid}' has a seq_sha256_12 that is not a 12-character "
                                f"fingerprint: '{pin}'.",
                                "That value is how the engine confirms it loaded the part you "
                                "meant. A malformed one cannot match anything, so the build stops.",
                                f"Run  python find_part.py {pid} --seal  and copy the "
                                f"value it prints.", blocking=True)
                try:
                    n = int(length)
                except (TypeError, ValueError):
                    n = -1
                if n <= 0:
                    rep.problem(f"'{pid}' has length {length!r} in its seal block.",
                                "A part with no length is not a part. The engine checks the "
                                "sequence it loads against this number, so it must be the real "
                                "one.",
                                f"Run  python find_part.py {pid} --seal  and copy the length "
                                f"it prints.", blocking=True)
                if not lib:
                    rep.check(f"'{pid}' has no lib: filename in its seal block.",
                              "The engine can usually find the part from the manifest anyway, but "
                              "the filename is what makes the Spec readable to a human.",
                              f"Run  python find_part.py {pid} --seal  and use the full block.")
        r = role_of(p)
        known = {PROMOTER, RBS, TERM} | CODING | BACKBONE
        if not r:
            rep.check(f"'{pid}' has no role given.",
                      "Role is what lets this checker reason about your design at all. Without "
                      "it, this part is invisible to every check below.",
                      "Add  role: promoter | rbs | cds | terminator  as appropriate.")
        elif r in ("set_this", "changeme", "change_me", "todo", "xxx"):
            rep.problem(f"'{pid}' still has the placeholder role '{p.get('role')}'.",
                        "The seal block was pasted but the role was never set. An unrecognised "
                        "role is invisible to every biology check below, so this part would be "
                        "silently skipped rather than checked.",
                        "Set it to what this part DOES in your circuit: promoter, rbs, cds, "
                        "terminator or reporter.")
        elif r not in known:
            rep.problem(f"'{pid}' has an unrecognised role: '{p.get('role')}'.",
                        "Only promoter, rbs, cds, terminator, reporter, ori and marker are "
                        "understood. Anything else is skipped by every check below, so the part "
                        "goes unexamined instead of being examined and passing.",
                        "Use one of the recognised roles, or leave the part out of "
                        "architecture.order if it is not part of the construct.")

    # ---- the biology, in the order the parts actually appear ----
    seq = [(str(pid), role_of(by_id.get(str(pid), {}))) for pid in order if str(pid) in by_id]
    roles = [r for _, r in seq]

    if seq and roles[0] != PROMOTER:
        rep.problem(f"The construct starts with '{seq[0][0]}' ({roles[0] or 'no role'}), "
                    f"not a promoter.",
                    "A promoter is the switch that starts transcription. Without one at the "
                    "front, nothing downstream is read, and the construct does nothing in the "
                    "cell even though it assembles perfectly.",
                    "Put a promoter first, unless this construct is meant to be driven by one "
                    "already present in the host or on the backbone.")

    if PROMOTER not in roles:
        rep.problem("There is no promoter anywhere in the construct.",
                    "Nothing will be transcribed.",
                    "Add one. Constitutive promoters are always on; try  "
                    "python find_part.py --have  to see what you already hold.")

    for n, (pid, role) in enumerate(seq):
        if role not in CODING:
            continue
        before = roles[n - 1] if n > 0 else None
        if before != RBS:
            rep.problem(f"'{pid}' is a gene with no ribosome binding site in front of it.",
                        "The RBS is what the ribosome grabs to start making protein. Without "
                        "one, the gene is transcribed into RNA and then almost nothing is "
                        "translated, so you get no protein and a result that looks like the "
                        "gene simply does not work.",
                        f"Insert an RBS immediately before '{pid}' in architecture.order.")

    if any(r in CODING for r in roles):
        last_cds = max(i for i, r in enumerate(roles) if r in CODING)
        if TERM not in roles[last_cds:]:
            rep.problem("The last gene has no terminator after it.",
                        "A terminator tells the cell where to stop transcribing. Without one, "
                        "transcription runs on into whatever follows on the plasmid, which "
                        "wastes the cell's resources and can switch on things you did not "
                        "intend.",
                        "Add a terminator at the end of the construct.")

    for n, (pid, role) in enumerate(seq):
        if role == RBS and (n + 1 >= len(seq) or roles[n + 1] not in CODING):
            after = seq[n + 1][0] if n + 1 < len(seq) else "nothing"
            rep.check(f"'{pid}' is a ribosome binding site, but '{after}' follows it, not a gene.",
                      "An RBS exists to start translation of the gene immediately after it. On "
                      "its own it does nothing.",
                      f"Either put a gene straight after '{pid}', or remove it.")

    if roles.count(PROMOTER) > 1 and TERM not in roles:
        rep.check(f"{roles.count(PROMOTER)} promoters and no terminator between them.",
                  "Two transcription units running into each other interfere: the first reads "
                  "through into the second, and neither behaves the way it did on its own.",
                  "Put a terminator at the end of each unit, before the next promoter.")

    # ---- things a vendor or a supervisor will ask about ----
    if not spec.get("host"):
        rep.check("No host: named.",
                  "The off-target check needs to know which organism this is going into, and "
                  "it silently does not run without it.",
                  "Add  host: E_coli_MG1655  (or your chassis), then  python get_genome.py")

    cons = spec.get("constraints") or {}
    if not cons.get("host_context"):
        rep.check("No constraints.host_context set.",
                  "Same reason: this is the field the off-target scan actually reads.",
                  "Set it to the same value you gave for host.")
    if not spec.get("backbone"):
        rep.check("No backbone: named.",
                  "The backbone is the circular carrier the insert goes into. Vendors and "
                  "collaborators will ask, and it decides your antibiotic selection.",
                  "Add  backbone: { vector: ..., ori: ..., marker: ... }")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Read a Katana Design Spec and report what is wrong with the design.")
    ap.add_argument("spec", type=Path, help="path to your .spec.yaml")
    a = ap.parse_args()

    if not a.spec.exists():
        print(f"\n  BLOCK: no such file: {a.spec}")
        print("  Check the spelling, and that you are in the right folder.")
        print("  To list the Specs next to you:   ls specs\n")
        return 2

    spec = load_spec(a.spec)
    rep = Report()
    analyse(spec, rep)
    return rep.render(a.spec.name)


if __name__ == "__main__":
    sys.exit(main())
