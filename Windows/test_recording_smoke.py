"""Smoke test for Windows Command Prompt recording integration."""

import glob
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    temp_dir = tempfile.mkdtemp(prefix="shellby_ci_win_")
    proc = None
    try:
        script_dir = Path(__file__).resolve().parent
        shellby_py = script_dir / "shellby.py"

        proc = subprocess.Popen(
            [sys.executable, str(shellby_py), "record", "--dir", temp_dir, "--title", "ci_smoke", "--no-guide"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            test_input = "cmd /c \"set /a 41000+1234\"\nexit\n"
            stdout, stderr = proc.communicate(input=test_input, timeout=25)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            raise RuntimeError(
                f"Windows recording process timed out after 25 seconds.\nStdout: {stdout}\nStderr: {stderr}"
            )

        if proc.returncode != 0:
            raise RuntimeError(
                f"Windows recording process failed with exit code {proc.returncode}.\n"
                f"Stderr: {stderr}\nStdout: {stdout}"
            )

        matches = glob.glob(f"{temp_dir}/**/ci_smoke*.json", recursive=True)
        if len(matches) != 1:
            raise RuntimeError(
                f"Expected exactly one session JSON, found {len(matches)}: {matches}.\nStdout: {stdout}\nStderr: {stderr}"
            )

        with open(matches[0], "r", encoding="utf-8") as f:
            data = json.load(f)

        # Validate schema against Windows Session model
        required_fields = ["session_id", "started_at", "shell", "columns", "initial_cwd", "events", "ended_at", "duration"]
        for req_field in required_fields:
            if req_field not in data:
                raise RuntimeError(f"Session JSON missing required field: '{req_field}'")

        if not isinstance(data["columns"], int) or data["columns"] <= 0:
            raise RuntimeError(f"Session JSON invalid columns: {data['columns']}")

        events = data.get("events", [])
        if not isinstance(events, list) or len(events) == 0:
            raise RuntimeError("No events captured in session JSON")

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

        if "41000+1234" not in input_text:
            raise RuntimeError(
                f"Command input '41000+1234' not found in input events. Captured: {input_text!r}"
            )
        # Verify computed result '42234' exists in output (distinguishes command execution from terminal echo)
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
        print(f"Windows recording verified successfully: {event_count} events recorded, execution and schema verified.")
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
