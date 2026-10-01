#!/usr/bin/env python3
"""Kagami, in a window. Pick a file, press Audit, read the answer.

WHY. Everything Kagami does was reachable only from a terminal, and Casper's students mostly do not
use one. A tool that needs a command line is a tool most of them will never run, which makes its
findings irrelevant no matter how good they are.

tkinter, because it is in the standard library. Kagami ships zero dependencies and that is a real
property, not a boast: it is why a student can run it on a school computer without asking anyone to
install anything. A web UI would mean a server process; a packaged app would mean a build step.
Neither is worth giving that up.

DESIGN NOTES, mostly about not lying to the reader:

The verdict is large and colour-coded, but the colour never carries the meaning alone - the word
PASS / CONDITIONAL / FAIL is always there, because roughly one in twelve men cannot reliably
separate the red from the green.

The audit runs on a worker thread. A frozen window is indistinguishable from a crashed one, and a
blastn run against 18,538 references takes a few seconds.

Missing blastn is reported as a specific, fixable condition rather than as a failure. Without it
Kagami still audits everything that does not need references, and the window says exactly that
instead of silently producing a thinner report.

A stitched input (several DNA fragments found in one spreadsheet) is announced in the window, in
the same words the CLI uses, because joining fragments in the wrong order produces a different
construct that would still audit cleanly.
"""
import os
import queue
import shutil
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import kg_audit          # noqa: E402
import kg_identify       # noqa: E402
import kg_parse          # noqa: E402
import kg_refs           # noqa: E402
import kg_rebuild        # noqa: E402

# The big verdict is coloured by KIND (PASS / REVIEW / FAIL), never by the display line, which now
# carries a count ("PASS — 2 notes"). Colour never carries the meaning alone: the word is always
# present, because roughly one in twelve men cannot separate red from green reliably.
VERDICT_COLOUR = {"PASS": "#1a7f37", "REVIEW": "#9a6700", "FAIL": "#b42318"}
STATUS_COLOUR = {"PASS": "#1a7f37", "FLAG": "#9a6700", "FAIL": "#b42318",
                 "NOTE": "#6b7280", "SKIP": "#9aa0a6"}
_ORDER = {"FAIL": 0, "FLAG": 1, "NOTE": 2, "SKIP": 3, "PASS": 4}

# Assembly-method dropdown → the enzyme context kg_audit uses. "Synthesis" means no enzyme, so a
# Type IIS / BioBrick site is only a note.
_ASSEMBLY = [
    ("Synthesis / none (Twist, IDT, GenScript)", None),
    ("Golden Gate — BsaI", "BsaI"),
    ("Golden Gate / MoClo — BsmBI", "BsmBI"),
    ("Golden Gate — SapI", "SapI"),
    ("BioBrick / RFC[10]", "BioBrick"),
]
_HOST_SKIP = "No host / skip the off-target check"
_HOST_CUSTOM = "Custom genome file…"

SEQ_TYPES = [
    ("Sequence files", "*.gb *.gbk *.genbank *.fasta *.fa *.fna *.seq *.txt *.csv *.tsv *.xlsx"),
    ("GenBank", "*.gb *.gbk *.genbank"),
    ("FASTA", "*.fasta *.fa *.fna"),
    ("Spreadsheet", "*.csv *.tsv *.xlsx"),
    ("All files", "*.*"),
]


class App:
    def __init__(self, root, container=None):
        self.root = root
        self.q = queue.Queue()
        self.result = None
        # container lets this whole panel sit inside a notebook tab. Passing nothing keeps the
        # original behaviour exactly — its own window, its own title — so running
        # kagami_gui.py directly is unchanged and none of the methods below had to move.
        if container is None:
            root.title("Kagami — sequence audit")
            root.minsize(760, 560)
            container = root

        outer = ttk.Frame(container, padding=12)
        outer.pack(fill="both", expand=True)

        # ---- input -------------------------------------------------------
        box = ttk.LabelFrame(outer, text="Sequence to check", padding=10)
        box.pack(fill="x")
        self.path = tk.StringVar()
        row = ttk.Frame(box); row.pack(fill="x")
        ttk.Entry(row, textvariable=self.path).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Choose file…", command=self.pick).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Paste sequence…", command=self.paste_dialog).pack(side="left",
                                                                                padx=(8, 0))
        ttk.Label(box, foreground="#555",
                  text="Choose a GenBank / FASTA / spreadsheet file, or paste a sequence straight "
                       "in — a bare sequence, FASTA, or GenBank all work.").pack(
            anchor="w", pady=(6, 0))

        # ---- options -----------------------------------------------------
        opt = ttk.LabelFrame(outer, text="Optional", padding=10)
        opt.pack(fill="x", pady=(10, 0))

        # Host chassis: a dropdown, because a student has no genome file to browse to. The choice
        # carries a recA status, which is what decides whether a host match is an actionable finding
        # (recA+) or just a note (recA−). "Custom genome file…" preserves the old browse behaviour.
        self._host_labels = [_HOST_SKIP] + [lbl for lbl, _ in kg_refs.HOSTS] + [_HOST_CUSTOM]
        self.host_choice = tk.StringVar(value=_HOST_SKIP)
        self.custom_host = ""            # set only when "Custom genome file…" is picked
        r1 = ttk.Frame(opt); r1.pack(fill="x")
        ttk.Label(r1, text="Host chassis", width=14).pack(side="left")
        self.host_cb = ttk.Combobox(r1, textvariable=self.host_choice, state="readonly",
                                    values=self._host_labels)
        self.host_cb.pack(side="left", fill="x", expand=True)
        self.host_cb.bind("<<ComboboxSelected>>", self._on_host_pick)
        ttk.Label(opt, foreground="#555",
                  text="Runs the off-target check against that host. recA− (a cloning strain) makes "
                       "a host match a note, not a problem.").pack(anchor="w", pady=(2, 8))

        # Assembly method: decides whether a Type IIS / BioBrick site is an actionable finding or a
        # note. Synthesis (the default) means neither is a problem.
        self._asm_labels = [lbl for lbl, _ in _ASSEMBLY]
        self.assembly_choice = tk.StringVar(value=self._asm_labels[0])
        r1b = ttk.Frame(opt); r1b.pack(fill="x")
        ttk.Label(r1b, text="Assembly", width=14).pack(side="left")
        ttk.Combobox(r1b, textvariable=self.assembly_choice, state="readonly",
                     values=self._asm_labels).pack(side="left", fill="x", expand=True)
        ttk.Label(opt, foreground="#555",
                  text="A restriction site is flagged only when the method's enzyme would cut it.").pack(
            anchor="w", pady=(2, 8))

        self.library = tk.StringVar()
        r2 = ttk.Frame(opt); r2.pack(fill="x")
        ttk.Label(r2, text="Your library", width=14).pack(side="left")
        ttk.Entry(r2, textvariable=self.library).pack(side="left", fill="x", expand=True)
        ttk.Button(r2, text="…", width=3, command=self.pick_library).pack(side="left", padx=(6, 0))
        ttk.Label(opt, foreground="#555",
                  text="A Katana parts library (the folder holding LOCK.tsv), checked "
                       "against its own hashes.").pack(anchor="w", pady=(2, 0))

        # ---- actions -----------------------------------------------------
        act = ttk.Frame(outer); act.pack(fill="x", pady=(12, 0))
        self.go = ttk.Button(act, text="Audit", command=self.run)
        self.go.pack(side="left")
        self.save = ttk.Button(act, text="Save report…", command=self.save_report,
                               state="disabled")
        self.save.pack(side="left", padx=(8, 0))
        self.rebuild_btn = ttk.Button(act, text="Rebuild clean with Katana…",
                                      command=self.rebuild, state="disabled")
        self.rebuild_btn.pack(side="left", padx=(8, 0))
        self.spin = ttk.Progressbar(act, mode="indeterminate", length=160)

        # ---- verdict -----------------------------------------------------
        self.verdict = tk.Label(outer, text="", font=("Segoe UI", 20, "bold"), anchor="w")
        self.verdict.pack(fill="x", pady=(14, 2))
        self.subtitle = ttk.Label(outer, text="", foreground="#555")
        self.subtitle.pack(fill="x")

        # ---- report ------------------------------------------------------
        self.text = tk.Text(outer, wrap="word", height=18, font=("Consolas", 10),
                            borderwidth=1, relief="solid", padx=10, pady=8)
        sb = ttk.Scrollbar(outer, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True, pady=(10, 0))
        sb.pack(side="right", fill="y", pady=(10, 0))
        for name, col in STATUS_COLOUR.items():
            self.text.tag_configure(name, foreground=col, font=("Consolas", 10, "bold"))
        self.text.tag_configure("head", font=("Segoe UI", 11, "bold"))
        self.text.tag_configure("dim", foreground="#666")
        self.text.tag_configure("fix", foreground="#0b5cad")
        self.text.configure(state="disabled")

        self._say("Choose a sequence file and press Audit.\n", "dim")
        if not (shutil.which("blastn") and shutil.which("makeblastdb")):
            self._say("\nNCBI BLAST+ was not found on this computer.\n", "FAIL")
            self._say("Kagami will still check the things that do not need references "
                      "(reading frame, GC, repeats, restriction sites), but it cannot identify "
                      "which parts are present. Install BLAST+ and reopen to get the full report.\n",
                      "dim")
        self.root.after(120, self._drain)

    # ---- pickers ---------------------------------------------------------
    def pick(self):
        p = filedialog.askopenfilename(title="Choose a sequence file", filetypes=SEQ_TYPES)
        if p:
            self.path.set(p)

    def _on_host_pick(self, _evt=None):
        # Only "Custom genome file…" needs a dialog; the named chassis resolve to bundled genomes.
        if self.host_choice.get() == _HOST_CUSTOM:
            p = filedialog.askopenfilename(title="Choose a host genome (FASTA)",
                                           filetypes=[("FASTA", "*.fna *.fasta *.fa"),
                                                      ("All files", "*.*")])
            if p:
                self.custom_host = p
            else:
                self.host_choice.set(_HOST_SKIP)     # cancelled: fall back to skip

    def _resolve_host(self):
        """Main-thread resolve of the host dropdown → (genome_path_or_None, recA_or_None)."""
        label = self.host_choice.get()
        if label == _HOST_SKIP:
            return None, None
        if label == _HOST_CUSTOM:
            return (self.custom_host or None), None          # recA unknown → assume worst (recA+)
        for lbl, meta in kg_refs.HOSTS:
            if lbl == label:
                return kg_refs.host_genome_path(meta["file"]), meta["reca"]
        return None, None

    def _resolve_assembly(self):
        for lbl, code in _ASSEMBLY:
            if lbl == self.assembly_choice.get():
                return code
        return None

    def pick_library(self):
        p = filedialog.askdirectory(title="Choose a Katana parts library (the folder with LOCK.tsv)")
        if p:
            self.library.set(p)

    def paste_dialog(self):
        """Paste a sequence instead of choosing a file. Many students have a sequence on the
        clipboard, not a file on disk. The pasted text is written to a temp file and audited by the
        same path as a chosen file; parse() dispatches on CONTENT, so a bare sequence, a FASTA, or a
        GenBank paste all work without the student having to say which it is."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Paste a sequence")
        dlg.transient(self.root)
        dlg.minsize(560, 360)
        ttk.Label(dlg, padding=(12, 12, 12, 4),
                  text="Paste a sequence, a FASTA, or a GenBank record below, then press Audit. "
                       "A plain run of A/C/G/T is fine.").pack(anchor="w")
        wrap = ttk.Frame(dlg, padding=(12, 0)); wrap.pack(fill="both", expand=True)
        txt = tk.Text(wrap, wrap="none", height=16, font=("Consolas", 10),
                      borderwidth=1, relief="solid")
        sb = ttk.Scrollbar(wrap, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        txt.focus_set()
        bar = ttk.Frame(dlg, padding=12); bar.pack(fill="x")

        def go():
            data = txt.get("1.0", "end").strip()
            if not data:
                messagebox.showinfo("Kagami", "Paste a sequence first.")
                return
            # Content-based dispatch means the extension is irrelevant; .txt is safe for all three.
            d = os.path.join(tempfile.gettempdir(), "kagami_pasted")
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, "pasted_sequence.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write(data + "\n")
            self.path.set(p)
            dlg.destroy()
            self.run()

        ttk.Button(bar, text="Audit this", command=go).pack(side="left")
        ttk.Button(bar, text="Cancel", command=dlg.destroy).pack(side="left", padx=(8, 0))

    # ---- running ---------------------------------------------------------
    def run(self):
        path = self.path.get().strip()
        if not path:
            messagebox.showinfo("Kagami", "Choose a sequence file first.")
            return
        if not os.path.isfile(path):
            messagebox.showerror("Kagami", f"No file at:\n{path}")
            return
        self.go.configure(state="disabled")
        self.save.configure(state="disabled")
        self.spin.pack(side="left", padx=(12, 0)); self.spin.start(12)
        self.verdict.configure(text="Working…", fg="#444")
        self.subtitle.configure(text="Identifying parts against the reference set.")
        self._clear()
        # Read every Tk variable HERE, on the main thread. tkinter is not thread-safe: calling
        # StringVar.get() from the worker raises "main thread is not in main loop", which the
        # worker's own except clause then reported to the user as "could not read that file".
        host, host_reca = self._resolve_host()
        assembly = self._resolve_assembly()
        lib = self.library.get().strip()
        threading.Thread(target=self._work, args=(path, host, host_reca, assembly, lib),
                         daemon=True).start()

    def _work(self, path, host, host_reca, assembly, lib):
        """Worker thread. Touches no Tk object: values come in as plain strings, results go out
        through a queue. A frozen window looks exactly like a crashed one, so this must not block
        the UI - and it must not touch the UI either."""
        try:
            import tempfile
            if lib:
                added, replaced, problems = kg_refs.add_library(lib)
                self.q.put(("lib", (added, replaced, problems)))
            record = kg_parse.parse(path)
            joined = getattr(record, "assembled_from", None)
            if joined:
                self.q.put(("joined", joined))
            ident_status = {}
            with tempfile.TemporaryDirectory() as wd:
                blocks = kg_identify.identify(record, wd, status=ident_status)
                host_seq = None
                if host and os.path.isfile(host):
                    host_seq = kg_parse.parse(host).seq
                # The startup banner already warns when BLAST+ is missing, but that scrolls away
                # and says nothing about THIS audit. Pass the status so the report itself carries
                # it, and so the verdict is held back to REVIEW rather than reading PASS.
                findings = kg_audit.audit(record, blocks, host_seq=host_seq,
                                          host_reca=host_reca, assembly=assembly,
                                          identify_status=ident_status)
            v = kg_audit.verdict(findings)
            self.q.put(("done", (record, blocks, findings, v)))
        except Exception as exc:                      # surface it, never swallow it
            self.q.put(("error", f"{type(exc).__name__}: {exc}"))

    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                getattr(self, f"_on_{kind}")(payload)
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    # ---- results ---------------------------------------------------------
    def _on_lib(self, payload):
        added, replaced, problems = payload
        self._say(f"Your library: {added} part(s) loaded"
                  + (f", {replaced} shipped reference(s) superseded" if replaced else "") + "\n")
        for p in problems:
            self._say(f"  REFUSED  {p}\n", "FAIL")

    def _on_joined(self, lens):
        self._say(f"\n{len(lens)} DNA fragments were found and joined in file order "
                  f"({', '.join(str(n) + ' bp' for n in lens)}).\n", "FLAG")
        self._say("If that is not the order they belong in, the audit below is of a construct "
                  "you did not build.\n\n", "dim")

    def _on_error(self, msg):
        self._finish()
        self.verdict.configure(text="Could not read that file", fg=VERDICT_COLOUR["FAIL"])
        self.subtitle.configure(text="")
        self._say(f"\n{msg}\n", "FAIL")

    def _on_done(self, payload):
        record, blocks, findings, v = payload
        self.result = payload
        self._finish()
        self.save.configure(state="normal")
        # Rebuild is only offered when the engine ships alongside Kagami (the bundle layout).
        self.rebuild_btn.configure(state=("normal" if kg_rebuild.find_engine(HERE) else "disabled"))
        kind = kg_audit.verdict_kind(findings)
        self.verdict.configure(text=v, fg=VERDICT_COLOUR.get(kind, "#444"))
        n_fail = kg_audit.count(findings, "FAIL")
        n_flag = kg_audit.count(findings, "FLAG")
        n_note = kg_audit.count(findings, "NOTE")
        if kind == "FAIL":
            tail = "   A hard error is present; fix it before building."
            counts = f"{n_fail} fail"
        elif kind == "REVIEW":
            tail = "   Resolve these before ordering."
            counts = f"{n_flag} to resolve" + (f", {n_note} note" if n_note else "")
        else:
            tail = ("   Clean to order; the notes are context, not problems." if n_note
                    else "   Clean to order.")
            counts = f"{n_note} note" if n_note else "clean"
        self.subtitle.configure(text=f"{record.name} · {len(record.seq)} bp · {counts}{tail}")

        self._say("\nWhat is in this sequence\n", "head")
        for b in blocks:
            # Prefer the note: "unannotated CDS-like ORF (119 aa)" tells a student
            # something, where the bare word "unidentified" tells them nothing.
            name = b.ident_id or b.claim_label or (b.note or "unidentified")
            role = b.ident_role or b.claim_role or "-"
            conf = (f"  [{b.pident}% id, {int((b.coverage or 0) * 100)}% cov]"
                    if b.pident is not None else "")
            alts = getattr(b, "alternatives", None) or []
            extra = f"  (= {len(alts)} other reference{'s' if len(alts) != 1 else ''})" if alts else ""
            self._say(f"  {b.start:>6}-{b.end:<6}  {role:<11} {name}{conf}{extra}\n")
            if b.note and name != b.note:
                self._say(f"          {b.note}\n", "dim")

        self._say("\nFindings\n", "head")
        for f in sorted(findings, key=lambda f: _ORDER.get(f.status, 9)):
            self._say(f"  [{f.status}] ", f.status)
            self._say(f"{f.category:<16} {f.summary}\n")
            if f.detail:
                self._say(f"          {f.detail}\n", "dim")
            if f.fix and f.status not in ("PASS", "SKIP"):
                self._say(f"          fix: {f.fix}\n", "fix")

        self._say("\nKagami reports; it does not seal. To rebuild cleanly, hand the draft Spec to "
                  "Katana.\n", "dim")

    def _finish(self):
        self.spin.stop(); self.spin.pack_forget()
        self.go.configure(state="normal")

    def save_report(self):
        if not self.result:
            return
        record, blocks, findings, v = self.result
        stem = os.path.splitext(os.path.basename(self.path.get()))[0]
        p = filedialog.asksaveasfilename(
            title="Save the report", defaultextension=".html",
            initialfile=f"{stem}_kagami_report.html",
            filetypes=[("HTML report", "*.html")])
        if not p:
            return
        import kagami as cli
        cli.write_html(p, record, blocks, findings, v)
        messagebox.showinfo("Kagami", f"Report saved:\n{p}")

    # ---- rebuild ---------------------------------------------------------
    def rebuild(self):
        """Seal the identified parts from their primary source and rebuild a clean construct via
        forward Katana. Runs on a worker thread; the seal never comes from the audited bytes."""
        if not self.result:
            return
        record, blocks, findings, v = self.result
        engine = kg_rebuild.find_engine(HERE)
        if not engine:
            messagebox.showerror("Kagami", "Rebuild needs the Katana engine bundled beside Kagami. "
                                 "Run this from the unzipped katana bundle.")
            return
        # library read HERE, on the main thread (tkinter is not thread-safe). Reuse the library
        # field if given, else a fresh library beside the audited file.
        lib = self.library.get().strip()
        if not lib:
            base = os.path.splitext(os.path.basename(self.path.get()))[0] or "construct"
            lib = os.path.join(os.path.dirname(os.path.abspath(self.path.get() or ".")),
                               base + "_katana", "parts-library")
        self.rebuild_btn.configure(state="disabled")
        self.go.configure(state="disabled")
        self.save.configure(state="disabled")
        self.spin.pack(side="left", padx=(12, 0)); self.spin.start(12)
        self._say("\nRebuilding with Katana\n", "head")
        threading.Thread(target=self._rebuild_work,
                         args=(record, blocks, lib, engine), daemon=True).start()

    def _rebuild_work(self, record, blocks, lib, engine):
        """Worker thread. Touches no Tk object: progress and the result go out through the queue."""
        try:
            res = kg_rebuild.rebuild(record, blocks, library=lib, engine=engine,
                                     progress=lambda m: self.q.put(("rbstep", m)))
            self.q.put(("rbdone", res))
        except Exception as exc:
            self.q.put(("rbdone", {"status": "seal-failed",
                                   "detail": f"{type(exc).__name__}: {exc}"}))

    def _on_rbstep(self, msg):
        self._say(f"  {msg}\n", "dim")

    def _on_rbdone(self, res):
        self._finish()
        self.rebuild_btn.configure(state="normal")
        self.save.configure(state="normal")
        st = res.get("status")
        if st == "rebuilt":
            self._say("\nREBUILT — clean, sealed, order-ready.\n", "PASS")
            if res.get("gb"):
                self._say(f"  construct: {res['gb']}\n")
            for o in res.get("outputs", []):
                if o != res.get("gb"):
                    self._say(f"  output:    {o}\n")
            self._say(f"  spec:      {res.get('spec', '')}\n", "dim")
            self._say("  Every base traces to a part sealed from its own primary source, not the "
                      "sequence you audited.\n", "dim")
        elif st == "blocked-unresolved":
            self._say("\nSTOPPED — some parts have no independent primary source to seal from:\n",
                      "FLAG")
            for b in res.get("blockers", []):
                self._say(f"  [{b['id']}] {b['reason']}\n", "dim")
        else:
            self._say(f"\nSTOPPED — {st}.\n", "FAIL")
            if res.get("detail"):
                self._say(res["detail"] + "\n", "dim")

    # ---- text helpers ----------------------------------------------------
    def _say(self, s, tag=None):
        self.text.configure(state="normal")
        self.text.insert("end", s, tag or ())
        self.text.see("end")
        self.text.configure(state="disabled")

    def _clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


def build_window(root):
    """Both directions in one window. Returns (audit_app, forward_tab).

    Separate from main() so the construction can be smoke-tested without entering mainloop —
    kagami/tests.py does exactly that when a display is available.

    The audit panel is placed in the first tab unchanged: App grew one optional `container`
    argument and none of its methods moved. If the forward tab cannot be imported the window
    still opens with audit alone, because the tool a stranger is invited to run must not stop
    working because a newer panel broke.
    """
    root.title("Katana — build and audit")
    root.minsize(820, 600)

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)

    audit_frame = ttk.Frame(nb)
    nb.add(audit_frame, text="  Audit a sequence  ")
    app = App(root, container=audit_frame)

    fwd = None
    try:
        import gui_forward
        build_frame = ttk.Frame(nb)
        nb.add(build_frame, text="  Build from a Spec  ")
        fwd = gui_forward.ForwardTab(root, build_frame)
    except Exception as e:                     # noqa: BLE001 — never lose the audit tab
        print(f"[kagami] forward tab unavailable: {e!r}", file=sys.stderr)

    return app, fwd


def main():
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.3)
    except Exception:
        pass
    app, _fwd = build_window(root)
    # A file dragged onto the launcher, or passed on the command line: fill it in and start.
    # Waiting for the user to press a button they did not ask for would be worse than useless.
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        app.path.set(os.path.abspath(sys.argv[1]))
        root.after(300, app.run)
    root.mainloop()


if __name__ == "__main__":
    main()
