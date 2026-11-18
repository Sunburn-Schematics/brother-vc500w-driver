# Cartridge experiment tools

These tools accompany the unchanged firmware in `../firmware`. The current
firmware is **eeprom_emulator_v12**, a classic ESP32 prototype. This source
publication does not establish generic M24C02 compatibility or cartridge-length
endurance. See [the build guide](../../docs/cartridge-build.md) and
[the experiment history](../../docs/cartridge-history.md).

## Offline build and tests

From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r cartridge/requirements.txt
make -C cartridge test PYTHON=../.venv/bin/python
make -C cartridge bootstrap PYTHON=../.venv/bin/python
make -C cartridge firmware PYTHON=../.venv/bin/python
# Independent passive sniffer, retaining the same updater:
make -C cartridge firmware SKETCH=bounded100_i2c PYTHON=../.venv/bin/python
```

The last two build commands are instructions for a user's own source build, not
a published binary release. `bootstrap` installs Arduino CLI 1.6.0 and ESP32 core
2.0.17 in `.cache`; it never changes global Arduino configuration. Automatic CLI
download is supported on Linux x86_64 and checks the official release SHA256.
On another platform install official CLI 1.6.0 yourself, then pass
`ARDUINO_CLI=/absolute/path/to/arduino-cli` to both commands. The board identifier
is `esp32:esp32:esp32doit-devkit-v1`; all needed libraries ship with that core.
Builds go to `.build/<sketch>`, including a `build-receipt.json` with the new app
and ELF hashes. Build paths/metadata can change those hashes; use your own
receipt for later pins. The helper has no upload operation.

`make test` runs fake-UART Python regressions, eleven C++ model harnesses with
AddressSanitizer/UndefinedBehaviorSanitizer, and the frozen firmware inventory.
Native harnesses require a C++17 compiler (`CXX=g++` by default). They generate
synthetic CRC-valid images under `.build/native-tests`, never a real cartridge
identity. They exercise models and host logic, not slave timing.

Original event-pinned recovery tests are retained but skipped in a public
checkout. Their private raw evidence and cartridge images are not distributed.
The experiment operator can opt into replay with `BROTHER_PRIVATE_REPLAY_DIR`
pointing at the original evidence tree. A missing explicitly selected bundle
then fails, rather than silently becoming synthetic evidence. Public V12 tests
separately exercise nine current framing, D19, START and STOP regressions using
generated data. These modified public adapters have offline regression coverage;
they have not been requalified on hardware.

## Select the exact device and pin the firmware

Live tools accept an explicit COM port or absolute `/dev/` serial path. They
require the enumerated device to be CP210x VID `10c4`, PID `ea60`; emulator tools
also require its exact serial number. They do not scan by opening candidate
ports or silently choose another device. Pass your own values, for example
`--port COM7 --serial-number YOUR_CP210X_SERIAL` on Windows, or
`--port /dev/ttyUSB0 --serial-number YOUR_CP210X_SERIAL` on Linux. POSIX aliases
are resolved against the enumerated port. Other USB bridge chips are not
supported by the current device guard.

`manage_esp32.py` provides the common no-button USB updater and authenticated
ArduinoOTA transport. The default UART revision is its historical V2 value;
**always pass `--expect-firmware` for the firmware actually running**. Example
passive status command (substitute all pins and paths):

```sh
python cartridge/tools/manage_esp32.py netinfo \
  --port COM7 --serial-number YOUR_CP210X_SERIAL \
  --expect-firmware i2c-eeprom-emulator-v12 --audit local-netinfo.jsonl
```

USB update adds `--app path/to/sketch.ino.bin --sha256 YOUR_APP_SHA256` and action
`update`. It uses bounded chunk acknowledgments and checks the image hash.
`ota` requires those app pins plus `--espota-script`, `--target`, `--local-ip`
and `--secrets`; the official ESP32 2.0.17 `tools/espota.py` is obtained from the
installed core, not vendored here. Its checked source SHA256 is
`894d128e8a1e755aab5cdf60b17e1066a817c6234b4271c034fa0edeb0643646`.
The uploader must bind an IPv4 address reachable from the ESP32 because
ArduinoOTA calls back to the host. OTA/USB updates require a closed passive
session and D19 OFF. They never replace the separate ROM installation needed
before the first application with an updater is present.

`wifi-set` takes a private JSON file through `--secrets`, with exactly `ssid`,
`password`, `ota_password`. No credentials belong in command arguments, source
control, screenshots, fixtures or logs. Use owner-only permissions on POSIX
(0600); Windows ACLs are checked. Successful provisioning reboots to replace
the ArduinoOTA authentication hash. Do not include this file in a public issue.

## Current emulator control and provisioning limit

`manage_emulator_v12.py` provides `status`, `diag`, `start`, `start-pullup`,
`pullup-on`, `pullup-off`, `stop`, and the archival `recover` command. Each takes
`--port`, `--serial-number`, `--expect-elf-sha256`, `--image-sha256`, and a new
`--output` evidence directory. `start` additionally requires `--sole-0x50` and
the exact `--durable-generation`. `start-pullup` also requires
`--printer-powered`. These are operator assertions; software cannot establish
physical wiring, power, roll length, or absence of another EEPROM.

The tested sequence loaded a complete provenance-pinned image using **V6**,
then upgraded the application while preserving its compatible NVS checkpoint.
**A fresh V12 boots passive and unloaded. The V12 host manager has no LOAD
action.** The historical `manage_emulator.py` and `manage_emulator_v7.py` include
LOAD but require their exact historical firmware; changing the expected identity
to bypass that guard is not a supported provisioning procedure. No public
cartridge seed, generic fresh-V12 loader, or automatic counter reset is supplied.

The historical recovery commands pin specific observed events, a particular
image hash and generation, and their complete original proofs. They are not
universal EEPROM reset or fault-clear utilities. The excluded evidence bundles
cannot be recreated by redacting a manifest or substituting synthetic data.

The D19 experiment uses GPIO19 HIGH **through 4.7 kΩ to SDA**, then input/high-Z
for OFF; it never drives LOW for OFF. GPIO21 is SDA and GPIO22 is SCL. A physical
EEPROM must not also respond. D19 must be OFF before the printer is powered off;
there is no automatic printer-power sensing or backfeed protection. Live RAM
writes are not durable until explicit detach/checkpoint succeeds. An unclean
session retains uncertainty rather than resetting the counter.

## Sniffer, isolated bench and supporting tools

- `capture_serial.py`: `--port`, `--serial-number`, `--expect-firmware`,
  `--duration`, and a new `--output` directory. Its default revision is V4
  `i2c-passive-poll-v4`. The current emulator must be inactive before capture.
  Window gaps and overflow remain explicit; no lossless capture claim.
- `decode_capture.py`: offline raw-edge decoder; `--help` gives the capture and
  metadata arguments. It separates sampling windows and retains ambiguities.
- `inspect_eeprom.py`: offline full-256-byte CRC/field inspection. It does not
  establish physical tape length or write anything.
- `bench_eeprom.py`: historical V5 isolated-chip read/restore tool. It requires
  physical signal isolation, exact image/ELF pins and an earlier qualified
  read receipt before restore. Do not run BENCH on the printer-owned bus.
- `print_emulator_calibration.py`: one full-cut calibration via a compatible
  owner API, with required `--owner-url` and `--printer-serial`, optional
  `--tape-type`, and explicit `--svg`/`--output`. No private address is built in.
  This targets the experiment owner's richer API; the standalone root driver
  is not itself an HTTP server implementing that API. It never opens TCP9100.
- `run_on_p53.py`: legacy filename for an explicit `--ssh-host` Windows
  PowerShell transport. It takes an exact executable/argument list after `--`.
- `capture_printer_camera_p53.py`: optional explicit remote DirectShow capture;
  requires `--ssh-host`, `--remote-python`, `--ffmpeg`, `--remote-root`, and
  `--camera-name`. No camera enumeration or integrated-camera fallback.

Earlier version-specific managers and `start_when_high_v9.py` remain to explain
the progression. They are not substitutes for the latest version's guards.
`history/audits/` preserves the authored experiment audit scripts as historical
source. They expect the original private build/evidence layout and are not
portable build or release commands. `research/` contains static ARM ELF
inspection tools with explicit caller-supplied inputs/outputs. They never run
the vendor target. Brother firmware, SDKs, binaries, toolchains, full flash
dumps, real EEPROM images, raw site evidence and the draft batch-print helper
are deliberately absent.

The repository's existing LICENSE applies to authored project code. Upstream
Arduino/Espressif, Python and FFmpeg software retains its own licensing; this
directory does not relicense or redistribute those packages.

## V13 initialization-budget candidate

`manage_emulator_v13.py` matches only `i2c-eeprom-emulator-v13`. It adds
`policy-enable`, `policy-disable` and the original-event-only `recover-stop`.
The [V13 policy guide](../../docs/cartridge-v13-budget.md) describes the separate
advertised image, exact image/generation guards and accepted live initialization.
Ten website jobs completed with verification; an eleventh remains uncertain
after status loss. The guide records the full result and remaining limits.
The default build and earlier V12 source remain unchanged.

Policy is explicit, volatile and OFF after boot. Enable takes
`--capacity-counts` in 1–11232, defaulting to 11232. Both enable and disable
require the current full backing-image SHA-256 and `--durable-generation`, a
clean passive session and D19 OFF. They preserve the newest backing bytes;
disable does not restore a stale seed. `status` saves backing and advertised
256-byte images with separate generation information. Policy controls do not
toggle D19, submit a print or refill the view after a verification read. The
separate explicit `start-pullup` action retains its existing D19 control.

The two V13 original-event proof tests use the existing private replay opt-in;
other V13 tests use synthetic images. Recovery remains limited to its exact
private failed-STOP/checkpoint bundle. Missing proof is never replaced with a
synthetic or edited cartridge identity. Public host portability adaptations have
offline coverage and have not themselves been requalified on hardware.
