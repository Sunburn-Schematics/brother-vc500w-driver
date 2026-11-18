"""Print one existing calibration SVG through the canonical printer owner.

Template: calibration-attempt-04-restored-chip-fresh-roll-direct.json and the
reviewed owner's supported POST /print route. Never opens printer TCP9100.
Run explicitly only after the emulator's transport and loaded roll are ready.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import urllib.error
import urllib.request
import urllib.parse


def request(owner: str, path: str, payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        owner + path,
        data=body,
        headers={"Content-Type": "application/json"} if body else {},
    )
    with urllib.request.urlopen(req, timeout=90 if body else 15) as response:
        return json.load(response)


def save(path: Path, evidence: dict) -> None:
    with path.open("w", encoding="utf-8") as stream:
        json.dump(evidence, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--svg", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--owner-url", required=True, help="Exact trusted printer-owner origin")
    parser.add_argument("--printer-serial", required=True)
    parser.add_argument("--tape-type", default="3/4")
    args = parser.parse_args()
    origin = urllib.parse.urlsplit(args.owner_url)
    if (origin.scheme not in {"http", "https"} or not origin.hostname or
            origin.username or origin.password or origin.query or origin.fragment or
            origin.path not in {"", "/"}):
        parser.error("owner-url must be an HTTP(S) origin without credentials, path or query")
    owner = args.owner_url.rstrip("/")
    svg = args.svg.read_bytes()
    evidence = {
        "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "template_source": "calibration-attempt-04-restored-chip-fresh-roll-direct.json",
        "owner": owner,
        "svg_path": str(args.svg.resolve()),
        "svg_sha256": hashlib.sha256(svg).hexdigest(),
        "automatic_retry": False,
        "post_may_have_reached_owner": False,
        "physical_output_confirmed": False,
    }
    # Exclusive reservation prevents an accidental rerun from replacing evidence.
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(evidence, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        evidence["queue_before"] = queue = request(owner, "/print/queue/status")
        evidence["status_before"] = status = request(owner, "/status")
        printer = status["printer"]
        remaining = printer.get("tapeRemainingMm")
        if not (
            queue.get("state") == "idle"
            and queue.get("queued", 0) == 0
            and not queue.get("error")
            and printer.get("connected") is True
            and printer.get("cached") is False
            and printer.get("busy") is False
            and printer.get("state") == "IDLE"
            and printer.get("stage") in {"READY FOR PRINT", "SUCCESS"}
            and printer.get("error") == "NONE"
            and printer.get("serial") == args.printer_serial
            and printer.get("tapeType") == args.tape_type
            and isinstance(remaining, (float, int))
            and remaining > 40
        ):
            raise RuntimeError("Printer/queue is not freshly verified ready for one calibration")
        payload = {
            "svgData": svg.decode("utf-8"),
            "options": {"cutMode": "full", "quality": "vivid", "copies": 1},
            "meta": {"mpn": "ESP32 EEPROM CALIBRATION", "dimensions": "288x270"},
        }
        evidence["post_may_have_reached_owner"] = True
        save(args.output, evidence)
        evidence["response"] = request(owner, "/print", payload)
        evidence["status_after"] = request(owner, "/status")
        evidence["outcome"] = "owner_response_retained_requires_physical_confirmation"
    except Exception as error:
        evidence["outcome"] = "unverified_review_before_retry"
        evidence["error_type"] = type(error).__name__
        evidence["error"] = str(error)
        if isinstance(error, urllib.error.HTTPError):
            evidence["http_error_body"] = error.read(32768).decode(errors="replace")
        raise
    finally:
        evidence["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save(args.output, evidence)
    print(json.dumps(evidence["response"]))


if __name__ == "__main__":
    main()
