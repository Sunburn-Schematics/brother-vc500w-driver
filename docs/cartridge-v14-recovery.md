# V14: recovery of one saved STOP checkpoint

**Live V14 metadata recovery and printer initialization passed. New V14 print
jobs remain pending.**
The [V13 experiment](cartridge-v13-budget.md) produced eleven physical labels,
with ten owner-confirmed completions and subsequent human confirmation of the
final label. Its last saved owner result and usage telemetry remain distinct.
V14 does not change that history or establish the cause of the later
printer/network stall.

V14 changes the firmware identity and the exact checkpoint accepted by the
existing metadata-only recovery command. It does not change the EEPROM model,
I2C interrupt handling, STOP behavior, D19 control, updater or virtual-budget
policy. V12 remains the default build; V12 and V13 sources are preserved.

## The particular recovery event

A later V13 STOP returned `ok:false`, `detached_saved_with_fault`, fault 9
(`stop_timeout`). Its complete before/after backing images were identical,
and the final saved state was closed, clean and durable at generation 63 with
D19 OFF. A subsequent detached diagnostic passed while retaining that fault,
checkpoint and image. These are separate observations: a verified saved
checkpoint does not turn the failed STOP into a successful operation or exclude
uncommitted bus activity.

`EMU_RECOVER_STOP_CHECKPOINT <sha256> <generation>` keeps the same command and
result framing. V14 accepts only that generation-63 image under the existing
closed/passive, exact-preimage, valid-CRC, D19-OFF and fault-9 guards. Policy must
be OFF. The new firmware defaults to that state after boot; it does not restore
an active policy setting. The earlier generation-43 recovery pin is rejected.
Recovery clears fault metadata through a verified checkpoint. It does not change
any cartridge byte, counter or generation. A checkpoint failure remains an
uncertain outcome, with no automatic retry or claimed rollback.

The host saves the full current preimage, exact historical proof and recovery
plan before sending one command, then verifies the complete postimage and
metadata. Its proof bundle contains the actual six STOP raw frames and twelve
detached-diagnostic raw frames, full images, receipts, intents and audit records.
The public source includes the verifier, not those private proof files or a
cartridge identity. `recover-stop` is therefore an archival event-specific
operation, not a general clear, loader or reset command.

## Live recovery and rearm

The installed V14 firmware retained the complete generation-63 backing image
and booted passive with policy OFF. The explicit recovery cleared fault 9 while
preserving every backing byte, counter and generation. Its new result is a
successful metadata recovery, not a reinterpretation of the earlier failed STOP.

Explicit policy enable then prepared a 11232-count advertised view while the
backing checkpoint remained unchanged. START with the controlled D19 pullup
succeeded. The subsequent complete 256-byte initialization read, terminal NACK
and STOP were observed without an emulator fault. Backing live and durable
generations remained 63, clean; view generation was 65, with one initialization
read and two refresh events. No print-use write was recorded in this sample.

Two fresh uncached owner samples at 19:26:28 and 19:26:31 UTC on 18 November
2026 reported `IDLE`, `READY FOR PRINT`, error `NONE`, recognized 3/4-inch media,
and 8915.4 mm initial and remaining virtual length. This establishes printer
acceptance after rearm. New V14 prints and a later post-use checkpoint remain
separate tests.

Unplugging and restoring printer power restored its Wi-Fi connection before
rearm; its power button had not responded. Neither that observation nor clearing
the emulator metadata proves the cause of the printer stall or STOP timeout.
V14 introduces no STOP/ISR fix and does not add an eleventh observed V13 usage
cycle. The original V13 evidence and its limits remain intact.

## Source and offline tests

The candidate is [`eeprom_emulator_v14`](../cartridge/firmware/eeprom_emulator_v14/),
with the matching [V14 host helper](../cartridge/tools/manage_emulator_v14.py).
Use the pinned toolchain in the [build guide](cartridge-build.md), selecting
V14 explicitly:

```sh
make -C cartridge firmware SKETCH=eeprom_emulator_v14
```

The public helper removes only the laboratory's fixed port and serial-number
choices. Explicit device, CP210x, serial, firmware, ELF, image and generation
guards remain. It does not contact hardware on import. Four original-event test
cases require the existing `BROTHER_PRIVATE_REPLAY_DIR` opt-in; synthetic policy
and fake-UART regressions run without it. The new native tests use synthetic
bytes and the same relative-include/runner convention as V13. No SDK, vendor
firmware, executable image, credentials, raw flash or full real EEPROM image is
redistributed.

See the [redacted V14 validation record](../cartridge/evidence/v14-recovery-validation.json).
The recorded live recovery and initialization are separate from offline test
success and the binary audit. They do not demonstrate new V14 printing or a
durable post-use checkpoint.

The V13 limits continue to apply: the virtual budget is bounded per
initialization, the printer caches remaining length in RAM, accepted writes
must read back exactly, policy is volatile/default OFF, and no automatic rescan
or post-verification refill is introduced. D19 must be OFF before printer power
removal. Full-roll endurance and active power-loss durability remain unqualified.
