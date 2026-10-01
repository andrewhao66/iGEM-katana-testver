#!/usr/bin/env python3
"""The forward-direction tab: pick a part by ID, write its seal: block into a Spec.

Milestone 2 of docs/superpowers/specs/2026-09-30-katana-gui-forward-tab-design.md. It wires
the window to gui_spec and nothing else; the CLI-driven steps (katana_init, find_part,
add_part, check_design, katana_build) are milestone 3.

Two things about this panel are deliberate and should not be "improved" later:

  There is no multi-line text box anywhere in it. A design names parts by ID, and the only
  way to choose one here is from the library's own manifest. The audit tab's "Paste
  sequence…" is a different thing — a construct arriving from outside to be checked — and the
  two tabs are coloured differently so that difference is visible rather than documented.

  When two sources disagree, the only buttons are "Copy readings" and "Close". No Fix, no
  re-seal, no overwrite. A GUI's reflex is to offer the repair, and on 2026-09-08 that repair
  would have written a false claim into the manifest that verified clean forever after.
"""
import os
import tkinter as tk
from tkinter import filedialog, ttk

import gui_spec

# A quiet wash of colour so the forward tab is not mistaken for the audit tab at a glance.
_TINT = "#f3f6fb"


class ForwardTab:
    def __init__(self, root, container):
        self.root = root
        self.entries = []
        self.lib_dir = None

        outer = ttk.Frame(container, padding=12)
        outer.pack(fill="both", expand=True)

        head = ttk.Label(
            outer, foreground="#444", justify="left",
            text="Build direction. Parts are chosen by ID from a sealed library — this panel "
                 "never accepts a sequence,\nand never edits one. It writes the seal: block "
                 "that add_part.py prints, into the Spec you choose.")
        head.pack(anchor="w", pady=(0, 10))

        # ---- the library ---------------------------------------------------
        libbox = ttk.LabelFrame(outer, text="1 · Library (its LOCK.tsv)", padding=10)
        libbox.pack(fill="x")
        self.lib = tk.StringVar()
        r1 = ttk.Frame(libbox); r1.pack(fill="x")
        ttk.Entry(r1, textvariable=self.lib).pack(side="left", fill="x", expand=True)
        ttk.Button(r1, text="Choose…", command=self.pick_lib).pack(side="left", padx=(8, 0))
        self.libnote = ttk.Label(libbox, foreground="#555", text="No library loaded.")
        self.libnote.pack(anchor="w", pady=(6, 0))

        # ---- the Spec ------------------------------------------------------
        specbox = ttk.LabelFrame(outer, text="2 · Design Spec to write into", padding=10)
        specbox.pack(fill="x", pady=(10, 0))
        self.spec = tk.StringVar()
        r2 = ttk.Frame(specbox); r2.pack(fill="x")
        ttk.Entry(r2, textvariable=self.spec).pack(side="left", fill="x", expand=True)
        ttk.Button(r2, text="Choose…", command=self.pick_spec).pack(side="left", padx=(8, 0))
        ttk.Label(specbox, foreground="#555",
                  text="Your comments are kept. The file is edited in place, line by line — "
                       "never re-emitted from a parser.").pack(anchor="w", pady=(6, 0))

        # ---- the part ------------------------------------------------------
        partbox = ttk.LabelFrame(outer, text="3 · Part", padding=10)
        partbox.pack(fill="both", expand=True, pady=(10, 0))
        r3 = ttk.Frame(partbox); r3.pack(fill="x")
        ttk.Label(r3, text="ID").pack(side="left")
        self.part = ttk.Combobox(r3, state="readonly", values=[], width=28)
        self.part.pack(side="left", padx=(8, 0))
        self.part.bind("<<ComboboxSelected>>", self._on_part)
        self.write_btn = ttk.Button(r3, text="Write seal into Spec",
                                    command=self.write, state="disabled")
        self.write_btn.pack(side="left", padx=(12, 0))
        self.partnote = ttk.Label(partbox, foreground="#555", justify="left",
                                  text="Choose a library first.")
        self.partnote.pack(anchor="w", pady=(8, 0))

        # ---- what happened --------------------------------------------------
        # Read-only, and it reports; it is not an editor. state="disabled" after every write
        # so nothing here can be typed into and mistaken for input.
        self.out = tk.Text(partbox, height=9, wrap="word", background=_TINT,
                           relief="flat", state="disabled")
        self.out.pack(fill="both", expand=True, pady=(10, 0))

    # ── pickers ────────────────────────────────────────────────────────────
    def pick_lib(self):
        p = filedialog.askopenfilename(title="Choose the library's LOCK.tsv",
                                       filetypes=[("LOCK manifest", "LOCK.tsv"),
                                                  ("All files", "*")])
        if not p:
            return
        self.lib.set(p)
        self.load_lib(p)

    def pick_spec(self):
        p = filedialog.askopenfilename(title="Choose a Design Spec",
                                       filetypes=[("Design Spec", "*.yaml *.yml"),
                                                  ("All files", "*")])
        if p:
            self.spec.set(p)
            self._refresh_button()

    # ── library ────────────────────────────────────────────────────────────
    def load_lib(self, lock_path):
        try:
            self.entries = gui_spec.read_library(lock_path)
        except Exception as e:
            self.entries = []
            self.lib_dir = None
            self.libnote.config(text=f"Could not read that manifest: {e}", foreground="#a00")
            self.part.config(values=[])
            self._refresh_button()
            return
        self.lib_dir = os.path.dirname(os.path.abspath(lock_path))
        ids = sorted(e["id"] for e in self.entries)
        self.part.config(values=ids)
        self.libnote.config(text=f"{len(self.entries)} parts sealed in this library.",
                            foreground="#555")
        self._refresh_button()

    def _entry(self, pid):
        for e in self.entries:
            if e["id"] == pid:
                return e
        return None

    def _on_part(self, _evt=None):
        e = self._entry(self.part.get())
        if not e:
            return
        # Deliberately no sequence shown: this is everything the manifest records EXCEPT the
        # bases, which gui_spec.read_library does not even return.
        self.partnote.config(
            text=f"{e['id']}  v{e['version']}   {e['length']} bp   {e['cls']}\n"
                 f"source: {e['source'][:110]}")
        self._refresh_button()

    def _refresh_button(self):
        ready = bool(self.entries and self.spec.get() and self.part.get())
        self.write_btn.config(state="normal" if ready else "disabled")

    # ── the write ──────────────────────────────────────────────────────────
    def write(self):
        e = self._entry(self.part.get())
        spec = self.spec.get()
        if not e or not spec:
            return
        res = gui_spec.insert_seal(spec, e["id"], e, self.lib_dir)
        self._say(res)
        if res["action"] == "stopped":
            self._stop_dialog(res)

    def _say(self, res):
        self.out.config(state="normal")
        self.out.delete("1.0", "end")
        self.out.insert("end", f"[{res['action'].upper()}]  {res['message']}\n")
        if res.get("readings"):
            self.out.insert("end", "\nreadings:\n")
            for k, v in res["readings"].items():
                self.out.insert("end", f"  {k:<16} {v}\n")
        self.out.config(state="disabled")

    def _stop_dialog(self, res):
        """Two readings, and no way to resolve them from here. Rule 4."""
        win = tk.Toplevel(self.root)
        win.title("Stopped — two sources disagree")
        win.transient(self.root)
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, justify="left", text=res["message"]).pack(anchor="w")
        if res.get("readings"):
            table = ttk.Frame(frame)
            table.pack(anchor="w", pady=(10, 0))
            for i, (k, v) in enumerate(res["readings"].items()):
                ttk.Label(table, text=k, foreground="#555").grid(row=i, column=0,
                                                                 sticky="w", padx=(0, 14))
                ttk.Label(table, text=str(v)).grid(row=i, column=1, sticky="w")
        ttk.Label(frame, foreground="#555", justify="left", pady=10,
                  text="Nothing was written. Which reading is correct is a question for a "
                       "person;\nanswering it here would destroy the evidence that they "
                       "differed.").pack(anchor="w")
        row = ttk.Frame(frame); row.pack(anchor="e")

        def copy():
            text = res["message"] + "\n\n" + "\n".join(
                f"{k}: {v}" for k, v in (res.get("readings") or {}).items())
            self.root.clipboard_clear()
            self.root.clipboard_append(text)

        ttk.Button(row, text="Copy readings", command=copy).pack(side="left")
        ttk.Button(row, text="Close", command=win.destroy).pack(side="left", padx=(8, 0))
