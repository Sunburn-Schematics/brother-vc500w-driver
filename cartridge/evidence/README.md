# Cartridge experiment evidence

[v12-twelve-print-validation.json](v12-twelve-print-validation.json) is a public
aggregate of the 18 November 2026 UTC experiment (17 November PST). It retains the tested versions,
actual counter and generation changes, CRC results, cumulative transport counters
and twelve redacted owner job outcomes. It omits the full EEPROM dump, part list,
job IDs, printer serial, network addresses, credentials and private host paths.

Source file SHA-256 values refer to unchanged private evidence. The public JSON
is a derived summary, not a byte-for-byte copy of those files or a substitute for
the firmware's experiment-specific recovery proof bundles. The original raw
serial frames were saved before parsing; all four final frame/raw pairs matched,
the 256-byte snapshot matched its hash, and all three CRCs were recomputed during
independent review.

The final sample remained active and dirty: live generation 25, durable generation
1. It demonstrates RAM updates during successful printing, not a completed flash
checkpoint or power-loss durability. No whole-cartridge endurance result is
included. Any later checkpoint result must be recorded separately.

[The wiring diagram](../../images/cartridge-wiring.svg) shows the reported tested connections,
not a photograph or an inferred pin numbering for every cassette PCB. Match
signals and verify voltage on the actual hardware before using another board.

Template source: the repository's existing protocol-captures documentation and
the experiment's versioned validation-receipt structure. Existing licensing is
unchanged; third-party firmware, SDK archives and vendor artwork are not copied.
