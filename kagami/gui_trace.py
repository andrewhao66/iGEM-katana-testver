#!/usr/bin/env python3
"""Breadcrumbs for the calls that can abort the interpreter instead of raising.

On macOS, Tk's native file panels and clipboard go through Cocoa. When Cocoa raises —
for example NSInvalidArgumentException, "object cannot be nil" — it is NOT a Python
exception. libc++abi terminates the process with SIGABRT, exit 134, and Python never gets
a frame, so there is no traceback and nothing in the log says what the user had just done.

That happened once to this window and could not be reproduced afterwards: launching it,
raising it to the front, querying it over the accessibility API, and opening each of its
three file dialogs all survive. Without a breadcrumb the next occurrence is as uninformative
as the first.

So every call that crosses into Cocoa writes a line first and flushes it. If the process
dies, the last line in the log names the call that was in flight. The log is small, append
only, and lives beside the other scratch output rather than in the repository.

This instruments the MODULE functions, not the panels' methods, so neither App nor
ForwardTab had to change to be covered.

Nothing here may break the window: every hook is wrapped, and a failure to trace is
silently ignored. A debugging aid that can take down the thing it watches is worse than none.
"""
import os
import tempfile
import time

LOG = os.environ.get("KATANA_GUI_TRACE") or os.path.join(
    tempfile.gettempdir(), "katana_gui_trace.log")

_installed = False


def mark(what: str) -> None:
    """Append one breadcrumb and flush it, so it survives an abort."""
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {what}\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        pass


def _wrap(mod, name, label):
    try:
        fn = getattr(mod, name)
    except AttributeError:
        return
    if getattr(fn, "_katana_traced", False):
        return

    def traced(*a, **kw):
        mark(f"ENTER {label} kwargs={sorted(kw)}")
        try:
            r = fn(*a, **kw)
        except BaseException as e:
            mark(f"RAISE {label} {type(e).__name__}: {e}")
            raise
        mark(f"LEAVE {label}")
        return r

    traced._katana_traced = True
    try:
        setattr(mod, name, traced)
    except Exception:
        pass


def install(root=None) -> str:
    """Hook the Cocoa-crossing calls. Safe to call more than once. Returns the log path."""
    global _installed
    if _installed:
        return LOG
    _installed = True
    mark("---- window starting ----")
    try:
        from tkinter import filedialog
        for n in ("askopenfilename", "asksaveasfilename", "askdirectory",
                  "askopenfilenames"):
            _wrap(filedialog, n, f"filedialog.{n}")
    except Exception:
        pass
    try:
        from tkinter import messagebox
        for n in ("showerror", "showwarning", "showinfo", "askyesno", "askokcancel"):
            _wrap(messagebox, n, f"messagebox.{n}")
    except Exception:
        pass
    # The clipboard is the other Cocoa path: NSPasteboard, reached by clipboard_clear and
    # clipboard_append. These are methods on the widget class, so they are hooked there.
    try:
        import tkinter as tk
        for n in ("clipboard_clear", "clipboard_append", "clipboard_get",
                  "selection_get", "selection_own"):
            _wrap(tk.Misc, n, f"Misc.{n}")
    except Exception:
        pass
    # A Python exception inside a Tk callback normally prints and is swallowed; record it too,
    # so the log shows ordinary failures as well as the fatal ones.
    if root is not None:
        try:
            import traceback

            def report(exc, val, tb):
                mark("CALLBACK EXCEPTION " + "".join(
                    traceback.format_exception_only(exc, val)).strip())
                traceback.print_exception(exc, val, tb)

            root.report_callback_exception = report
        except Exception:
            pass
    return LOG
