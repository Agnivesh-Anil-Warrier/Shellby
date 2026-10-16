"""Smoke test for Linux POSIX PTY recording integration."""

import glob
import json
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main():
    temp_dir = tempfile.mkdtemp(prefix="shellby_ci_linux_")
    master = None
    slave = None
    proc = None
    try:
        master, slave = pty.openpty()

        def set_controlling_terminal():
            import fcntl
            import termios
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        script_dir = Path(__file__).resolve().parent
        shellby_py = script_dir / "shellby.py"

        proc = subprocess.Popen(
            [sys.executable, str(shellby_py), "record", "--dir", temp_dir, "--title", "ci_smoke", "--no-guide"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            preexec_fn=set_controlling_terminal,
        )
        os.close(slave)
        slave = None

        # Bounded readiness loop: wait until child starts up and signals readiness
        start_time = time.time()
        startup_output = b""
        started = False
        while time.time() - start_time < 10.0:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"Shellby process exited prematurely with code {proc.returncode} "
                    f"before accepting input. Output: {startup_output.decode(errors='replace')!r}"
                )
            r, _, _ = select.select([master], [], [], 0.1)
            if master in r:
                try:
                    chunk = os.read(master, 1024)
                    if chunk:
                        startup_output += chunk
                        if b"Shellby is recording" in startup_output or b"$" in startup_output or b"#" in startup_output:
                            started = True
                            break
                except OSError:
                    break

        if not started:
            raise RuntimeError(
                f"Timed out waiting for Shellby startup signal. Process poll: {proc.poll()}, "
                f"Output captured: {startup_output.decode(errors='replace')!r}"
            )

        # Write computed arithmetic command and wait for output execution signal
        try:
            os.write(master, b"expr 41000 + 1234\n")
        except OSError as exc:
            raise RuntimeError(f"Failed to write command to PTY master: {exc}")

        cmd_start = time.time()
        cmd_output = b""
        executed = False
        while time.time() - cmd_start < 10.0:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"Process exited prematurely with code {proc.returncode} while waiting for command execution. "
                    f"Captured output: {cmd_output.decode(errors='replace')!r}"
                )
            r, _, _ = select.select([master], [], [], 0.1)
            if master in r:
                try:
                    chunk = os.read(master, 1024)
                    if chunk:
                        cmd_output += chunk
                        if b"42234" in cmd_output:
                            executed = True
                            break
                except OSError:
                    break

        if not executed:
            raise RuntimeError(
                f"Timed out waiting for execution result '42234' on PTY master. "
                f"Captured: {cmd_output.decode(errors='replace')!r}"
            )

        # Send clean exit signal
        try:
            os.write(master, b"exit\n")
        except OSError as exc:
            raise RuntimeError(f"Failed to send exit command to PTY master: {exc}")

        try:
            proc.wait(timeout=25)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise RuntimeError("Linux PTY recording process timed out after 25 seconds.")

        if proc.returncode != 0:
            raise RuntimeError(f"Linux PTY recording failed with exit code {proc.returncode}")

        matches = glob.glob(f"{temp_dir}/**/ci_smoke*.json", recursive=True)
        if len(matches) != 1:
            raise RuntimeError(
                f"Expected exactly one session JSON, found {len(matches)}: {matches}"
            )

        with open(matches[0], "r", encoding="utf-8") as f:
            data = json.load(f)

        # Validate schema against Linux Session model
        required_fields = ["session_id", "started_at", "shell", "columns", "initial_cwd", "events", "ended_at", "duration"]
        for req_field in required_fields:
            if req_field not in data:
                raise RuntimeError(f"Session JSON missing required field: '{req_field}'")

        if not isinstance(data["columns"], int) or data["columns"] <= 0:
            raise RuntimeError(f"Session JSON invalid columns: {data['columns']}")

        events = data.get("events", [])
        if not isinstance(events, list) or len(events) == 0:
            raise RuntimeError("Session JSON has no recorded events.")

        valid_types = {"input", "output", "cwd", "resize"}
        for idx, ev in enumerate(events):
            if not isinstance(ev, dict) or "type" not in ev or "time" not in ev or "text" not in ev:
                raise RuntimeError(f"Event at index {idx} does not conform to Event schema: {ev}")
            if ev["type"] not in valid_types:
                raise RuntimeError(f"Event at index {idx} has unrecognized type: {ev['type']}")

        input_events = [ev for ev in events if ev["type"] == "input"]
        output_events = [ev for ev in events if ev["type"] == "output"]

        input_text = "".join(ev.get("text", "") for ev in input_events)
        output_text = "".join(ev.get("text", "") for ev in output_events)

        if "41000 + 1234" not in input_text:
            raise RuntimeError(
                f"Command input '41000 + 1234' not found in input events. Captured: {input_text!r}"
            )
        # Verify computed result '42234' exists in output (proves execution occurred, not just command echo)
        if "42234" not in output_text:
            raise RuntimeError(
                f"Computed execution output '42234' not found in output events. Captured: {output_text!r}"
            )
        # Verify result was NOT typed into input
        if "42234" in input_text:
            raise RuntimeError(
                "Execution output '42234' was unexpectedly present in input events."
            )

        event_count = len(events)
        print(f"Linux PTY recording verified successfully: {event_count} events recorded, execution and schema verified.")
    finally:
        if slave is not None:
            try:
                os.close(slave)
            except OSError:
                pass
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
