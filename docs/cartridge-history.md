# Cartridge EEPROM development history

The cartridge project began as an investigation into how the VC-500W accounts
for tape, then became an ESP32 implementation of the cartridge's 256-byte I²C
memory. The host-side JPEG/XML printer driver and the cartridge-side ESP32 are
separate: the driver submits images; the ESP32 answers the printer's cartridge
bus. Changing the advertised tape count does not create physical tape or fix a
jam.

This history covers the November 17–18, 2026 experiments. The tested printer
reported firmware `2022071322`; static analysis also used the public Brother
upgrade archive `2022071302`. Findings from that archive retain this revision
limit. The target was a classic ESP32 at 240 MHz, built with Arduino ESP32
`2.0.17`, rather than every member of the ESP32 family.

## Establishing the memory image

Passive captures and two identical isolated-bench reads established an exact
256-byte image. A read-only inspector independently checked three CRC regions.
The CRC is CRC-16/X-25: reflected polynomial `0x8408`, initial value `0xffff`,
final XOR `0xffff`; the standard ASCII `123456789` check is `0x906e`.

| Region | CRC input, inclusive | Stored CRC, little endian |
| --- | --- | --- |
| R0 | `0x00–0xad` | `0xae–0xaf` |
| RW1 | `0xd0–0xe5` | `0xe6–0xe7` |
| RW2 | `0xe8–0xf5` | `0xf6–0xf7` |

The initial-length field is a little-endian 16-bit value at `0x60`; remaining
length is at `0xe8`. In the analyzed non-wristband path a count is 1/32 inch
(`0.79375 mm`). The archived driver treats revision byte `image[1] == 3` as
wristbands with a band count, so the millimeter conversion is not universal.
Unknown fields were preserved rather than assigned invented meanings.

A controlled physical-chip experiment changed only the two remaining-count
bytes and their two CRC bytes. Two complete readbacks matched the planned
image. With that chip and a fresh physical roll, the printer printed and ejected
a label normally, and the user confirmed the result. A later capture showed
the printer's normal two eight-byte RW2 writes and its 16-byte readback. The
emulator seed retained that observed consumption instead of silently resetting
the counter again.

## Firmware revisions and lessons

Each retained sketch represents an experiment, not an equally qualified release.
V12 is the current implementation. Historical sketches are useful for explaining
why a simpler approach was insufficient.

| Version | Purpose and result |
| --- | --- |
| V1 | Passive GPIO edge interrupts with a bounded buffer. Real captures missed transitions; they were not treated as complete EEPROM dumps. |
| V2 | Added authenticated ArduinoOTA, private USB Wi-Fi provisioning, and a hashed USB application updater. Capture behavior remained the same. |
| V3 | Added direct GPIO/cycle-counter polling in bounded IRAM windows, with sampling gaps reported explicitly. A build that inlined the hot loop into flash was rejected and rebuilt with a separate IRAM function. |
| V4 | Extended the triggered polling tail to 100 ms. This captured a complete 256-byte startup read with final NACK and STOP; all three memory CRCs agreed. |
| V5 | Added an isolated, open-drain bench transport. It required matching complete reads before a narrowly planned counter restore and verified the full postimage afterward. The printer was isolated from both bus signal lines for this operation. |
| V6 | First hardware-slave emulator: RAM mirror, page writes, audit metrics, passive boot and explicit checkpoints. The first attachment failed before bus activity. Missing or inadequate SDA pullup remained an electrical issue to resolve. |
| V7 | Added attachment-stage diagnostics and narrowly scoped recovery for an unchanged V6 image. An attempted START timed out; preserved evidence did not establish that no activity or reset had occurred. |
| V8 | Added a detached, one-shot peripheral diagnostic and bounded ISR fault guards. On the actual board, RX threshold zero produced an idle pending RX interrupt with an empty FIFO. A final pin-matrix readback issue was also exposed. These observations were retained as failures. |
| V9 | Used RX threshold one and verified detached pin matrices. START remained active without an idle interrupt storm. Restoring SDA high triggered printer initialization without a printer restart, but the pointer transaction exposed a stale RX interrupt after draining the FIFO. |
| V10 | Drained RX before clearing its interrupt and added GPIO19's switched pullup through 4.7 kΩ to SDA. The printer completed its startup read and reached READY. A comparison between completed bytes and interrupt count then incorrectly faulted the read. |
| V11 | Counted bytes dequeued at the terminal NACK rather than equating interrupt count with byte count. A calibration label physically printed and ejected. The first eight-byte write was rejected by another unqualified interrupt-count assumption; its discarded payload had not been retained. |
| V12 | Uses the observed ACK/STOP conditions for the printer's writes, retains rejected-page evidence, and records the last four committed pages when detached. Real label printing exercised the normal two-page write and readback path without the earlier faults. |

## What V12 demonstrated

During the observed user print run, the printer successfully printed twelve
labels. The emulator's live generation advanced from 1 to 25, consistent with
24 accepted page commits. Its remaining count decreased from 6256 to 5821.
A complete post-run RAM snapshot passed all three memory CRC checks. The
recorded run had no RX overflow, TX underflow, FIFO accounting error, invalid
STOP or ISR-storm fault.

These observations qualify the startup 256-byte read and the normal printer
write/readback sequence on this setup. They do not establish every possible
EEPROM transaction. Generic short reads, unusual current-address reads, malformed
mid-byte STOPs, different printer revisions and a full cartridge's endurance
remain separate tests.

The emulator deliberately preserves the printer's writes, including the brief
interval between the first and second page when the RW2 CRC may be invalid.
It does not repair every intermediate CRC or continuously replenish remaining
length. A CRC-valid snapshot is evidence about the bytes, not evidence that
physical tape remains available.

## Persistence and interrupted experiments

During active emulation, accepted writes update RAM and increment the live
generation. Flash persistence occurs after explicit STOP detaches the responder
and checkpoints the complete image. The post-run observation above still had
live generation 25 and durable generation 1: it was evidence of RAM updates,
not a claim that all updates had already reached flash.

The firmware uses complete-image checkpoints, generation and outer-CRC checks,
and an active-session marker. A reboot with an unfinished session starts
passive and reports uncertainty. OTA is also a reboot: stop and save first.
Losing power before checkpointing can lose recent accepted writes. The firmware
does not promise EEPROM-equivalent persistence within a 5 ms write cycle.

Earlier failures remain part of the evidence. The V11 discarded write payload
could not be reconstructed from rounded printer status; one calibration's
33.782 mm of owner-reported tape usage was recorded as unresolved usage rather than
invented EEPROM bytes. The V7 timeout similarly left pre-reset volatile history
unknown. The historical recovery commands acknowledge only their exact,
evidence-pinned cases and are not general counter-reset commands.

## Printing and physical handling

A print-data ACK confirms acceptance, not physical completion. The tested host
path separately observed print activity followed by stable successful idle
status, with physical output confirmation where available. Lost responses were
not grounds to resend the same label automatically.

The intended batch workflow uses half cuts between labels and keeps the backing
strip continuous until the user makes the final cut and removes it. A loose
label at the output and an actual empty roll both produced mechanical problems
during development. Neither observation proves a counter or emulator defect.

The project enables controlled inspection, accounting experiments and cartridge
emulation. Printing an entire roll, or choosing an advertised length that uses
all physically available media, remains an endurance and feed-path measurement
to document with an actual complete roll.
