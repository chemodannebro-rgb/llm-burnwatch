"""Exec sink: runs a fixed local command, writing the alert (as a JSON
string) to its stdin.

Security-critical: `command` is a list of argv-style arguments, never a
shell string, and is passed to `subprocess.run` with `shell=False` hard-coded
(not a caller-configurable option) -- so alert content, which may include
user-supplied `label`/`extra` text carried through from the log, can never
be interpreted as shell syntax. See SECURITY.md's alert-sinks section for the
threat this specifically defends against, and what it explicitly does not:
the command itself is fully trusted -- llm-burnwatch does not vet what it
does with the alert JSON it's handed, and a command that itself interprets
its arguments as code/templates (e.g. `sh -c`) reopens exactly the injection
risk this sink is designed to avoid.

The alert is deliberately passed via stdin, not as an argv entry: `command`
itself (the fixed argv this sink was configured with) never changes, but the
alert JSON does, once per delivery, and can carry log content
(`label`/`extra`) the configuring user didn't necessarily write themselves.
Process argv is visible to every other local user via `ps`/`/proc/<pid>/
cmdline`; stdin is not. `--webhook-url`/`--slack-webhook-url` get the same
treatment for the analogous reason (prefer env vars over the flag for a
secret-bearing URL) -- this is that same discipline applied to the payload
instead of the destination.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess

from ..detectors.protocol import Alert
from .protocol import SinkError

TIMEOUT_SECONDS = 10


class ExecSink:
    name = "exec"

    def __init__(self, command: list[str], timeout: float = TIMEOUT_SECONDS) -> None:
        if not command:
            raise ValueError("ExecSink command must be a non-empty list of arguments")
        self.command = list(command)
        self.timeout = timeout

    def send(self, alert: Alert) -> None:
        payload = json.dumps(dataclasses.asdict(alert))
        try:
            result = subprocess.run(
                self.command,
                input=payload.encode("utf-8"),
                shell=False,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except OSError as exc:
            raise SinkError(f"failed to run exec sink command {self.command}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise SinkError(f"exec sink command {self.command} timed out: {exc}") from exc

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            detail = f": {stderr}" if stderr else ""
            raise SinkError(
                f"exec sink command {self.command} exited {result.returncode}{detail}"
            )
