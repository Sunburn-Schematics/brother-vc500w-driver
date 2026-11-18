# Building and updating the cartridge firmware

The current target is the classic ESP32 DOIT DevKit V1, using the pinned Arduino
ESP32 2.0.17 core. The firmware accesses that chip's I²C and GPIO registers
directly; selecting a similarly named ESP32-C3, S2 or S3 board does not make it
compatible. Read the [cartridge history](cartridge-history.md) for what has been
tested and what remains experimental.

**Fresh-board limit:** building and flashing V12 is supported; loading an
arbitrary new cartridge image through the V12 host manager is not a finished
public workflow. The hardware experiment loaded its evidence-pinned image with
V6, then preserved the same NVS image through later application-only updates.
The V12 manager provides status, diagnostics, start/stop, pullup control and
the original narrowly pinned recovery operation. A fresh V12 installation stays
unloaded and passive. Historical loaders and recovery commands retain their
original version/provenance guards; a synthetic test fixture is not a usable
cartridge identity. Do not remove those guards to claim fresh provisioning.

## Pinned environment

| Component | Version or setting |
| --- | --- |
| Arduino CLI | `1.6.0` |
| Arduino board package | `esp32:esp32@2.0.17` |
| Underlying ESP-IDF | `4.4.7`, supplied by that board package |
| FQBN | `esp32:esp32:esp32doit-devkit-v1` |
| CPU | 240 MHz, checked by the firmware and host tools |
| UART | 921600 baud |
| Python | 3.11 or later |
| Python UART dependency | `pyserial==3.5` |
| Native model-test compiler | C++17 with AddressSanitizer and UndefinedBehaviorSanitizer |

The sketch uses libraries supplied by the pinned ESP32 core. No separately
installed Arduino libraries are required. The two OTA application slots in the
tested partition layout are 1,310,720 bytes each. Keep an OTA-capable partition
layout; replacing it with a large single application partition breaks the update
assumptions.

## Build

Run these commands from the repository root on Linux x86-64 with Python 3,
`make` and `g++`. The bootstrap downloads and checksum-checks the official CLI
archive with Python, then installs the pinned board package locally:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r cartridge/requirements.txt
make -C cartridge bootstrap
make -C cartridge firmware
```

Other hosts can supply an installed official Arduino CLI 1.6.0 executable using
`ARDUINO_CLI=/absolute/path/to/arduino-cli` on both Make commands. This option
does not change the required board package or FQBN. Windows PowerShell users
can run the underlying Python helper directly; the firmware build does not
require the Windows-only printer USB helper.

The default sketch is `eeprom_emulator_v12`. Build products and
`build-receipt.json` are written to `cartridge/.build/eeprom_emulator_v12/`;
the local configuration is `cartridge/.cache/arduino-cli.yaml`. To build an earlier experiment,
select its directory explicitly, for example the V4 passive sniffer:

```sh
make -C cartridge firmware SKETCH=bounded100_i2c
```

The bootstrap keeps its toolchain configuration local to the project. Inspect
the Makefile and bootstrap script before running them. The build compiles source;
it neither flashes an ESP32 nor starts a cartridge responder.

Run the offline checks separately:

```sh
make -C cartridge test
node --test runtime/printer-driver.test.cjs
```

The public tests use synthetic EEPROM fixtures and loopback printer responses.
Historical replay tests that require omitted private proof bundles are reported
as skipped unless those bundles are supplied locally. A passing synthetic test
does not establish that a real cartridge image or a hardware transport is valid.

For an existing Arduino CLI installation, the equivalent board selection is:

```sh
arduino-cli core install esp32:esp32@2.0.17 \
  --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli compile \
  --fqbn esp32:esp32:esp32doit-devkit-v1 \
  --build-path /absolute/path/to/fresh-build \
  cartridge/firmware/eeprom_emulator_v12
```

Use a fresh build path when retaining an earlier build as evidence. Record the
application binary's SHA-256 and the ELF file's SHA-256. The compiled ESP32
application descriptor embeds the ELF identity, which `NETINFO` exposes after
boot. A rebuild in a different path or tool environment can change the ELF and
application hashes; the original experiment's hashes are not a substitute for
hashing your own output.

## Initial serial flash

Initial flashing installs the bootloader, partition table and application. It
can require holding BOOT during the reset/download handshake on this board.
Disconnect the cartridge bus for that first installation. The initial image
starts passive; an upload is not permission to assume the board is responding
at address `0x50`.

Use the pinned Arduino CLI's upload operation with the same FQBN and the exact
serial device belonging to your board:

```sh
cartridge/.cache/bin/arduino-cli \
  --config-file cartridge/.cache/arduino-cli.yaml upload \
  --fqbn esp32:esp32:esp32doit-devkit-v1 \
  --port YOUR_SERIAL_PORT \
  --input-dir /absolute/path/to/fresh-build \
  cartridge/firmware/eeprom_emulator_v12
```

That executable path is the Linux bootstrap default. Use your explicitly
supplied CLI executable on other hosts, with the same local configuration file.

An application-only OTA image is not a merged whole-flash image. Do not write an
application binary at flash offset zero. Repartitioning or erasing flash can
destroy stored network settings and EEPROM checkpoints; normal later updates
use an inactive application slot instead.

## Later updates over USB

The management helper checks the selected CP210x UART, exact running firmware
revision and passive pin configuration. Supply your board's serial identity
and a new audit filename. Stop emulation, retain the full current snapshot,
verify that its live generation has been checkpointed, and confirm GPIO19's
pullup is off before updating.

```sh
python3 cartridge/tools/manage_esp32.py netinfo \
  --port YOUR_SERIAL_PORT --serial-number YOUR_USB_SERIAL \
  --expect-firmware i2c-eeprom-emulator-v12 \
  --audit /private/path/netinfo-new.jsonl

python3 cartridge/tools/manage_esp32.py update \
  --port YOUR_SERIAL_PORT --serial-number YOUR_USB_SERIAL \
  --expect-firmware i2c-eeprom-emulator-v12 \
  --app /absolute/path/to/fresh-build/eeprom_emulator_v12.ino.bin \
  --sha256 YOUR_COMPILED_APP_SHA256 \
  --audit /private/path/usb-update-new.jsonl
```

`--expect-firmware` identifies the image running **before** the update. The USB
protocol bounds the application size, checks the application descriptor,
acknowledges each chunk, and verifies the complete SHA-256 before selecting the
new slot. It does not erase the EEPROM namespace. After reboot, probe again and
compare the reported application ELF hash with the build you installed. Preserve
failed-upload evidence; the helper does not automatically retry an uncertain
operation.

## Wi-Fi provisioning and OTA

Wi-Fi is disabled while emulation or capture is active. To update over Wi-Fi,
first STOP and checkpoint over USB, then let the passive firmware restore its
network connection. Native Tailscale is not part of this firmware: run the OTA
client on a computer that can reach the ESP32's LAN, optionally reaching that
computer through an existing remote-access service.

Create a private UTF-8 JSON file outside the repository containing exactly:

```json
{
  "ssid": "YOUR_WIFI_SSID",
  "password": "YOUR_WIFI_PASSWORD",
  "ota_password": "YOUR_SEPARATE_LONG_OTA_PASSWORD"
}
```

The OTA password must be 16–128 UTF-8 bytes. On Unix, the helper requires a
regular file owned by the current user with mode `0600`; on Windows it verifies
the file's ACL. Credentials are read from the file rather than command-line
arguments. Keep this file, NVS dumps and generated private audit data out of Git.

Provision over the already verified UART:

```sh
python3 cartridge/tools/manage_esp32.py wifi-set \
  --port YOUR_SERIAL_PORT --serial-number YOUR_USB_SERIAL \
  --expect-firmware i2c-eeprom-emulator-v12 \
  --secrets /private/path/esp32-wifi.json \
  --audit /private/path/wifi-provision-new.jsonl
```

Provisioning reboots the board. Re-probe it before an update. OTA uses the
unmodified `tools/espota.py` supplied with Arduino ESP32 2.0.17; the management
helper verifies that script's exact hash before loading it:

```sh
python3 cartridge/tools/manage_esp32.py ota \
  --app /absolute/path/to/fresh-build/eeprom_emulator_v12.ino.bin \
  --sha256 YOUR_COMPILED_APP_SHA256 \
  --secrets /private/path/esp32-wifi.json \
  --espota-script /path/to/esp32/2.0.17/tools/espota.py \
  --target ESP32_LAN_IP --local-ip OTA_CLIENT_LAN_IP \
  --audit /private/path/ota-update-new.jsonl
```

The client must be reachable for the OTA return connection; choose its actual
LAN interface, not an unrelated VPN interface. ArduinoOTA authentication does
not provide encrypted transport. Use it on a trusted network. The updater
validates immutable application bytes before invoking the pinned sender, then
the next UART probe establishes which application actually booted.

## Keep build, bus and print evidence separate

A successful compile establishes source/toolchain compatibility. It does not
qualify wiring, EEPROM contents, I²C timing, physical tape feed or print quality.
The host tools save complete byte images and bounded UART diagnostics, and
require explicit serial, firmware, ELF and image identities for device actions.
See [the tool usage guide](../cartridge/tools/README.md) for their exact command
contracts. Historical fault-recovery commands are tied to the original evidence
and must not be generalized into an erase, reset or arbitrary counter repair.

The printer should have one print owner. Do not run the repository's standalone
printing examples concurrently with a desktop application that already owns the
printer connection.
