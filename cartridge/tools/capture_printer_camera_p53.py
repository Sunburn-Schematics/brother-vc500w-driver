"""Capture only the authorized external EMEET printer camera on P53.

Reuses this experiment's approved run_on_p53 transport. No app launch, service,
UART, printer command, integrated-camera fallback, or device scan is performed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

REMOTE_PROGRAM = r'''
import datetime,hashlib,json,pathlib,subprocess,sys
destination=pathlib.Path(sys.argv[1]); destination.mkdir(parents=True,exist_ok=False)
frames=int(sys.argv[2]); fps=int(sys.argv[3]); executable=sys.argv[4]; camera=sys.argv[5]
command=[executable,"-hide_banner","-nostdin","-f","dshow","-i",
         "video="+camera,"-ss","1","-vf",f"fps={fps}",
         "-frames:v",str(frames),"-q:v","2",str(destination/"frame-%03d.jpg")]
started=datetime.datetime.now(datetime.timezone.utc).isoformat()
try:
    result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                          text=True,timeout=20)
    log=result.stdout; code=result.returncode
except subprocess.TimeoutExpired as error:
    log=error.stdout or b""; log=log.decode(errors="replace") if isinstance(log,bytes) else log
    code=124
(destination/"ffmpeg.txt").write_text(log,encoding="utf-8")
images=sorted(destination.glob("frame-*.jpg"))
manifest={"started_at_utc":started,"finished_at_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),
          "camera":camera,
          "command":command,"returncode":code,"requested_frames":frames,
          "nominal_frames_per_second":fps,"warmup_seconds":1,
          "frame_times_are_nominal_not_hardware_timestamps":True,
          "images":[{"file":image.name,"bytes":image.stat().st_size,
                     "sha256":hashlib.sha256(image.read_bytes()).hexdigest()} for image in images]}
(destination/"manifest.json").write_text(json.dumps(manifest,indent=2)+"\n",encoding="utf-8")
print(json.dumps(manifest),flush=True)
raise SystemExit(code or (0 if len(images)==frames else 1))
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--remote-python", required=True)
    parser.add_argument("--ffmpeg", required=True, help="Exact remote FFmpeg executable")
    parser.add_argument("--remote-root", required=True, help="Remote Windows output directory")
    parser.add_argument("--camera-name", required=True, help="Exact DirectShow device; no fallback")
    parser.add_argument("--frames", type=int, default=1)
    parser.add_argument("--fps", type=int, choices=(1, 2, 4, 5, 10), default=5)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*", args.ssh_host):
        parser.error("Use an exact SSH host or configured alias")
    if not 1 <= args.frames <= 24 or args.frames / args.fps > 8:
        parser.error("Use 1..24 frames with at most eight seconds nominal capture")
    args.output.mkdir(parents=True, exist_ok=False)
    identifier = "camera-" + uuid.uuid4().hex
    remote_directory = args.remote_root.rstrip("\\/") + "\\" + identifier
    code = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("run_on_p53.py")),
            "--ssh-host",
            args.ssh_host,
            "--receipt",
            str(args.output / "transport.txt"),
            "--",
            args.remote_python,
            "-c",
            REMOTE_PROGRAM,
            remote_directory,
            str(args.frames),
            str(args.fps),
            args.ffmpeg,
            args.camera_name,
        ],
        check=False,
    ).returncode
    copied = subprocess.run(
        [
            "scp",
            "-r",
            args.ssh_host + ":" + remote_directory.replace("\\", "/"),
            str(args.output / "frames"),
        ],
        check=False,
        timeout=45,
    ).returncode
    (args.output / "capture-source.json").write_text(
        json.dumps(
            {"remote_directory": remote_directory, "remote_exit": code, "scp_exit": copied},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    raise SystemExit(code or copied)


if __name__ == "__main__":
    main()
