#!/usr/bin/env python3
"""Bounded subprocess execution shared by local media engines."""
import subprocess


class ProcessTimeoutError(RuntimeError):
    """A local renderer exceeded its bounded execution window."""


def run_cmd(command, *, cwd=None, timeout=600, check=False, text=True,
            capture_output=False, **kwargs):
    """Run a child process with a finite timeout and useful timeout context."""
    try:
        result = subprocess.run(command, cwd=cwd, timeout=timeout, check=check,
                                text=text, capture_output=capture_output, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise ProcessTimeoutError(
            "PROCESS_TIMEOUT after %ss: %s" % (timeout, " ".join(map(str, command)))
        ) from exc
    return result
