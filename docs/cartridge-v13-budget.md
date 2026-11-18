# V13: a virtual budget at cartridge initialization

**Eleven labels printed with V13: ten had verified owner completion, and the
user confirmed the final label printed completely after its status timeout.**
At 17:02:26 UTC on 18 November 2026, a fresh uncached printer status reported
`IDLE`, `READY FOR PRINT`, error `NONE`, and 8915.4 mm initial and remaining
length with the V13 responder. See the [redacted initialization and print
evidence](../cartridge/evidence/v13-initialization-validation.json).
The existing V12 twelve-print result remains a separate historical test. Neither
run establishes unlimited printing or complete-roll endurance.

The [existing experiment](cartridge-emulation.md),
[protocol measurements](cartridge-protocol.md) and
[build guide](cartridge-build.md) remain the starting points. V13 uses the same
classic ESP32 target, core version and sole-responder wiring.

## Why a new policy needs initialization

The printer caches remaining length in RAM. In the analyzed Brother engine, its
normal usage-update path writes that cached value to the cartridge and compares
every byte of the following 16-byte readback. That readback does not reload the
RAM counter. Returning a high remaining value immediately would break the
comparison; changing EEPROM bytes after verification would leave the cached RAM
value unchanged.

V13 therefore provides a **bounded budget per initialization**, rather than
claiming to refill the cached counter after each print. Explicit passive enable
prepares a separate advertised image. An observed pointer-zero read epoch can
refresh that view; a completed 256-byte initialization read is counted separately.
Pointer-zero traffic alone does not prove a new physical roll was installed.
There is no automatic D19 toggle, between-job rescan or post-verification refill.

## Backing image and advertised view

The 256-byte backing image remains the exact mirror of qualified printer writes.
The host saves its complete pre-enable bytes and hash. The advertised view is a
separate image with a configurable virtual initial and remaining count, bounded
to **1–11232 counts**. The default maximum is 11232, or nominally 8915.4 mm in the
observed non-wristband format. This is a software budget, not a measurement of
physical tape.

At enable, only these offsets can differ from the backing image:

| Offset | Advertised value |
| --- | --- |
| `60..61` | Virtual initial count, little endian |
| `AE..AF` | R0 CRC for the modified initial count |
| `E8..E9` | Virtual remaining count, little endian |
| `F6..F7` | RW2 CRC for the modified remaining count |

The host independently constructs the expected full view and recomputes its
CRCs. It checks that every backing byte and its generation remain unchanged.
Ordinary usage writes subsequently update both mirrors faithfully, including
the intermediate invalid CRC between the two observed eight-byte page writes.
Verification reads receive those exact bytes. Refresh is blocked while write
verification is pending.

Disable retains the newest backing image. It does not restore the original seed
or undo the printer's usage writes. A remaining count written while the virtual
budget was active can exceed the backing image's original initial-count field;
turning the policy off does not restore measured physical-roll accounting.
Retain original, post-use and checkpoint images as separate evidence.

## Explicit controls

The candidate is [`eeprom_emulator_v13`](../cartridge/firmware/eeprom_emulator_v13/),
with the matching [V13 host helper](../cartridge/tools/manage_emulator_v13.py).
The existing default build remains V12. Select V13 explicitly after obtaining
the pinned toolchain described in the build guide:

```sh
make -C cartridge firmware SKETCH=eeprom_emulator_v13
```

The host adds `policy-enable`, `policy-disable` and `recover-stop` to the existing
explicit emulator controls. Policy changes require a closed, clean, passive
checkpoint with D19 OFF, a complete valid backing image, and exact USB serial,
firmware, ELF, image SHA-256 and generation pins. For example:

```sh
python cartridge/tools/manage_emulator_v13.py policy-enable \
  --port YOUR_SERIAL_PORT --serial-number YOUR_CP210X_SERIAL \
  --expect-elf-sha256 YOUR_COMPILED_ELF_SHA256 \
  --image-sha256 YOUR_CURRENT_BACKING_SHA256 --durable-generation YOUR_GENERATION \
  --capacity-counts 11232 --output /private/path/new-enable-evidence
```

This command changes policy on the selected ESP32 when run. It does not start
the responder, print a label or establish printer acceptance. Read the resulting
receipt before a separate START. `status` saves both the complete backing image
and advertised image, including their separate generations. Use the latest
backing hash and checkpoint generation when disabling later.

V13 policy is volatile and defaults OFF after reboot. Application-only updates
retain the compatible backing-image checkpoint, not an active policy setting.
The public manager still has no generic fresh-image LOAD operation. Historical
provisioning and recovery tools retain their exact version/provenance guards.

`recover-stop` is an archival, metadata-only recovery for one recorded failed
STOP and its independently verified closed checkpoint. It keeps that STOP's
`ok:false` result and the possibility of uncommitted bus activity in its evidence.
It requires the original private proof bundle, which is not distributed here.
It is not a general fault-clear or counter-reset command.

## Test and evidence boundaries

The Python tests use synthetic images and fake UARTs. Two original-event replay
cases require `BROTHER_PRIVATE_REPLAY_DIR`; they are explicitly skipped when the
private bundle is absent. Native C++ tests exercise the policy, CRC projection,
ordinary write/readback behavior, initialization refresh and checkpoint guards
without an ESP32 or printer. They do not qualify peripheral timing.

The live V13 check retained the existing generation-43 backing checkpoint,
completed the separate metadata-only STOP recovery, enabled the 11232-count view,
and started the sole responder without a reported fault. The printer accepted
the advertised initialization values. Ten subsequent user-submitted website
jobs, each with one copy and a half cut, reached verified completion and settled
idle. The tenth label was visible at the output in the camera view.

The eleventh job received a submission ACK and reached `PRINTING` and `EJECTING`,
but status feedback then disappeared. The owner timed out after about 60 seconds
and recorded completion as uncertain. It did not replay the label. The user
subsequently confirmed that this final label printed completely. That human
confirmation establishes physical output; it does not change the saved owner's
uncertain result or supply missing usage-write and completion telemetry.

The post-run emulator snapshot had no fault and valid CRCs in both full images.
Its counters matched ten normal write/readback cycles: backing generation rose
from 43 to 63, with ten verification reads and no additional refresh. Remaining
count was 10868. The responder was still active and dirty, with durable generation
43: the latest twenty page writes were in RAM, not a completed flash checkpoint.
Policy remained enabled and volatile; it still defaults OFF after reboot.
A later complete emulator sample retained the same bytes, counters and no-fault
state.

A later owner status was disconnected/`UNKNOWN` with an idle queue. A supported
Wi-Fi reconnect timed out, and a subsequent ping received no reply. The cause of
the printer/network stall remains unresolved. These observations do not establish
a Wi-Fi-only root cause or an emulator failure. The camera still showed an
attached strip. At 19:07:56 UTC, the owner was still disconnected/`UNKNOWN`, its
queue was idle, and no additional print jobs had been recorded. The desktop
service was responding. A later requested batch encountered a reconnect error;
it is not another demonstrated print run. Status-channel recovery, the final
label's missing usage-write telemetry and a clean detach/checkpoint remain
separate work.

Physical media-end sensing, full-roll use, arbitrary bus masters, interrupted
transactions and EEPROM-like power-loss persistence remain separate tests.
A virtual counter cannot create tape, repair a jam or establish that a roll is
feedable. Stop and checkpoint before OTA or power removal, and disable D19
before switching the printer off.
