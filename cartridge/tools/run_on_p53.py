"""Reuse this experiment's encoded-PowerShell SSH transport and receipt format.

This is host orchestration only. It does not select firmware actions, retry,
touch UART directly, or read private credentials. Pass an exact command after --.
"""

from __future__ import annotations

import argparse
import base64
import re
import subprocess
from pathlib import Path


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--ssh-host", required=True, help="Explicit SSH host/alias, optionally user@host")
    parser.add_argument("--cwd")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*", args.ssh_host):
        parser.error("Use an exact SSH host or configured alias without shell syntax")
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("An exact remote executable and its arguments are required")
    script = "$ProgressPreference='SilentlyContinue'\n$ErrorActionPreference='Stop'\n"
    if args.cwd:
        script += "Set-Location " + ps_quote(args.cwd) + "\n"
    # Bypass Windows PowerShell's legacy native-argument repacking, which can
    # remove literal quotes inside arguments such as Python -c source.
    script += "$p53Start = New-Object System.Diagnostics.ProcessStartInfo\n"
    script += "$p53Start.FileName = " + ps_quote(command[0]) + "\n"
    script += "$p53Start.Arguments = " + ps_quote(subprocess.list2cmdline(command[1:])) + "\n"
    if args.cwd:
        script += "$p53Start.WorkingDirectory = " + ps_quote(args.cwd) + "\n"
    script += "$p53Start.UseShellExecute = $false\n"
    script += "$p53Start.CreateNoWindow = $true\n"
    script += "$p53Start.RedirectStandardOutput = $true\n"
    script += "$p53Start.RedirectStandardError = $true\n"
    script += "$p53Process = [System.Diagnostics.Process]::Start($p53Start)\n"
    script += "$p53ErrorRead = $p53Process.StandardError.ReadToEndAsync()\n"
    script += "while (($p53Line = $p53Process.StandardOutput.ReadLine()) -ne $null) { [Console]::Out.WriteLine($p53Line) }\n"
    script += "$p53Process.WaitForExit()\n"
    script += "[Console]::Error.Write($p53ErrorRead.Result)\nexit $p53Process.ExitCode\n"
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    with args.receipt.open("x", encoding="utf-8") as receipt:
        process = subprocess.Popen(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                args.ssh_host,
                "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            receipt.write(line)
            receipt.flush()
        code = process.wait()
        line = f"\nRemote exit: {code}\n"
        print(line, end="", flush=True)
        receipt.write(line)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
