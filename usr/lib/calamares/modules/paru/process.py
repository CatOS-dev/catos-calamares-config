#!/usr/bin/env python3

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Callable, Sequence


class ProcessTimeout(subprocess.TimeoutExpired):
    """Raised after the complete process group has been terminated."""


def run_process_group(
    command: Sequence[str],
    *,
    timeout: float = 0,
    terminate_grace: float = 5,
    line_func: Callable[[str], None] | None = None,
) -> None:
    """Run a command in its own session and terminate all descendants on timeout."""
    proc = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )

    def consume_output() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            if line_func is not None and line.strip():
                line_func(line)

    reader = threading.Thread(target=consume_output, name="calamares-paru-output", daemon=True)
    reader.start()

    try:
        proc.wait(timeout=timeout if timeout > 0 else None)
    except subprocess.TimeoutExpired as error:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

        try:
            proc.wait(timeout=terminate_grace)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()

        reader.join(timeout=terminate_grace)
        raise ProcessTimeout(command, timeout) from error
    finally:
        if proc.stdout is not None:
            proc.stdout.close()

    reader.join(timeout=terminate_grace)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, list(command))
