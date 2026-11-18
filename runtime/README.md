# Standalone runtime driver used in the cartridge experiment

This directory contains the printer driver used by the desktop owner during the
November 2026 ESP32 cartridge experiment. It requires Node.js built-ins and the
included `usb-printer-io.cs`; it does not require Electron. The two source files
are byte-identical to the reviewed runtime copies. [provenance.json](provenance.json)
records their hashes and origin. The repository's root `driver.js` is an older
API retained for its existing examples.

Use this module only when your process is the sole printer owner. If an existing
desktop service owns the printer, use that service's supported API rather than
opening another TCP or USB connection. This module is not an HTTP server and
does not include the website's queue, authentication or job storage.

## Wi-Fi API

Use Node.js 22 or later for the documented test command. Wi-Fi printing uses
raw TCP port 9100. Supplying a host explicitly and calling `connectWifi()` avoids
automatic discovery and USB probing:

```js
const fs = require('node:fs');
const { VC500WDriver } = require('./runtime/printer-driver');

async function main() {
  const host = process.env.PRINTER_HOST;
  if (!host) throw new Error('Set PRINTER_HOST to the printer you own');
  const printer = new VC500WDriver({ host, port: 9100 });
  try {
    await printer.connectWifi(host, 9100);
    console.log(await printer.getConfig());
    console.log(await printer.getPublicStatus());
    const result = await printer.printJpeg(fs.readFileSync('label.jpg'), {
      quality: 'vivid',
      cutMode: 'half'
    });
    console.log(result);
    if (!result.completed) {
      // Inspect the physical output and saved result before deciding what next.
      process.exitCode = 1;
    }
  } finally {
    printer.close();
  }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
```

The example submits one actual print when run. Reading configuration/status alone
does not require loading an image or calling `printJpeg()`.

The runtime constructor and lifecycle differ from the older root driver:

| Operation | Runtime API |
| --- | --- |
| Explicit TCP connection | `await printer.connectWifi(host, port)` |
| Explicit Windows USB connection | `await printer.connectUsb()` |
| Automatic discovery | `await printer.connect()`; can probe network and USB |
| Configuration | `await printer.getConfig()` |
| Detailed public status | `await printer.getPublicStatus()` |
| Single JPEG | `await printer.printJpeg(buffer, options)` |
| Batch | `await printer.printBatch(labels, options, cancelToken)` |
| Release connections | `printer.close()` |

Batch entries are objects containing `jpegBuffer`, with optional `cutMode`,
`imgWidth` and `imgHeight`. Batch quality comes from `options.quality`, not an
individual entry. Batch options also include `finalCutMode`;
set it explicitly if the final label should also use a half cut. The batch
`cancelToken` is an object whose `cancelled` flag can be set by the owning
application. Cancellation is cooperative between operations; it does not prove
an image already accepted by the printer stopped printing.

## Completion and uncertainty

The completion protocol identifier is `vc500w-idle-v1`. A print-data ACK means
the printer accepted data. For the tested Wi-Fi path, `success` is returned only
after the driver observes activity and two healthy idle samples with stage
`SUCCESS` and error `NONE`. Activity may be a qualified printing/feed stage or
a sufficient decrease in reported tape remaining. The result preserves observations,
`completed`, `verified`, `uncertain`, tape readings and a reason.

The driver retains the observed close/reconnect sequence for cutting. It can
reconnect the status channel within the original completion deadline. It does
not resend an image through another channel once submission may have reached
the printer. A timeout or lost response must be handled as an uncertain outcome,
not an instruction to retry automatically.

Status can be cached while printing or after a failed refresh. Check the returned
freshness and communication fields before treating a status value as a new
hardware observation. The parser distinguishes the initial read ACK from the
following configuration/status document. It is a bounded-purpose XML text parser,
not a general binary-file reader for undocumented printer paths.

## Windows USB

The C# helper uses Windows `usbprint.sys` device discovery and direct device I/O
through PowerShell. It is loaded from this directory, so keep the sibling file
beside the JavaScript module. Some USB responses are unreliable; physical print
completion needs the available status channel. The twelve-print cartridge run
used Wi-Fi image submission, not a qualification of every USB path.

## Offline regression tests

```sh
node --test runtime/printer-driver.test.cjs
```

The tests use synthetic status/configuration documents, stubbed submissions and
loopback TCP servers bound to `127.0.0.1`. They do not discover printers, invoke
PowerShell, open a USB device or send a physical print. The completion tests use
scaled clocks; they exercise reconnect deadlines, stale responses, idle without
activity, printer errors and connection ownership. Separate cases cover
fragmented ACK/payload framing and refusal to replay an uncertain submission.

Electron-specific request-admission and IPC tests were omitted with the desktop
application; this standalone test command makes no claim about that application.
The existing repository [license](../LICENSE) applies; third-party credits in
the source are retained.
