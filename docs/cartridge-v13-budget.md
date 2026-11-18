# V13: a virtual budget at cartridge initialization

**Live V13 initialization was accepted; V13 print verification is pending.**
At 17:02:26 UTC on 18 November 2026, a fresh uncached printer status reported
`IDLE`, `READY FOR PRINT`, error `NONE`, and 8915.4 mm initial and remaining
length with the V13 responder. See the [redacted initialization
evidence](../cartridge/evidence/v13-initialization-validation.json).
V12's recorded twelve-print result remains the completed print baseline. Neither
result establishes unlimited printing or complete-roll endurance.

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

The first live V13 check retained the existing generation-43 backing checkpoint,
completed the separate metadata-only STOP recovery, enabled the 11232-count view,
and started the sole responder without a reported fault. The printer then
accepted the advertised initialization values. This does not yet establish that
new V13 print jobs complete, their usage writes verify, or a later checkpoint
succeeds. Those results remain pending. Record the exact job outcomes,
write/readback and generation changes, complete-image CRCs, faults, physical
output and eventual checkpoint separately. Native test results cannot fill
those fields.

Physical media-end sensing, full-roll use, arbitrary bus masters, interrupted
transactions and EEPROM-like power-loss persistence remain separate tests.
A virtual counter cannot create tape, repair a jam or establish that a roll is
feedable. Stop and checkpoint before OTA or power removal, and disable D19
before switching the printer off.
