#!/usr/bin/env python3
"""Subprocess helpers that do not leave descendant processes behind."""

import os
import signal
import subprocess
from typing import Any, Sequence


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    else:
        proc.terminate()
    try:
        proc.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    else:
        proc.kill()
    proc.wait(timeout=2)


def run_process(
    cmd: Sequence[str],
    *,
    capture_output: bool = False,
    text: bool = False,
    timeout: float = None,
    cwd: str = None,
    env: dict = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """Run a command in its own process group and reap it on timeout."""
    if capture_output:
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    proc = subprocess.Popen(
        list(cmd),
        text=text,
        cwd=cwd,
        env=env,
        start_new_session=(os.name == "posix"),
        **kwargs,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=0.2)
        except subprocess.TimeoutExpired:
            stdout, stderr = exc.output, exc.stderr
        raise subprocess.TimeoutExpired(
            cmd=list(cmd),
            timeout=timeout,
            output=stdout if stdout is not None else exc.output,
            stderr=stderr if stderr is not None else exc.stderr,
        )
    return subprocess.CompletedProcess(list(cmd), proc.returncode, stdout, stderr)
