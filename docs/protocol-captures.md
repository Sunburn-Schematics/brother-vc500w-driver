# Protocol Captures & Raw Examples

This document shows actual captured XML exchanges between a client and the Brother VC-500W.

## Environment

- Printer: Brother VC-500W, firmware 2022071322
- Tape: CZ-1004 (25.4mm / 1" continuous)
- Connection: TCP port 9100 over WiFi

## Configuration Query

### Request
```
<?xml version="1.0" encoding="UTF-8"?>
<read>
<path>/config.xml</path>
</read>
```

### Response
```xml
<?xml version="1.0" encoding="UTF-8"?>
<status>
<code>0</code>
<datasize>847</datasize>
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

**Notes:**
- `<datasize>` indicates the size of the payload that follows the status block
- `<media_length_initial>` is the total tape length in inches (394" = ~10m)
- `<width_inches>` is the tape width (1.0 = 25.4mm CZ-1004)
- `<cassette_type>` maps to the physical tape cassette model

## Status Query (Idle)

### Request
```
<?xml version="1.0" encoding="UTF-8"?>
<read>
<path>/status.xml</path>
</read>
```

### Response
```xml
<?xml version="1.0" encoding="UTF-8"?>
<status>
<code>0</code>
<datasize>392</datasize>
</status>
<?xml version="1.0" encoding="UTF-8"?>
<status>
<print_state>IDLE</print_state>
<print_job_stage>NONE</print_job_stage>
<print_job_error>NONE</print_job_error>
<remain>350.5</remain>
</status>
```

**Notes:**
- `<remain>` is tape remaining in inches
- When tape is very low or empty, `<remain>` approaches 0
- After a fresh tape is loaded, `<remain>` is close to `<media_length_initial>`

## Print Job (Vivid Mode, Full Cut)

### Step 1: Send Print Command

```
<?xml version="1.0" encoding="UTF-8"?>
<print>
<mode>vivid</mode>
<speed>0</speed>
<lpi>317</lpi>
<width>0</width>
<height>0</height>
<dataformat>jpeg</dataformat>
<autofit>1</autofit>
<datasize>45230</datasize>
<cutmode>full</cutmode>
</print>
```

### Step 2: Printer Responds "Ready"

```xml
<?xml version="1.0" encoding="UTF-8"?>
<status>
<code>0</code>
</status>
```

A code of `0` means the printer is ready to receive JPEG data.

### Step 3: Send Raw JPEG Bytes

Send exactly `<datasize>` bytes of JPEG data on the same TCP connection.
No framing, no headers — just raw binary JPEG data.

### Step 4: Printer Responds "Complete"

```xml
<?xml version="1.0" encoding="UTF-8"?>
<status>
<code>0</code>
</status>
```

Code `0` = print data received successfully. **But the label is not yet cut.**

### Step 5: Close TCP Socket

**This is the critical step.** The printer will NOT cut the label until the TCP
connection is closed. If you keep the socket open, the printer sits in a
"waiting" state with blinking lights.

### Step 6: Poll for IDLE

After closing the socket, wait ~3 seconds for the cut cycle to begin, then
reconnect and poll `/status.xml` until `print_state` returns to `IDLE`:

```
State progression: printing → feeding → cutting → IDLE
```

Typical time from socket close to IDLE: 8-12 seconds.

## Print Job Error (No Media)

### Response
```xml
<?xml version="1.0" encoding="UTF-8"?>
<status>
<code>3</code>
<comment>No media loaded</comment>
</status>
```

When the printer responds with a non-zero code, do NOT send JPEG data.

## Lock / Release

### Lock Request
```
<?xml version="1.0" encoding="UTF-8"?>
<lock>
<op>set</op>
<page_count>-1</page_count>
<job_timeout>99</job_timeout>
</lock>
```

### Lock Response
```xml
<?xml version="1.0" encoding="UTF-8"?>
<status>
<code>0</code>
</status>
<?xml version="1.0" encoding="UTF-8"?>
<lock>
<job_token>A1B2C3D4</job_token>
<code>0</code>
</lock>
```

### Release Request
```
<?xml version="1.0" encoding="UTF-8"?>
<lock>
<op>cancel</op>
<job_token>A1B2C3D4</job_token>
</lock>
```

**Warning:** The lock mechanism is brittle. If a print fails while locked, you
may need to manually release the lock or power-cycle the printer. Sequential
single prints (close socket between each) are more reliable.

## USB Communication

USB uses the same XML protocol, but over the Windows USB print interface
(`usbprint.sys`) instead of TCP sockets.

### Key Differences from WiFi

1. **Write-only in practice**: `ReadFile` on the USB endpoint either times out
   or returns 0 bytes. The printer does not send acknowledgments over USB.

2. **Fire-and-forget printing**: Send XML command, sleep 300ms, send JPEG data,
   close handle. No way to confirm the print succeeded.

3. **Device enumeration**: Use `SetupDi*` APIs with `GUID_DEVINTERFACE_USBPRINT`
   to find the printer. Filter by VID `04F9` (Brother).

4. **Handle lifecycle**: Each operation (send command, send data) opens and closes
   its own handle. Keeping a handle open can block the printer.

### USB Print Sequence (Hex Dump)

```
→ Write: [XML command bytes, ~200-400 bytes]
   (300ms delay)
→ Write: [JPEG data, 10-200 KB]
→ Close handle
```

No response is read. Success is assumed if both `WriteFile` calls succeed.
