"""Verify that the published firmware files match the frozen source inventory."""
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    manifest = json.loads((ROOT / "firmware/source-manifest.json").read_text())
    for name, expected in manifest["files"].items():
        path = ROOT / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]:
            raise RuntimeError("Firmware source differs from frozen inventory: " + name)
    print(f"PASS: {len(manifest['files'])} unchanged firmware files")


if __name__ == "__main__":
    main()
