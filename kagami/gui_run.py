#!/usr/bin/env python3
"""Run a shipped Katana tool, showing the command first.

Milestone 3 of the forward tab. The tab drives the CLI rather than reimplementing it, so that
what the README documents and what the window does are the same thing — and so the CLI stays
the authority. This module is the whole of that mechanism: build the command, show it, stream
its output, allow cancelling it.

Two decisions worth keeping:

  The interpreter is the one already running, never "python" looked up again on PATH. The
  Windows launcher rejects any candidate under WindowsApps because the Microsoft Store ships a
  placeholder python.exe that is on PATH by default and answers "installed" on a machine with
  no Python at all. Re-resolving would discard that work.

  Tools are resolved from the repository root computed from __file__, never from the working
  directory. The launcher runs with the working directory inside kagami/ while the forward
  tools live one level up, so a relative path would simply not find them.

Only the tools shipped in this repository can be run. The allow-list is not a security
boundary — the person already has a shell — it is there so a typo becomes an exception here
instead of an attempt to execute something arbitrary.

on_line and on_done are called from a worker thread. Tk widgets must not be touched from one,
so a caller inside the GUI puts the payload on a queue and reads it from the main thread, the
way App already does with its _drain poll.
"""
import os
import shlex
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

TOOLS = frozenset({
    "katana_init.py", "find_part.py", "add_part.py", "check_design.py", "katana_build.py",
    "katana_drylab.py", "katana_lock.py", "katana_order_table.py", "katana_sbol.py",
    "get_genome.py", "blast_offtarget.py", "verify.py", "verify_library_v2.py",
})


def tool_cmd(tool: str, args, root: str = ROOT, py: str = None) -> "list[str]":
    """The argv for one shipped tool. Raises if `tool` is not one of them."""
    if tool not in TOOLS:
        raise ValueError(f"not a Katana tool: {tool!r}. One of: {', '.join(sorted(TOOLS))}")
    return [py or sys.executable, os.path.join(root, tool), *[str(a) for a in args]]


def shell_preview(cmd) -> str:
    """The command as a person would type it, for showing before it runs.

    Quoted for a POSIX shell. On Windows the quoting rules differ, so this is for READING and
    for copying into a terminal the user can check — it is never parsed back or executed.
    """
    return shlex.join([str(c) for c in cmd])


class Handle:
    """A running child. cancel() ends it; wait() blocks until it is done."""

    def __init__(self, proc, thread, done):
        self._proc, self._thread, self._done = proc, thread, done
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        try:
            self._proc.terminate()
        except Exception:
            pass

    def wait(self, timeout=None):
        self._done.wait(timeout)
        return self

    @property
    def done(self):
        return self._done.is_set()


def run_streaming(cmd, on_line=None, on_done=None, cwd=None) -> Handle:
    """Start cmd, delivering stdout line by line, and return a Handle.

    on_done receives {"returncode": int, "cancelled": bool}. A cancelled run reports
    cancelled=True and a non-zero returncode, and must never be reported as having finished —
    the audit's own rule: do not report a clean verdict for something that did not run.
    """
    proc = subprocess.Popen(
        [str(c) for c in cmd], cwd=cwd or ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
    done = threading.Event()
    handle = None

    def pump():
        try:
            for line in proc.stdout:
                if on_line:
                    on_line(line.rstrip("\n"))
        except Exception as e:                                    # noqa: BLE001
            if on_line:
                on_line(f"[reading output failed: {e!r}]")
        finally:
            rc = proc.wait()
            try:
                proc.stdout.close()
            except Exception:
                pass
            cancelled = bool(handle and handle.cancelled)
            # A terminated child can report 0 on some platforms. Cancelled is cancelled.
            if cancelled and rc == 0:
                rc = -1
            done.set()
            if on_done:
                on_done({"returncode": rc, "cancelled": cancelled})

    thread = threading.Thread(target=pump, daemon=True)
    handle = Handle(proc, thread, done)
    thread.start()
    return handle
