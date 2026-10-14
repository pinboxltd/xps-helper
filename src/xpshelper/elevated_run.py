"""Run one XPS Helper command elevated and report the result.

This is deliberately a *file*, not an inline ``cmd /c "..."`` command line.

Why
---
Passing a quoted command through ``ShellExecuteW(..., "runas", ...)`` means cmd
receives something like::

    cmd /c ""C:\\path with spaces\\python.exe" -m xpshelper profile quiet" > out 2> err

cmd's quoting rules then strip the outer pair of quotes, and because the
remainder still holds a space it resolves the *whole* first token as a filename,
fails with "The system cannot find the file specified", and exits 0. The result
is a write that never happened being reported as success -- the single most
dangerous outcome for a tool that changes firmware settings.

Putting the same command in a script file removes the ambiguity entirely: the
launcher only ever has to run one unquoted, space-free path, so redirection and
the exit code behave predictably.

Usage (from the elevated bootstrap):

    python -m xpshelper.elevated_run <request.json>

The request file holds the command plus the files to write output and the exit
code to. The exit-code file is written **last**, so its appearance is proof the
child finished rather than merely started.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if len(argv) > 1:
        print("usage: python -m xpshelper.elevated_run [request.json]", file=sys.stderr)
        return 2

    if argv:
        try:
            with open(argv[0], encoding="utf-8") as handle:
                request: dict[str, Any] = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"could not read the elevation request: {exc}", file=sys.stderr)
            return 2
    else:
        # No argument: this is the Scheduled Task path. A task action is a fixed
        # command line, so the request is picked up from the location both halves
        # derive. Validation (including a staleness check) lives there.
        from .privileged import HelperError, clear_request, read_request

        try:
            request = read_request()
        except HelperError as exc:
            print(f"no usable privileged request: {exc}", file=sys.stderr)
            return 2
        finally:
            # Clear it regardless, so a failed run cannot be replayed.
            clear_request()

    command = [str(part) for part in request.get("command") or []]
    if not command:
        print("the elevation request contained no command", file=sys.stderr)
        return 2

    workdir = request.get("cwd") or None
    environment = dict(os.environ)
    for key, value in (request.get("env") or {}).items():
        environment[str(key)] = str(value)

    try:
        completed = subprocess.run(  # noqa: S603 - argv list, no shell
            command,
            cwd=workdir,
            env=environment,
            capture_output=True,
            text=True,
            errors="replace",
        )
        code, out, err = completed.returncode, completed.stdout or "", completed.stderr or ""
    except OSError as exc:
        # The command could not even be started. Report it as a failure rather
        # than letting the caller conclude the work succeeded.
        code, out, err = 127, "", f"could not start {command[0]}: {exc}"

    _write(request.get("out_file"), out)
    _write(request.get("err_file"), err)
    # Written last: this file is the completion sentinel.
    _write(request.get("code_file"), f"{code}\n")
    return code


def _write(path: Any, text: str) -> None:
    if not path:
        return
    try:
        with open(str(path), "w", encoding="utf-8", errors="replace") as handle:
            handle.write(text)
    except OSError:
        pass


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
