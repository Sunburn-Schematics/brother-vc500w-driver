# VC-500W cartridge research source

Start with the [illustrated experiment](../docs/cartridge-emulation.md),
[protocol measurements](../docs/cartridge-protocol.md), and
[build and update guide](../docs/cartridge-build.md).

The current experimental firmware is
[`firmware/eeprom_emulator_v12`](firmware/eeprom_emulator_v12/).
The earlier firmware directories are development history, including prototypes
with known failures. Their versioned host tools intentionally check matching
firmware identities. Do not weaken those checks to combine arbitrary versions.

[V13's initialization-budget policy](../docs/cartridge-v13-budget.md) has passed
its live initialization check. Eleven labels printed: ten had verified owner
completion, and the user confirmed the final label after its status timeout.
The saved final owner result remains uncertain; the linked guide records the
limits.
Its source and offline tests are included separately. V12 remains the default.

[V14](../docs/cartridge-v14-recovery.md) adds recovery of one exact saved STOP
checkpoint. Live metadata recovery and printer initialization passed; new V14
prints remain pending.

| Directory | Contents |
| --- | --- |
| `firmware/` | Authored passive sniffers, isolated EEPROM tools, updater, V12 baseline and V13 policy candidate |
| `tools/` | UART management, capture decoding, checksum/image inspection and printing diagnostics |
| `tests/` | Offline native regression harnesses and public test support |
| `evidence/` | Aggregate twelve-print observations and disclosure of measurement limits |

The [standalone print runtime](../runtime/) sends normal image jobs. Cartridge
emulation does not replace that host-to-printer driver, and it is not a
transparent bridge to another EEPROM on the same bus.

## Prototype boundaries

The successful run used an already provisioned EEPROM image. The frozen V12 host
helper does not provide a generic fresh-install image loader. Historical tools
loaded the image under V6, then firmware updates retained its compatible NVS
state. Read the build guide before attempting provisioning; no full cartridge
image or universal cartridge identity is supplied here.

Some recovery commands require the exact private evidence bundle from a
particular failed experiment. Publishing their code does not make them general
reset commands. Private proof bundles, credentials, raw device flash, downloaded
SDKs and Brother firmware binaries are excluded.

V12 completed twelve real half-cut jobs with no emulator faults and valid final
checksums. At the end, the new counter state remained in RAM: live generation 25,
durable generation 1, dirty true. Full-roll endurance and active power-loss
persistence have not been demonstrated. This source publication does not include
prebuilt binaries or claim a production-ready cartridge replacement.

Use the repository's existing [noncommercial license](../LICENSE). Third-party
dependencies must be obtained from their upstream projects under their own terms.
