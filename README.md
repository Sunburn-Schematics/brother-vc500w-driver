# Brother VC-500W Driver & Protocol Documentation

Open-source driver and complete protocol reference for the **Brother VC-500W** color label printer. Works over **WiFi (TCP)** and **USB** on Windows, with WiFi support on any platform with TCP sockets.

<!-- Uncomment when images are added:
![Printer in action](images/printer-in-action.jpg)
-->

## Why This Exists

If you've ever prototyped with SMD components, you know the pain. You've got dozens of tiny WENTAI boxes full of 0402 capacitors, 0603 resistors, SOT-23 transistors — and they all look identical. You're squinting at faded factory labels, cross-referencing Digi-Key order sheets, and losing 10 minutes every time you need a 4.7uF cap because it's buried in a box that says "CL05A475MP5NRNC" in 4-point font.

The Brother VC-500W with the **3/4" (19mm) CZ-1003 tape** or **1" (25mm) CZ-1004 tape** solves this. Print full-color labels with the part number, value, package size, and even a thumbnail of the schematic symbol — stick it right on the WENTAI box. Now you can actually find what you need while you're in the middle of hand-soldering a prototype board.

The problem? Brother only ships a locked-down Windows/Mac app with no API, no Linux support, and zero protocol documentation. So we reverse-engineered the whole thing.

This project gives you:
- **The complete XML-over-TCP protocol** — every command, response, and status code documented
- **Working Node.js drivers** for WiFi and USB
- **A C# USB driver** for direct Windows device I/O
- **Production-tested** — we print hundreds of component labels with this at [Sunburn Schematics](https://sunburnschematics.com)

### The Workflow

1. Design your labels in whatever tool you want (we built a web app for it)
2. Send JPEG images to the printer over WiFi or USB
3. The printer auto-scales to fit the tape width
4. Full-cut mode gives you individual stickers; half-cut gives you a peelable strip
5. Stick them on your component boxes, bins, bags — whatever your storage system is
6. Actually find the right 0402 cap on the first try instead of the fifth

For batch printing (labeling all your boxes at once), the driver supports sequential printing with automatic IDLE detection between labels — about 15 seconds per label. Tilt the printer upright so the labels fall out by gravity and it runs hands-free.

## Quick Start

### WiFi (any platform)

```js
const { VC500WDriver } = require('./driver');

const printer = new VC500WDriver({ host: '192.168.1.243', port: 9100 });
await printer.connect();

// Print a JPEG label
const jpeg = fs.readFileSync('label.jpg');
const result = await printer.printJpeg(jpeg, { quality: 'vivid', cutMode: 'full' });
console.log(result.success ? 'Printed!' : 'Failed:', result);

// Check status
const status = await printer.getStatus();
console.log('State:', status.state); // 'idle', 'printing', 'cutting', etc.
```

### USB (Windows only)

```js
const printer = new VC500WDriver();
await printer.connect(); // auto-discovers USB and WiFi
const result = await printer.printJpeg(jpeg, { quality: 'vivid', cutMode: 'full' });
```

## Hardware Specifications

| Spec | Value |
|------|-------|
| Model | Brother VC-500W |
| Print Technology | ZINK (Zero Ink) |
| Resolution | 313 x 313 DPI (normal) / 313 x 317 DPI (vivid) |
| Max Print Width | ~25.4mm (1 inch) with CZ-1004 tape |
| Connectivity | WiFi 802.11 b/g/n, USB 2.0 |
| USB VID:PID | `04F9:20B0` |
| Internal Model Name | "Wedge" |
| Default WiFi Hostname | `BRVC-500W-XXXX.local` (last 4 of serial) |
| TCP Port | 9100 |
| Power | 24V DC adapter |

### Compatible Tape Cassettes

| Model | Width | Type |
|-------|-------|------|
| CZ-1001 | 9mm | Continuous |
| CZ-1002 | 12mm | Continuous |
| CZ-1003 | 19mm | Continuous |
| CZ-1004 | 25mm (1") | Continuous |
| CZ-1005 | 50mm (2") | Continuous |

## Protocol Overview

The VC-500W uses a custom **XML-over-TCP** protocol on port 9100. There is **no HTTP layer** — raw XML commands are sent directly over a TCP socket, and the printer responds with XML. Image data (JPEG) is sent as raw binary on the same TCP channel.

**Key insight**: There is **no encryption** — all communication is plaintext XML + binary JPEG.

### Connection Lifecycle

```
Client                              Printer (port 9100)
  |                                      |
  |-------- TCP Connect ----------------->|
  |                                      |
  |-- XML: <read>/config.xml</read> ---->|  (optional: get config)
  |<-- XML: <status>...<config>... ------|
  |                                      |
  |-- XML: <read>/status.xml</read> ---->|  (check if idle)
  |<-- XML: <status>...<status>... ------|
  |                                      |
  |-- XML: <print>...</print> ---------->|  (send print command)
  |<-- XML: <status><code>0</code> ------|  (printer ready for data)
  |                                      |
  |-- [raw JPEG bytes] ----------------->|  (send image)
  |<-- XML: <status><code>0</code> ------|  (print complete)
  |                                      |
  |-------- TCP Close ------------------->|  ** REQUIRED to trigger cut **
  |                                      |
  |  ... wait for printer to become IDLE ...
  |                                      |
  |-------- TCP Connect ----------------->|  (reconnect for next label)
```

### Critical Discovery: Socket Close Triggers Cut

**The printer will NOT cut the label until the TCP connection is closed.** If you keep the socket open after receiving the print completion response, the printer sits in a "waiting" state with blinking lights. You must close the TCP socket to trigger the cut cycle, then reconnect for the next operation.

## XML Commands

All commands are prefixed with `<?xml version="1.0" encoding="UTF-8"?>` followed by a newline.

### Read Configuration

**Request:**
```xml
<read>
<path>/config.xml</path>
</read>
```

**Response:**
```xml
<?xml version="1.0" encoding="UTF-8"?>
<status>
<code>0</code>
<datasize>NNNN</datasize>
</status>
<?xml version="1.0" encoding="UTF-8"?>
<config>
<model_name>VC-500W</model_name>
<serial_number>U64934A5Y165843</serial_number>
<fw_version_number>2022071322</fw_version_number>
<wlan0_mac_address>04:FE:A1:56:D1:0C</wlan0_mac_address>
<cassette_type>CZ-1004</cassette_type>
<width_inches>1.0</width_inches>
<media_length_initial>394.0</media_length_initial>
</config>
```

### Read Status

**Request:**
```xml
<read>
<path>/status.xml</path>
</read>
```

**Response:**
```xml
<?xml version="1.0" encoding="UTF-8"?>
<status>
<code>0</code>
<datasize>NNNN</datasize>
</status>
<?xml version="1.0" encoding="UTF-8"?>
<status>
<print_state>IDLE</print_state>
<print_job_stage>NONE</print_job_stage>
<print_job_error>NONE</print_job_error>
<remain>350.5</remain>
</status>
```

**`print_state` values:**

| State | Meaning |
|-------|---------|
| `IDLE` | Ready for next print |
| `ready` | Ready (alternate form) |
| `printing` | Currently printing |
| `feeding` | Feeding tape |
| `cutting` | Cutting label |

**`remain`** is tape remaining in inches. Multiply by 25.4 for millimeters.

### Read Status with Job Token

After acquiring a lock (see below), you can query status for a specific job:

```xml
<read>
<path>/status.xml</path>
<job_token>XXXXXXXX</job_token>
</read>
```

### Print Command

**Request:**
```xml
<print>
<mode>vivid</mode>
<speed>0</speed>
<lpi>317</lpi>
<width>0</width>
<height>0</height>
<dataformat>jpeg</dataformat>
<autofit>1</autofit>
<datasize>12345</datasize>
<cutmode>full</cutmode>
</print>
```

**Fields:**

| Field | Description |
|-------|-------------|
| `mode` | `vivid` (high quality) or `color` (normal/fast) |
| `speed` | `0` for vivid, `1` for normal |
| `lpi` | Lines per inch: `317` for vivid, `264` for normal |
| `width` | Image width in pixels (0 = auto) |
| `height` | Image height in pixels (0 = auto) |
| `dataformat` | Always `jpeg` |
| `autofit` | `1` to auto-scale image to tape width (use when width/height are 0) |
| `datasize` | Size of the JPEG data in bytes (must be exact) |
| `cutmode` | `full`, `half`, or `none` (see Cut Modes below) |

**Response (printer ready for data):**
```xml
<?xml version="1.0" encoding="UTF-8"?>
<status>
<code>0</code>
</status>
```

After receiving code 0, send the raw JPEG bytes. The printer then responds with another status when printing is complete.

**Error codes:**

| Code | Meaning |
|------|---------|
| 0 | Success / Ready |
| 3 | No media (tape not loaded or cover open) |
| >0 | Error (check `<comment>` for details) |
| -1 | No `<code>` tag in response (not necessarily an error) |

### Lock / Release (Optional)

The printer supports a lock/release mechanism for multi-label sessions. **In practice, this is error-prone and usually unnecessary** — sequential single prints with socket close/reconnect between them works more reliably.

**Lock:**
```xml
<lock>
<op>set</op>
<page_count>-1</page_count>
<job_timeout>99</job_timeout>
</lock>
```

Response includes a `<job_token>` for use in subsequent commands.

**Release:**
```xml
<lock>
<op>cancel</op>
<job_token>XXXXXXXX</job_token>
</lock>
```

### Cut Modes

| Mode | Behavior |
|------|----------|
| `full` | Cuts through both the label AND the backing paper. Label separates completely. |
| `half` | Kiss cut — cuts the label surface but NOT the backing. Creates peelable stickers on a continuous strip. |
| `none` | No cut. Tape continues feeding for the next label without cutting. |

## USB Communication (Windows)

### Overview

USB communication uses the Windows `usbprint.sys` driver, accessed via `SetupDi` device enumeration and `CreateFile` / `WriteFile` / `ReadFile` kernel32 APIs.

**Critical finding: USB is effectively write-only.** The VC-500W does not reliably send read responses over USB. `ReadFile` calls either time out or return no data. Because of this:

- **Status queries only work over WiFi** (TCP port 9100)
- **USB prints use "fire-and-forget"** — send XML + JPEG, assume success
- **Config/status queries over USB** sometimes work via `OpenSendReceive` but are unreliable

### USB Device Discovery

The printer is enumerated via `SetupDiGetClassDevs` using the USB Print interface GUID:

```
GUID_DEVINTERFACE_USBPRINT = {28d78fad-5a12-11d1-ae5b-0000f803a8c2}
```

Filter for Brother's vendor ID: `VID_04F9`

The device path looks like:
```
\\?\usb#vid_04f9&pid_20b0#u64934a5y165843#{28d78fad-5a12-11d1-ae5b-0000f803a8c2}
```

### USB Print Flow

```
1. FindBrotherPrinter()     → enumerate USB devices, find VID_04F9
2. CreateFile(devicePath)   → open device handle (GENERIC_READ | GENERIC_WRITE)
3. WriteFile(xmlCommand)    → send print XML
4. Sleep(300ms)             → give printer time to process
5. WriteFile(jpegData)      → send JPEG image
6. CloseHandle()            → close device
```

**Do NOT attempt to ReadFile after WriteFile** — it will hang indefinitely on most VC-500W units.

### USB Error Codes (Win32)

| Code | Meaning |
|------|---------|
| 2 | Device not found (ERROR_FILE_NOT_FOUND) |
| 5 | Access denied — another app is using the printer |
| 21 | Device not ready — printer may be off |
| 31 | General failure — try unplugging and re-plugging USB |
| 32 | Sharing violation — device in use by another process |
| 87 | Invalid parameter — device path may be stale |
| 121 | Timeout waiting for response (ERROR_SEM_TIMEOUT) |
| 1167 | Device not connected |
| 1168 | Device path not found — printer unplugged |

## Printer Behavior Notes

### Sleep Mode
The printer auto-sleeps after being idle for several minutes. When asleep:
- WiFi TCP connections are refused
- USB interface shuts down (`FindBrotherPrinter` returns no device)
- **Physical button press required to wake**
- Keep-alive polling (e.g., status query every 45 seconds) prevents sleep

### Multi-Label Printing
For printing multiple labels sequentially:

1. **Best approach**: Print one label, close TCP socket (triggers cut), poll status until IDLE, reconnect, print next label. This gives ~15 seconds per label.

2. **Lock approach** (not recommended): Acquire lock, print multiple labels without closing socket, release lock. Error-prone — if anything goes wrong, the lock can get stuck and requires manual release.

3. **The printer will complain** (blinking lights, refuse to print) if a label is not removed/cleared from the output slot before the next one prints. Tilting the printer upright so labels fall out by gravity solves this.

### Image Format
- Input: JPEG (any resolution)
- The printer auto-scales to fit tape width when `autofit=1`
- For best results with CZ-1004 (25mm/1" tape): ~313 pixels wide
- Vivid mode: 317 LPI (lines per inch) — slower, better color
- Normal mode: 264 LPI — faster, slightly less vibrant

## Project Structure

```
brother-vc500w-driver/
├── README.md                  # This file — full protocol documentation
├── LICENSE                    # MIT License
├── driver.js                  # Node.js WiFi driver (TCP, cross-platform)
├── usb-printer-io.cs          # C# USB driver (Windows, via PowerShell)
├── driver-usb.js              # Node.js USB wrapper (Windows, calls C# via PS)
├── examples/
│   ├── print-label.js         # Print a single JPEG label
│   ├── print-batch.js         # Print multiple labels sequentially
│   ├── get-status.js          # Query printer status
│   └── discover-printer.js    # Find printer on network
└── docs/
    └── protocol-captures.md   # Raw packet capture examples
```

## Credits

- **Sunburn Schematics** — production use, USB driver, protocol discoveries
- **[sgrimee/labelprinter-vc500w](https://github.com/sgrimee/labelprinter-vc500w)** — original Python reverse engineering
- **[m7i.org](https://m7i.org/projects/labelprinter-linux-python-for-vc-500w/)** — initial MITM packet capture methodology

## License

MIT License — see [LICENSE](LICENSE) for details.

## Contributing

Found a new protocol command? Discovered behavior on a different tape type? PRs and issues welcome.
