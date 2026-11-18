/**
 * Brother VC-500W Printer Driver
 *
 * Supports two connection modes:
 * 1. WiFi: Direct TCP to port 9100 (XML protocol, bidirectional, autocut)
 * 2. USB: Direct device I/O via usbprint.sys (CreateFile, bidirectional)
 *
 * Cut modes:
 * - full: Cuts through label + backing (separates the label completely)
 * - half: Kiss cut - cuts label but NOT backing (peelable stickers on a strip)
 * - none: No cut (for when you want continuous uncut tape)
 *
 * Protocol: XML commands + raw JPEG data
 * Reference: https://github.com/sgrimee/labelprinter-vc500w
 */

var net = require('net');
var http = require('http');
var { execFile } = require('child_process');
var path = require('path');
var fs = require('fs');
var os = require('os');

var XML_PREFIX = '<?xml version="1.0" encoding="UTF-8"?>\n';

var PRINT_MODES = {
  vivid: { name: 'vivid', speed: 0, lpi: 317 },
  normal: { name: 'color', speed: 1, lpi: 264 }
};

var CUT_MODES = ['full', 'half', 'none'];

var WIFI_READY_POLL_INTERVAL = 1000;
var WIFI_READY_POLL_MAX_WAIT = 30000;
var COMPLETION_PROTOCOL = 'vc500w-idle-v1';

function normalizedState(value) {
  return typeof value === 'string' ? value.trim().toUpperCase() : '';
}

function parsePrintStatus(response) {
  var remain = parseXmlValue(response, 'remain');
  var inches = remain == null || remain.trim() === '' ? NaN : Number(remain);
  return {
    state: normalizedState(parseXmlValue(response, 'print_state')),
    stage: normalizedState(parseXmlValue(response, 'print_job_stage')),
    error: normalizedState(parseXmlValue(response, 'print_job_error')),
    tapeRemainingMm: Number.isFinite(inches) && inches >= 0 ? inches * 25.4 : null
  };
}

function uncertainPrint(connection, error) {
  return { success: false, code: -2, connection: connection, verified: null,
    completed: false, uncertain: true, error: error };
}

function takeResponseDocument(buffer, expectedRoot, requiredField, onIgnored) {
  // Replies have no request IDs. Discard complete documents of another type,
  // and return only one matching document so callers cannot mix their fields.
  var opening = /<(status|config|lock)(?:\s[^>]*)?>/.exec(buffer);
  while (opening) {
    var contentStart = opening.index + opening[0].length;
    var closing = new RegExp('</' + opening[1] + '\\s*>').exec(buffer.slice(contentStart));
    if (!closing) break;
    var end = contentStart + closing.index + closing[0].length;
    var document = buffer.slice(opening.index, end);
    buffer = buffer.slice(end);
    var fieldValue = requiredField ? parseXmlValue(document, requiredField) : null;
    if (opening[1] === expectedRoot && (!requiredField || (fieldValue != null && fieldValue.trim() !== ''))) {
      return { document: document, remaining: buffer };
    }
    if (onIgnored) onIgnored(opening[1], document);
    opening = /<(status|config|lock)(?:\s[^>]*)?>/.exec(buffer);
  }
  return { document: null, remaining: buffer };
}

// ---------- TCP (WiFi) Connection ----------

class TCPConnection {
  constructor(host, port) {
    this.host = host;
    this.port = port || 9100;
    this.socket = null;
    this.driver = null; // set by VC500WDriver to flag wifiConnected on error
    this._cmdQueue = Promise.resolve(); // serialize commands on the socket
  }

  connect(timeout) {
    this.close();
    return new Promise(function (resolve, reject) {
      var self = this;
      var settled = false;
      var socket = new net.Socket();
      self.socket = socket;
      socket.setTimeout(timeout || 5000);
      socket.connect(self.port, self.host, function () {
        if (self.socket !== socket) {
          socket.destroy();
          if (!settled) { settled = true; reject(new Error('Connection superseded')); }
          return;
        }
        settled = true;
        socket.setKeepAlive(true, 30000);
        socket.setTimeout(0);
        resolve();
      });
      socket.on('error', function (err) {
        if (!settled) { settled = true; reject(err); }
        console.log('[TCP] Socket error:', err.message);
        if (self.socket === socket && self.driver && self.driver.wifiConnection === self) {
          self.driver.wifiConnected = false;
        }
      });
      socket.on('close', function () {
        if (!settled) { settled = true; reject(new Error('Connection closed')); }
        console.log('[TCP] Socket closed');
        if (self.socket === socket && self.driver && self.driver.wifiConnection === self) {
          self.driver.wifiConnected = false;
        }
      });
      socket.on('timeout', function () {
        socket.destroy();
        if (!settled) { settled = true; reject(new Error('Connection timeout')); }
      });
    }.bind(this));
  }

  sendCommand(xml, timeout) {
    var self = this;
    // Chain onto the queue so commands run sequentially
    self._cmdQueue = self._cmdQueue.then(function () {
      return self._sendCommandInner(xml, timeout);
    }, function () {
      return self._sendCommandInner(xml, timeout);
    });
    return self._cmdQueue;
  }

  _sendCommandInner(xml, timeout) {
    return new Promise(function (resolve, reject) {
      var self = this;
      var socket = self.socket;
      if (!socket || socket.destroyed) {
        reject(new Error('Printer socket is not connected'));
        return;
      }
      var fullXml = XML_PREFIX + xml;
      var expectedRoot = /<path>\s*\/config\.xml\s*<\/path>/.test(xml) ? 'config'
        : /<lock(?:\s|>)/.test(xml) ? 'lock' : 'status';
      // A read ACK also uses <status>; it is not the status.xml payload.
      var requiredField = expectedRoot === 'config' ? 'model_name'
        : /<path>\s*\/status\.xml\s*<\/path>/.test(xml) ? 'print_state'
          : /<print(?:\s|\/?>)/.test(xml) ? 'code' : null;
      var ignoredReadFrames = 0;
      var reportIgnoredRead = function (root, document) {
        if ((requiredField !== 'print_state' && requiredField !== 'model_name') || ignoredReadFrames >= 3) return;
        ignoredReadFrames++;
        // Bounded protocol diagnostics: never include config values or raw XML.
        console.log('[TCP] Ignored read response:', JSON.stringify({
          expectedRoot: expectedRoot, requiredField: requiredField, root: root,
          tags: Array.from(document.matchAll(/<([A-Za-z_][A-Za-z0-9_.:-]*)(?=[\s>])/g),
            function (match) { return match[1]; }).slice(0, 32),
          print_state: parseXmlValue(document, 'print_state'),
          print_job_stage: parseXmlValue(document, 'print_job_stage'),
          print_job_error: parseXmlValue(document, 'print_job_error'),
          code: parseXmlValue(document, 'code')
        }));
      };
      var responseData = '';
      var timer = setTimeout(function () {
        socket.removeListener('data', onData);
        // A late response has no request ID. Discard the connection so it
        // cannot be misattributed to the next status/config request.
        socket.destroy();
        if (self.driver && self.driver.wifiConnection === self) self.driver.wifiConnected = false;
        reject(new Error('Response timeout'));
      }, timeout || 10000);

      var onData = function (data) {
        responseData += data.toString('utf8');
        var extracted = takeResponseDocument(responseData, expectedRoot, requiredField, reportIgnoredRead);
        responseData = extracted.remaining;
        if (extracted.document) {
          clearTimeout(timer);
          socket.removeListener('data', onData);
          resolve(extracted.document);
        }
      };
      socket.on('data', onData);
      socket.write(fullXml, 'utf8');
    }.bind(this));
  }

  sendData(buffer) {
    return new Promise(function (resolve, reject) {
      this.socket.write(buffer, function (err) {
        if (err) reject(err);
        else resolve();
      });
    }.bind(this));
  }

  waitForResponse(timeout) {
    return new Promise(function (resolve, reject) {
      var self = this;
      var responseData = '';
      var timer = setTimeout(function () {
        if (self.socket) self.socket.removeListener('data', onData);
        reject(new Error('Print response timeout'));
      }, timeout || 120000);

      var onData = function (data) {
        responseData += data.toString('utf8');
        if (responseData.includes('</status>')) {
          clearTimeout(timer);
          if (self.socket) self.socket.removeListener('data', onData);
          resolve(responseData);
        }
      };
      self.socket.on('data', onData);
    }.bind(this));
  }

  // Atomic print sequence: sendCommand + sendData + waitForResponse under one queue slot
  sendPrintJob(xml, jpegBuffer, timeout) {
    var self = this;
    self._cmdQueue = self._cmdQueue.then(function () {
      return self._sendPrintJobInner(xml, jpegBuffer, timeout);
    }, function () {
      return self._sendPrintJobInner(xml, jpegBuffer, timeout);
    });
    return self._cmdQueue;
  }

  _sendPrintJobInner(xml, jpegBuffer, timeout) {
    var self = this;
    var printTimeout = timeout || 120000;

    // Step 1: send the print XML command and wait for status (printer ready)
    return self._sendCommandInner(xml, 8000).then(function (readyResponse) {
      console.log('[TCP] Print initial response:', readyResponse.substring(0, 300));

      // Check if printer reported an error before sending JPEG data
      // Missing or invalid readiness acknowledgements must not dispatch an image.
      var initCode = parseStatusCode(readyResponse);
      var initComment = parseXmlValue(readyResponse, 'comment') || '';
      if (initCode !== 0) {
        var errMsg = 'Printer not ready (code ' + initCode + '): ' + initComment;
        console.log('[TCP] Aborting print —', errMsg);
        throw new Error(errMsg);
      }

      console.log('[TCP] Printer ready, sending JPEG data (' + jpegBuffer.length + ' bytes)...');
      // The next response acknowledges the data, not physical completion.
      // Install the listener before writing: a response can arrive before the
      // socket write callback. Fence retries before any bytes can leave.
      return new Promise(function (resolve, reject) {
        var responseData = '';
        var timer = setTimeout(function () {
          if (self.socket) self.socket.removeListener('data', onData);
          if (self.socket) self.socket.destroy();
          if (self.driver && self.driver.wifiConnection === self) self.driver.wifiConnected = false;
          console.log('[TCP] Print completion timeout ??? partial response:', responseData.substring(0, 500) || '(none)');
          reject(new Error('Print response timeout'));
        }, printTimeout);

        var onData = function (data) {
          var chunk = data.toString('utf8');
          responseData += chunk;
          console.log('[TCP] Print data chunk (' + chunk.length + ' chars):', chunk.substring(0, 200));
          var extracted = takeResponseDocument(responseData, 'status', 'code');
          responseData = extracted.remaining;
          if (extracted.document) {
            clearTimeout(timer);
            if (self.socket) self.socket.removeListener('data', onData);
            resolve(extracted.document);
          }
        };
        self.socket.on('data', onData);
        if (self.driver) self.driver._dataSentToPrinter = true;
        self.socket.write(jpegBuffer, function (err) {
          if (err) {
            clearTimeout(timer);
            if (self.socket) self.socket.removeListener('data', onData);
            reject(err);
          }
        });
      });
    });
  }

  close() {
    if (this.socket) {
      this.socket.destroy();
      this.socket = null;
    }
  }
}

// ---------- Direct USB Connection (via usbprint.sys CreateFile) ----------

// The C# code for USB I/O - included in every PowerShell invocation
var USB_CSHARP = fs.readFileSync(path.join(__dirname, 'usb-printer-io.cs'), 'utf8');

function runPowerShell(script, timeout) {
  return new Promise(function (resolve, reject) {
    // Write script to temp file to avoid escaping issues
    var tmpScript = path.join(os.tmpdir(), 'sunburn-ps-' + Date.now() + '.ps1');
    fs.writeFileSync(tmpScript, script, 'utf8');

    execFile('powershell', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', tmpScript], {
      timeout: timeout || 30000,
      maxBuffer: 1024 * 1024
    }, function (err, stdout, stderr) {
      try { fs.unlinkSync(tmpScript); } catch (e) {}
      if (err) reject(new Error(stderr || err.message));
      else resolve(stdout.trim());
    });
  });
}

function runPowerShellInline(script, timeout) {
  return new Promise(function (resolve, reject) {
    execFile('powershell', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script], {
      timeout: timeout || 30000,
      maxBuffer: 1024 * 1024
    }, function (err, stdout, stderr) {
      if (err) reject(new Error(stderr || err.message));
      else resolve(stdout.trim());
    });
  });
}

class DirectUSBConnection {
  constructor() {
    this.devicePath = null;
  }

  async connect() {
    // Find the Brother USB printer device path
    var script = "Add-Type @'\n" + USB_CSHARP + "\n'@\n" +
      'Write-Output ([UsbPrinterIO]::FindBrotherPrinter())';
    var result = await runPowerShell(script);

    if (result.startsWith('ERROR:')) {
      throw new Error('USB printer not found: ' + result);
    }

    this.devicePath = result;
    console.log('[USB Direct] Found device:', this.devicePath);
  }

  async sendCommand(xml, timeout) {
    var fullXml = XML_PREFIX + xml;
    var tmpData = path.join(os.tmpdir(), 'sunburn-cmd-' + Date.now() + '.bin');
    fs.writeFileSync(tmpData, fullXml, 'utf8');

    var script = "Add-Type @'\n" + USB_CSHARP + "\n'@\n" +
      '$r = [UsbPrinterIO]::OpenSendReceive("' + this.devicePath.replace(/'/g, "''") + '", "' + tmpData.replace(/\\/g, '\\\\') + '", 2000)\n' +
      'Write-Output $r';

    try {
      var result = await runPowerShell(script, timeout || 15000);
      try { fs.unlinkSync(tmpData); } catch (e) {}

      if (result.startsWith('DATA:')) {
        return result.substring(5);
      } else if (result.startsWith('ERROR:')) {
        throw new Error('USB command failed: ' + result);
      }
      console.log('[USB Direct] Command result:', result);
      return '<status><code>0</code></status>';
    } catch (e) {
      try { fs.unlinkSync(tmpData); } catch (e2) {}
      throw e;
    }
  }

  async sendData(buffer) {
    var tmpData = path.join(os.tmpdir(), 'sunburn-data-' + Date.now() + '.bin');
    fs.writeFileSync(tmpData, buffer);

    var script = "Add-Type @'\n" + USB_CSHARP + "\n'@\n" +
      '$r = [UsbPrinterIO]::OpenAndSend("' + this.devicePath.replace(/'/g, "''") + '", "' + tmpData.replace(/\\/g, '\\\\') + '")\n' +
      'Write-Output $r';

    try {
      var result = await runPowerShell(script, 30000);
      try { fs.unlinkSync(tmpData); } catch (e) {}

      if (result.startsWith('ERROR:')) {
        throw new Error('USB data send failed: ' + result);
      }
      console.log('[USB Direct] Data sent:', result);
    } catch (e) {
      try { fs.unlinkSync(tmpData); } catch (e2) {}
      throw e;
    }
  }

  async waitForResponse(timeout) {
    var script = "Add-Type @'\n" + USB_CSHARP + "\n'@\n" +
      '$r = [UsbPrinterIO]::OpenAndRead("' + this.devicePath.replace(/'/g, "''") + '", ' + (timeout || 5000) + ')\n' +
      'Write-Output $r';

    var result = await runPowerShell(script, (timeout || 120000) + 5000);

    if (result.startsWith('DATA:')) {
      return result.substring(5);
    }
    console.log('[USB Direct] Wait response:', result);
    return '<status><code>0</code></status>';
  }

  close() {
    // Each operation opens/closes its own handle, so nothing to do here
  }
}

// ---------- USB Single-shot print (all-in-one PowerShell session) ----------

function buildPrintXml(jpegSize, options) {
  options = options || {};
  var quality = options.quality || 'vivid';
  var cutMode = options.cutMode || 'full';
  var imgWidth = options.imgWidth || 0;
  var imgHeight = options.imgHeight || 0;
  var m = PRINT_MODES[quality] || PRINT_MODES.vivid;

  var useAutofit = (imgWidth === 0 && imgHeight === 0);

  return '<print>\n' +
    '<mode>' + m.name + '</mode>\n' +
    '<speed>' + m.speed + '</speed>\n' +
    '<lpi>' + m.lpi + '</lpi>\n' +
    '<width>' + imgWidth + '</width>\n' +
    '<height>' + imgHeight + '</height>\n' +
    '<dataformat>jpeg</dataformat>\n' +
    (useAutofit ? '<autofit>1</autofit>\n' : '') +
    '<datasize>' + jpegSize + '</datasize>\n' +
    '<cutmode>' + cutMode + '</cutmode>\n' +
    '</print>';
}

async function usbDirectPrint(devicePath, jpegPath, options) {
  var jpegSize = fs.statSync(jpegPath).size;
  var printXml = buildPrintXml(jpegSize, options);

  var tmpXml = path.join(os.tmpdir(), 'sunburn-printxml-' + Date.now() + '.bin');
  fs.writeFileSync(tmpXml, XML_PREFIX + printXml, 'utf8');

  var escapedDevice = devicePath.replace(/'/g, "''");
  var escapedXml = tmpXml.replace(/\\/g, '\\\\');
  var escapedJpeg = jpegPath.replace(/\\/g, '\\\\');

  // Use fire-and-forget for USB ??? the VC-500W does not send read responses over USB,
  // so bidirectional PrintJob hangs waiting for acknowledgments that never come.
  var script = "Add-Type @'\n" + USB_CSHARP + "\n'@\n" +
    '$r = [UsbPrinterIO]::PrintJobFireAndForget("' + escapedDevice + '", "' +
    escapedXml + '", "' + escapedJpeg + '")\n' +
    'Write-Output $r';

  try {
    var result = await runPowerShell(script, 30000);
    try { fs.unlinkSync(tmpXml); } catch (e) {}
    return result;
  } catch (e) {
    try { fs.unlinkSync(tmpXml); } catch (e2) {}
    throw e;
  }
}

// Verify USB printer is reachable by checking if it's enumerated by Windows.
// We use FindBrotherPrinter (SetupDi enumeration) rather than opening the device,
// because OpenAndSend/WriteFile can hang on the VC-500W USB endpoint.
async function usbProbeConnection(devicePath) {
  var script = "Add-Type @'\n" + USB_CSHARP + "\n'@\n" +
    'Write-Output ([UsbPrinterIO]::FindBrotherPrinter())';

  try {
    var result = await runPowerShell(script, 10000);
    console.log('[USB Probe] Result:', result);
    if (result.startsWith('ERROR:')) {
      return { alive: false, error: 'USB printer not enumerated: ' + result, devicePath: null };
    }
    var pathMatch = (result === devicePath);
    if (!pathMatch) {
      console.log('[USB Probe] Device path changed:', devicePath, '->', result);
    }
    return { alive: true, devicePath: result };
  } catch (e) {
    return { alive: false, error: 'USB probe failed: ' + e.message, devicePath: null };
  }
}

async function usbDirectBatchPrint(devicePath, jobs) {
  // jobs = [{ jpegPath, cutMode, quality, finalCutMode }, ...]
  // Prints all labels on a single USB handle
  // Kiss cuts between labels, full cut on last one

  var tmpXmls = [];
  var tmpJpegs = [];

  for (var i = 0; i < jobs.length; i++) {
    var job = jobs[i];
    var isLast = (i === jobs.length - 1);
    var cutMode = isLast ? (job.finalCutMode || 'full') : (job.cutMode || 'full');
    var jpegSize = fs.statSync(job.jpegPath).size;
    var printXml = buildPrintXml(jpegSize, { quality: job.quality, cutMode: cutMode, imgWidth: job.imgWidth || 0, imgHeight: job.imgHeight || 0 });

    var tmpXml = path.join(os.tmpdir(), 'sunburn-batchxml-' + Date.now() + '-' + i + '.bin');
    fs.writeFileSync(tmpXml, XML_PREFIX + printXml, 'utf8');
    tmpXmls.push(tmpXml);
    tmpJpegs.push(job.jpegPath);
  }

  // Build PowerShell array literals for paths
  var xmlArr = tmpXmls.map(function (p) { return '"' + p.replace(/\\/g, '\\\\') + '"'; }).join(',');
  var jpegArr = tmpJpegs.map(function (p) { return '"' + p.replace(/\\/g, '\\\\') + '"'; }).join(',');

  var script = "Add-Type @'\n" + USB_CSHARP + "\n'@\n" +
    '$xmls = @(' + xmlArr + ')\n' +
    '$jpegs = @(' + jpegArr + ')\n' +
    '$r = [UsbPrinterIO]::BatchPrintJob("' + devicePath.replace(/'/g, "''") + '", $xmls, $jpegs)\n' +
    'Write-Output $r';

  try {
    var result = await runPowerShell(script, 300000);
    tmpXmls.forEach(function (f) { try { fs.unlinkSync(f); } catch (e) {} });
    return result;
  } catch (e) {
    tmpXmls.forEach(function (f) { try { fs.unlinkSync(f); } catch (e2) {} });
    throw e;
  }
}

// ---------- XML Helpers ----------

function parseXmlValue(xml, tag) {
  var re = new RegExp('<' + tag + '>([^<]*)</' + tag + '>');
  var match = xml.match(re);
  return match ? match[1] : null;
}

function parseStatusCode(response) {
  var code = parseXmlValue(response, 'code');
  return code !== null ? parseInt(code, 10) : -1;
}

// ---------- Printer Discovery ----------

// Get local IPv4 subnet prefixes (e.g. ['192.168.1'] for 192.168.1.x/24)
function getLocalSubnetInfo() {
  var subnets = [];
  var ifaces = os.networkInterfaces();
  var keys = Object.keys(ifaces);
  for (var i = 0; i < keys.length; i++) {
    var addrs = ifaces[keys[i]];
    for (var j = 0; j < addrs.length; j++) {
      var addr = addrs[j];
      if (addr.family === 'IPv4' && !addr.internal) {
        var parts = addr.address.split('.');
        if (parts.length === 4) {
          subnets.push(parts.slice(0, 3).join('.'));
        }
      }
    }
  }
  return subnets;
}

// Verify a host is a Brother VC-500W by querying its XML config
async function verifyBrotherPrinter(host) {
  try {
    var conn = new TCPConnection(host, 9100);
    await conn.connect(2000);
    var response = await conn.sendCommand('<read>\n<path>/config.xml</path>\n</read>', 3000);
    conn.close();
    var model = parseXmlValue(response, 'model_name');
    return model && model.indexOf('VC-500W') >= 0;
  } catch (e) {
    return false;
  }
}

// Scan a /24 subnet for a printer listening on port 9100, then verify it's a Brother
async function scanSubnetForPrinter(prefix) {
  var BATCH_SIZE = 50;
  var CONNECT_TIMEOUT = 500;
  var found = null;

  function probeHost(ip) {
    return new Promise(function (resolve) {
      var socket = new net.Socket();
      socket.setTimeout(CONNECT_TIMEOUT);
      socket.connect(9100, ip, function () {
        resolve(ip);
        socket.destroy();
      });
      socket.on('error', function () { resolve(null); socket.destroy(); });
      socket.on('timeout', function () { resolve(null); socket.destroy(); });
    });
  }

  // Scan in batches to avoid overwhelming the network
  for (var start = 1; start <= 254 && !found; start += BATCH_SIZE) {
    var batch = [];
    for (var i = start; i < start + BATCH_SIZE && i <= 254; i++) {
      batch.push(probeHost(prefix + '.' + i));
    }
    var results = await Promise.all(batch);
    for (var r = 0; r < results.length; r++) {
      if (results[r]) {
        // Verify it's actually a Brother printer, not just any device on 9100
        var isBrother = await verifyBrotherPrinter(results[r]);
        if (isBrother) {
          found = results[r];
          break;
        }
      }
    }
  }

  return found;
}

async function discoverPrinter(options) {
  var result = {
    usb: null,
    wifi: null,
    recommended: null
  };

  // Check USB via SetupDi
  try {
    var script = "Add-Type @'\n" + USB_CSHARP + "\n'@\n" +
      'Write-Output ([UsbPrinterIO]::FindBrotherPrinter())';
    var usbResult = await runPowerShell(script);
    if (!usbResult.startsWith('ERROR:')) {
      result.usb = { devicePath: usbResult };
    }
  } catch (e) {}

  // WiFi discovery with global 5-second timeout
  var WIFI_DISCOVERY_TIMEOUT = 5000;
  var wifiTimedOut = false;

  var wifiDiscovery = (async function () {
    // Tier 1: Try hostname, Tier 2: Try saved IP
    var wifiHosts = ['BRVC-500W-5843'];
    if (options && options.savedIp) {
      wifiHosts.push(options.savedIp);
    }

    for (var hi = 0; hi < wifiHosts.length; hi++) {
      try {
        var tryHost = wifiHosts[hi];
        var hostCheck = await new Promise(function (resolve) {
          var socket = new net.Socket();
          socket.setTimeout(2000);
          socket.connect(9100, tryHost, function () {
            resolve(socket.remoteAddress);
            socket.destroy();
          });
          socket.on('error', function () { resolve(null); socket.destroy(); });
          socket.on('timeout', function () { resolve(null); socket.destroy(); });
        });
        if (hostCheck) {
          return { host: hostCheck, port: 9100, resolvedFrom: tryHost };
        }
      } catch (e) {}
    }

    // Tier 3: Subnet scan — only if hostname and saved IP both failed
    var subnets = getLocalSubnetInfo();
    if (subnets.length > 0) {
      console.log('[Discovery] Hostname/saved IP failed — scanning subnets:', subnets.join(', '));
      for (var si = 0; si < subnets.length; si++) {
        var found = await scanSubnetForPrinter(subnets[si]);
        if (found) {
          console.log('[Discovery] Subnet scan found printer at', found);
          return { host: found, port: 9100, resolvedFrom: 'subnet-scan' };
        }
      }
      console.log('[Discovery] Subnet scan found no printer');
    }

    return null;
  })();

  var timeoutPromise = new Promise(function (resolve) {
    setTimeout(function () {
      wifiTimedOut = true;
      resolve(null);
    }, WIFI_DISCOVERY_TIMEOUT);
  });

  try {
    result.wifi = await Promise.race([wifiDiscovery, timeoutPromise]);
  } catch (e) {
    console.log('[Discovery] WiFi discovery error:', e.message);
  }

  if (wifiTimedOut) {
    console.log('[Discovery] WiFi discovery timed out after ' + (WIFI_DISCOVERY_TIMEOUT / 1000) + 's');
    result.wifiNote = 'scan timed out';
  }

  if (result.wifi) {
    result.recommended = 'wifi';
  } else if (result.usb) {
    result.recommended = 'usb';
  }

  return result;
}

// ---------- Main Printer Driver ----------

class VC500WDriver {
  constructor(options) {
    options = options || {};
    this.wifiConnection = null;
    this.usbConnection = null;
    this.host = options.host || '';
    this.port = options.port || 9100;
    this.printerName = options.printerName || 'Brother VC-500W';
    this.wifiConnected = false;
    this.usbConnected = false;
    this.devicePath = null;
    this._reconnecting = false;
    this._connecting = false;
    this._connectionEpoch = 0;
    this._busy = false; // true during print, prevents keep-alive conflicts
    this._requestBusy = false;
    this._cachedPublicStatus = {};
    this._publicStatusInFlight = null;
  }

  get connected() {
    return this.wifiConnected || this.usbConnected;
  }

  get mode() {
    if (this.wifiConnected && this.usbConnected) return 'dual';
    if (this.wifiConnected) return 'wifi';
    if (this.usbConnected) return 'usb';
    return 'none';
  }

  _beginConnection() {
    if (this._connecting || this._reconnecting || this._busy) {
      throw new Error('A printer connection or print is already in progress');
    }
    this._connecting = true;
    return ++this._connectionEpoch;
  }

  _requireConnection(epoch) {
    if (this._connectionEpoch !== epoch) throw new Error('Printer connection cancelled');
  }

  _finishConnection(epoch) {
    if (this._connectionEpoch === epoch) this._connecting = false;
  }

  // Connect both channels in parallel. Succeeds if at least one connects.
  async connect() {
    var self = this;
    var epoch = this._beginConnection();
    try {
    var discovery = await discoverPrinter({ savedIp: this.host || undefined });
    this._requireConnection(epoch);
    console.log('[VC-500W] Discovery:', JSON.stringify(discovery));

    var wifiPromise = null;
    var usbPromise = null;

    if (discovery.wifi) {
      wifiPromise = (async function () {
        try {
          self.host = discovery.wifi.host;
          self.port = discovery.wifi.port || 9100;
          if (self.wifiConnection) self.wifiConnection.close();
          self.wifiConnection = new TCPConnection(self.host, self.port);
          self.wifiConnection.driver = self;
          var connection = self.wifiConnection;
          await connection.connect();
          if (self._connectionEpoch !== epoch) connection.close();
          self._requireConnection(epoch);
          self.wifiConnected = true;
          console.log('[VC-500W] WiFi connected to', self.host);
        } catch (e) {
          self._requireConnection(epoch);
          console.log('[VC-500W] WiFi connect failed:', e.message);
          self.wifiConnection = null;
          self.wifiConnected = false;
        }
      })();
    }

    if (discovery.usb) {
      usbPromise = (async function () {
        try {
          if (self.usbConnection) self.usbConnection.close();
          self.usbConnection = new DirectUSBConnection();
          var connection = self.usbConnection;
          await connection.connect();
          if (self._connectionEpoch !== epoch) connection.close();
          self._requireConnection(epoch);
          self.devicePath = self.usbConnection.devicePath;
          self.usbConnected = true;
          console.log('[VC-500W] USB connected');
        } catch (e) {
          self._requireConnection(epoch);
          console.log('[VC-500W] USB connect failed:', e.message);
          self.usbConnection = null;
          self.usbConnected = false;
        }
      })();
    }

    var promises = [wifiPromise, usbPromise].filter(Boolean);
    if (promises.length > 0) {
      await Promise.all(promises);
    }

    this._requireConnection(epoch);
    if (!this.connected) {
      throw new Error('Printer not found on WiFi or USB. Is it powered on?');
    }

    console.log('[VC-500W] Connected:', this.mode);
    return true;
    } finally {
      this._finishConnection(epoch);
    }
  }

  // Connect just the WiFi channel (additive ??? doesn't close USB)
  async connectWifi(host, port) {
    var epoch = this._beginConnection();
    try {
    if (this.wifiConnection) {
      this.wifiConnection.close();
      this.wifiConnected = false;
    }
    this.host = host;
    this.port = port || 9100;
    this.wifiConnection = new TCPConnection(this.host, this.port);
    this.wifiConnection.driver = this;
    var connection = this.wifiConnection;
    await connection.connect();
    if (this._connectionEpoch !== epoch) connection.close();
    this._requireConnection(epoch);
    this.wifiConnected = true;
    console.log('[VC-500W] WiFi connected to', this.host);
    } finally {
      this._finishConnection(epoch);
    }
  }

  // Connect just the USB channel (additive ??? doesn't close WiFi)
  async connectUsb() {
    var epoch = this._beginConnection();
    try {
    if (this.usbConnection) {
      this.usbConnection.close();
      this.usbConnected = false;
    }
    this.usbConnection = new DirectUSBConnection();
    var connection = this.usbConnection;
    await connection.connect();
    if (this._connectionEpoch !== epoch) connection.close();
    this._requireConnection(epoch);
    this.devicePath = this.usbConnection.devicePath;
    this.usbConnected = true;
    console.log('[VC-500W] USB connected');
    } finally {
      this._finishConnection(epoch);
    }
  }

  async getConfig() {
    if (this._busy || this._requestBusy) {
      var cached = this._cachedPublicStatus;
      return { model: cached.model, serial: cached.serial, firmware: cached.firmware,
        tapeType: cached.tapeType, tapeWidth: cached.tapeWidthMm,
        tapeLengthMm: cached.tapeLengthMm, busy: true };
    }
    // Try WiFi first (bidirectional ??? can read config)
    if (this.wifiConnected && this.wifiConnection) {
      try {
        var response = await this.wifiConnection.sendCommand(
          '<read>\n<path>/config.xml</path>\n</read>'
        );
        return {
          model: parseXmlValue(response, 'model_name') || 'VC-500W',
          serial: parseXmlValue(response, 'serial_number'),
          firmware: parseXmlValue(response, 'fw_version_number'),
          tapeType: parseXmlValue(response, 'cassette_type'),
          tapeWidth: parseFloat(parseXmlValue(response, 'width_inches') || '0') * 25.4,
          tapeLengthMm: parseFloat(parseXmlValue(response, 'media_length_initial') || '0') * 25.4,
          connection: this.mode,
          raw: response
        };
      } catch (e) {
        console.log('[VC-500W] WiFi config query failed:', e.message);
      }
    }
    // Try USB config query (bidirectional read via OpenSendReceive)
    if (this.usbConnected && this.usbConnection) {
      try {
        var usbResponse = await this.usbConnection.sendCommand(
          '<read>\n<path>/config.xml</path>\n</read>'
        );
        // Only parse if we got actual XML back (not NODATA or empty)
        if (usbResponse && usbResponse.indexOf('<config') >= 0) {
          return {
            model: parseXmlValue(usbResponse, 'model_name') || 'VC-500W',
            serial: parseXmlValue(usbResponse, 'serial_number'),
            firmware: parseXmlValue(usbResponse, 'fw_version_number'),
            tapeType: parseXmlValue(usbResponse, 'cassette_type'),
            tapeWidth: parseFloat(parseXmlValue(usbResponse, 'width_inches') || '0') * 25.4,
            tapeLengthMm: parseFloat(parseXmlValue(usbResponse, 'media_length_initial') || '0') * 25.4,
            connection: this.mode,
            raw: usbResponse
          };
        }
      } catch (e) {
        console.log('[VC-500W] USB config query failed:', e.message);
      }
    }
    // All queries failed ??? return static known values
    return {
      model: 'VC-500W',
      serial: null,
      firmware: null,
      tapeType: 'CZ-1004',
      tapeWidth: 25.4,
      tapeLengthMm: null,
      connection: this.mode,
      note: 'Detailed config not available'
    };
  }

  async verifyConnection() {
    var result = { alive: false, wifi: null, usb: null };

    // Skip verification if a print is in progress (avoid socket conflicts)
    if (this._busy) {
      return { alive: this.connected, wifi: null, usb: null, busy: true };
    }

    if (this.wifiConnected && this.wifiConnection) {
      try {
        var response = await this.wifiConnection.sendCommand(
          '<read>\n<path>/status.xml</path>\n</read>'
        );
        result.wifi = { alive: true, response: response };
      } catch (e) {
        result.wifi = { alive: false, error: e.message };
        this.wifiConnected = false;
        console.log('[VC-500W] WiFi verify failed:', e.message);
      }
    }

    if (this.usbConnected && this.devicePath) {
      var usbProbe = await usbProbeConnection(this.devicePath);
      result.usb = usbProbe;
      if (!usbProbe.alive) {
        this.usbConnected = false;
        console.log('[VC-500W] USB verify failed:', usbProbe.error);
      }
    }

    result.alive = (result.wifi && result.wifi.alive) || (result.usb && result.usb.alive);
    return result;
  }

  async getStatus() {
    if (this._busy || this._requestBusy) {
      return Object.assign({}, this._cachedPublicStatus, { state: 'PRINTING', busy: true });
    }
    // Try WiFi first (bidirectional ??? can read status)
    if (this.wifiConnected && this.wifiConnection) {
      try {
        var response = await this.wifiConnection.sendCommand(
          '<read>\n<path>/status.xml</path>\n</read>'
        );
        return {
          state: parseXmlValue(response, 'print_state') || 'unknown',
          stage: parseXmlValue(response, 'print_job_stage'),
          error: parseXmlValue(response, 'print_job_error'),
          tapeRemainingMm: parseFloat(parseXmlValue(response, 'remain') || '0') * 25.4,
          connection: this.mode
        };
      } catch (e) {
        console.log('[VC-500W] WiFi status query failed:', e.message);
      }
    }
    // USB-only or WiFi failed
    return {
      state: 'ready',
      connection: this.mode,
      note: 'Detailed status not available over USB'
    };
  }

  // --- Private print methods ---

  async getPublicStatus() {
    var self = this;
    var base = {
      connected: self.connected, mode: self.mode,
      wifiConnected: self.wifiConnected, usbConnected: self.usbConnected,
      completionProtocol: COMPLETION_PROTOCOL
    };
    if (self._busy || self._requestBusy) {
      return Object.assign({}, self._cachedPublicStatus, base,
        { state: 'PRINTING', busy: true, cached: true });
    }
    if (!self.wifiConnected || !self.wifiConnection) {
      return Object.assign(base, { state: 'UNKNOWN', busy: false });
    }
    if (self._publicStatusInFlight) return self._publicStatusInFlight;
    self._publicStatusInFlight = (async function () {
      try {
        var response = await self.wifiConnection.sendCommand(
          '<read>\n<path>/status.xml</path>\n</read>', 1200);
        if (self._busy || self._requestBusy) {
          return Object.assign({}, self._cachedPublicStatus, base,
            { state: 'PRINTING', busy: true, cached: true });
        }
        var config = await self.wifiConnection.sendCommand(
          '<read>\n<path>/config.xml</path>\n</read>', 1200);
        var value = Object.assign({}, base, parsePrintStatus(response), {
          model: parseXmlValue(config, 'model_name'),
          serial: parseXmlValue(config, 'serial_number'),
          firmware: parseXmlValue(config, 'fw_version_number'),
          tapeType: parseXmlValue(config, 'cassette_type'),
          tapeWidthMm: Number(parseXmlValue(config, 'width_inches')) * 25.4,
          tapeLengthMm: Number(parseXmlValue(config, 'media_length_initial')) * 25.4,
          busy: false, cached: false
        });
        if (value.state === 'READY') value.state = 'IDLE';
        self._cachedPublicStatus = value;
        return self._busy || self._requestBusy
          ? Object.assign({}, value, { state: 'PRINTING', busy: true, cached: true }) : value;
      } catch (err) {
        return Object.assign({}, self._cachedPublicStatus, base,
          { state: 'UNKNOWN', error: 'STATUS UNAVAILABLE', busy: false, cached: true });
      } finally {
        self._publicStatusInFlight = null;
      }
    })();
    return self._publicStatusInFlight;
  }

  async _waitForWifiReady(cancelToken, labelIndex) {
    var self = this;

    // If connection is down (e.g. after print-triggered close), wait for reconnect
    if (!self.wifiConnected || !self.wifiConnection) {
      console.log('[VC-500W] WiFi not connected — waiting for reconnect before label', labelIndex + 1);
      for (var w = 0; w < 10; w++) {
        await new Promise(function (r) { setTimeout(r, 1000); });
        if (self.wifiConnected && self.wifiConnection) break;
      }
      if (!self.wifiConnected || !self.wifiConnection) {
        console.log('[VC-500W] Still not connected — attempting reconnect');
        await self._reconnectWifi();
        if (!self.wifiConnected) throw new Error('Printer connection unavailable');
      }
    }

    await new Promise(function (r) { setTimeout(r, 500); });

    var start = Date.now();
    while (Date.now() - start < WIFI_READY_POLL_MAX_WAIT) {
      if (cancelToken && cancelToken.cancelled) throw new Error('Print cancelled');
      var response;
      try {
        response = await self.wifiConnection.sendCommand(
          '<read>\n<path>/status.xml</path>\n</read>', 5000
        );
      } catch (e) {
        console.log('[VC-500W] Status poll error:', e.message, '??? retrying...');
      }
      if (response) {
        var status = parsePrintStatus(response);
        if (status.state === 'ERROR' || (status.error && status.error !== 'NONE')) {
          throw new Error('Printer reported ' + (status.error || status.state));
        }
        if ((status.state === 'IDLE' || status.state === 'READY') && status.error === 'NONE') return;
      }
      await new Promise(function (r) { setTimeout(r, WIFI_READY_POLL_INTERVAL); });
    }
    throw new Error('Printer did not become ready before the deadline');
  }

  async _reconnectWifi() {
    for (var attempt = 0; attempt < 5; attempt++) {
      try {
        if (this.wifiConnection) this.wifiConnection.close();
        this.wifiConnection = new TCPConnection(this.host, this.port);
        this.wifiConnection.driver = this;
        await this.wifiConnection.connect();
        this.wifiConnected = true;
        console.log('[VC-500W] TCP reconnected after print (attempt', attempt + 1 + ')');
        return;
      } catch (e) {
        console.log('[VC-500W] TCP reconnect attempt', attempt + 1, 'failed:', e.message);
        if (this.wifiConnection) { this.wifiConnection.close(); this.wifiConnection = null; }
        await new Promise(function (r) { setTimeout(r, 2000); });
      }
    }
    console.log('[VC-500W] TCP reconnect failed after 5 attempts — will reconnect on next use');
  }

  async _printViaWifi(jpegBuffer, options) {
    var printXml = buildPrintXml(jpegBuffer.length, options);
    var tapeBeforeMm = await this._readTapeRemainMm();
    // Once submission starts, a lost response cannot prove that nothing printed.
    this._dataSentToPrinter = true;
    var response = await this.wifiConnection.sendPrintJob(printXml, jpegBuffer, 15000);
    var code = parseStatusCode(response);
    if (code !== 0) {
      return uncertainPrint('wifi', 'Printer did not acknowledge the submitted image');
    }
    // Preserve the characterized close/reconnect cut sequence. The ACK above
    // only accepts image data; the status observations below confirm completion.
    try {
      this.wifiConnection.close();
      this.wifiConnection = null;
      this.wifiConnected = false;
      await new Promise(function (r) { setTimeout(r, 3000); });
      for (var attempt = 0; attempt < 2; attempt++) {
        try {
          this.wifiConnection = new TCPConnection(this.host, this.port);
          this.wifiConnection.driver = this;
          await this.wifiConnection.connect(2000);
          this.wifiConnected = true;
          break;
        } catch (err) {
          if (this.wifiConnection) this.wifiConnection.close();
          this.wifiConnection = null;
          await new Promise(function (r) { setTimeout(r, 500); });
        }
      }
    } catch (postPrintErr) {
      return uncertainPrint('wifi', postPrintErr.message);
    }
    var verification = await this._verifyPrint(tapeBeforeMm);
    return {
      success: verification.completed, code: verification.completed ? 0 : -2,
      response: response, connection: 'wifi', verified: verification.verified,
      completed: verification.completed, uncertain: !verification.completed,
      verification: verification,
      error: verification.completed ? null : verification.reason
    };
  }

  async _refreshUsbIfNeeded() {
    if (!this.usbConnected || !this.devicePath) return;
    var probe = await usbProbeConnection(this.devicePath);
    if (!probe.alive) {
      console.log('[VC-500W] USB handle stale, re-discovering...');
      if (probe.devicePath && probe.devicePath !== this.devicePath) {
        this.devicePath = probe.devicePath;
        console.log('[VC-500W] USB path refreshed:', this.devicePath);
      } else {
        this.usbConnected = false;
        this.devicePath = null;
        console.log('[VC-500W] USB no longer available');
      }
    }
  }

  async _printViaUsb(jpegBuffer, options) {
    await this._refreshUsbIfNeeded();
    if (!this.usbConnected || !this.devicePath) {
      throw new Error('USB printer disconnected after refresh');
    }
    // Read tape-remaining BEFORE the write so we can sanity-check after.
    // Best effort — only works when WiFi is also connected.
    var tapeBeforeMm = await this._readTapeRemainMm();

    var tmpJpeg = path.join(os.tmpdir(), 'sunburn-jpeg-' + Date.now() + '.jpg');
    fs.writeFileSync(tmpJpeg, jpegBuffer);
    // The helper can write bytes before its process or response is lost.
    this._dataSentToPrinter = true;
    try {
      var result = await usbDirectPrint(this.devicePath, tmpJpeg, options);
      try { fs.unlinkSync(tmpJpeg); } catch (e) {}
      console.log('[VC-500W] USB print result:', result);
      var hasError = result.indexOf('ERROR:') >= 0;
      // PRINT_OK from the C# helper means "WriteFile() succeeded" —
      // Windows accepted the bytes. It does NOT mean the printer printed.
      // Treat it as a "data sent" signal, not a success signal.
      var dataSent = !hasError && (result.indexOf('PRINT_OK') >= 0 || result.indexOf('BATCH_OK') >= 0);
      if (hasError) {
        var errorDetail = result.match(/ERROR:(.*)/);
        var errorMsg = errorDetail ? errorDetail[1].trim() : 'Unknown USB error';
        console.error('[VC-500W] USB error detail:', errorMsg);
        return {
          success: false, code: -1, response: result,
          error: errorMsg, connection: 'usb'
        };
      }
      if (!dataSent) {
        return {
          success: false, code: -1, response: result,
          error: 'USB write did not return PRINT_OK', connection: 'usb'
        };
      }

      // Bytes hit the printer's USB endpoint — set _dataSentToPrinter NOW.
      // Even if verification later says "couldn't see busy state", the printer
      // has already received the job and will print when ready. Retrying via
      // WiFi at this point would double-print.
      this._dataSentToPrinter = true;

      var verify = await this._verifyPrint(tapeBeforeMm);
      return {
        success: verify.completed, code: verify.completed ? 0 : -2,
        response: result, connection: 'usb', verified: verify.verified,
        completed: verify.completed, uncertain: !verify.completed,
        verification: verify, error: verify.completed ? null : verify.reason
      };
    } catch (e) {
      try { fs.unlinkSync(tmpJpeg); } catch (e2) {}
      throw e;
    }
  }

  _buildChannelOrder() {
    // Prefer WiFi: it has bidirectional status (busy/idle/tape-remain) so
    // we can VERIFY the printer actually printed. USB is fire-and-forget
    // (the C# helper writes bytes to the kernel pipe and trusts the
    // printer); we only fall back there when WiFi is unavailable. After
    // a USB print we still try to verify via WiFi if it comes back up.
    var channels = [];
    if (this.wifiConnected) channels.push('wifi');
    if (this.usbConnected && this.devicePath) channels.push('usb');
    if (this._lastFailedChannel && channels.length > 1) {
      var demoted = this._lastFailedChannel;
      var now = Date.now();
      // Demotion window dropped 5 min → 30 s. The previous 5-minute window
      // meant a single transient WiFi blip locked us into unverified USB
      // for 5 min, swallowing print failures.
      if (this._lastFailedTime && (now - this._lastFailedTime) < 30000) {
        channels = channels.filter(function (c) { return c !== demoted; });
        channels.push(demoted);
        console.log('[VC-500W] Demoting', demoted, 'after recent failure, order:', channels.join(' > '));
      }
    }
    return channels;
  }

  // Activity and completion are separate. Require two healthy idle samples
  // after activity; ACK, warming, cooling and error states are not completion.
  async _verifyPrint(tapeBeforeMm) {
    if (!this.wifiConnected || !this.wifiConnection) {
      return { verified: null, completed: false, reason: 'WiFi unavailable — completion unknown' };
    }
    var pollStart = Date.now();
    var startedAtUtc = new Date().toISOString();
    var maxWait = 60000;
    var sawBusy = false;
    var settled = false;
    var stableIdle = 0;
    var tapeAfterMm = null;
    var tapeUsedMm = null;
    var activity = false;
    var reason = 'Printer completion not observed before the deadline';
    var observations = [];
    var deadline = pollStart + maxWait;
    var epoch = this._connectionEpoch;
    while (Date.now() < deadline && this._connectionEpoch === epoch) {
      try {
        // A timed-out read destroys its socket because replies have no IDs.
        // Recover only the status channel; never resend image or cut commands.
        if (!this.wifiConnected || !this.wifiConnection) {
          if (this.wifiConnection) this.wifiConnection.close();
          this.wifiConnection = new TCPConnection(this.host, this.port);
          this.wifiConnection.driver = this;
          var connection = this.wifiConnection;
          var connectRemaining = deadline - Date.now();
          if (connectRemaining <= 0) break;
          await connection.connect(Math.min(2000, connectRemaining));
          if (this._connectionEpoch !== epoch) { connection.close(); break; }
          this.wifiConnected = true;
        }
        var remaining = deadline - Date.now();
        if (remaining <= 0) break;
        var response = await this.wifiConnection.sendCommand(
          '<read>\n<path>/status.xml</path>\n</read>', Math.min(1500, remaining)
        );
        if (Date.now() >= deadline || this._connectionEpoch !== epoch) break;
        var status = parsePrintStatus(response);
        var previous = observations[observations.length - 1];
        if (!previous || previous.state !== status.state || previous.stage !== status.stage ||
            previous.error !== status.error || previous.tapeRemainingMm !== status.tapeRemainingMm) {
          observations.push(Object.assign({ elapsedMs: Date.now() - pollStart }, status));
        }
        tapeAfterMm = status.tapeRemainingMm;
        tapeUsedMm = tapeBeforeMm != null && tapeAfterMm != null ? tapeBeforeMm - tapeAfterMm : null;
        if (status.state === 'ERROR' || (status.error && status.error !== 'NONE')) {
          reason = 'Printer reported ' + (status.error || status.state) + '; completion unknown';
          break;
        }
        // This VC-500W reports BUSY with a separate PRINTING/EJECTING stage.
        // PREPARING PRINT and PREHEAT alone do not prove that a label printed.
        if (['PRINTING', 'FEEDING', 'CUTTING'].indexOf(status.state) !== -1 ||
            (status.state === 'BUSY' && ['PRINTING', 'EJECTING'].indexOf(status.stage) !== -1)) sawBusy = true;
        activity = sawBusy || (tapeUsedMm != null && tapeUsedMm >= 5);
        var healthyIdle = (status.state === 'IDLE' || status.state === 'READY') &&
          status.stage === 'SUCCESS' && status.error === 'NONE';
        stableIdle = activity && healthyIdle ? stableIdle + 1 : 0;
        if (stableIdle >= 2) {
          settled = true;
          reason = 'Print activity followed by stable idle, successful stage and no printer error';
          this._cachedPublicStatus = Object.assign({}, this._cachedPublicStatus, status);
          break;
        }
      } catch (e) {
        if (this._connectionEpoch !== epoch) break;
        stableIdle = 0;
        reason = 'Status unavailable after submission; completion unknown';
        this.wifiConnected = false;
        if (this.wifiConnection) this.wifiConnection.close();
        this.wifiConnection = null;
      }
      var delay = Math.min(1000, deadline - Date.now());
      if (delay > 0) await new Promise(function (r) { setTimeout(r, delay); });
    }
    return { verified: activity, completed: settled, sawBusy: sawBusy, settled: settled,
             tapeBeforeMm: tapeBeforeMm, tapeAfterMm: tapeAfterMm,
             tapeUsedMm: tapeUsedMm, reason: reason, startedAtUtc: startedAtUtc,
             elapsedMs: Date.now() - pollStart, observations: observations };
  }

  async _readTapeRemainMm() {
    if (!this.wifiConnected || !this.wifiConnection) return null;
    try {
      var response = await this.wifiConnection.sendCommand(
        '<read>\n<path>/status.xml</path>\n</read>', 1500
      );
      return parsePrintStatus(response).tapeRemainingMm;
    } catch (e) {
      return null;
    }
  }

  _demoteChannel(channel) {
    this._lastFailedChannel = channel;
    this._lastFailedTime = Date.now();
  }

  async printJpeg(jpegBuffer, options) {
    options = options || {};
    if (this._busy || (this._requestBusy && !options.requestLease)) {
      throw new Error('A print is already in progress');
    }
    var quality = options.quality || 'vivid';
    var cutMode = options.cutMode || 'full';
    var imgWidth = options.imgWidth || 0;
    var imgHeight = options.imgHeight || 0;

    if (CUT_MODES.indexOf(cutMode) === -1) {
      cutMode = 'full';
    }

    var printOpts = { quality: quality, cutMode: cutMode, imgWidth: imgWidth, imgHeight: imgHeight };

    console.log('[VC-500W] Print:', {
      jpegSize: jpegBuffer.length,
      quality: quality,
      cutMode: cutMode,
      mode: this.mode
    });

    var channels = this._buildChannelOrder();

    if (channels.length === 0) {
      throw new Error('No printer connection available');
    }

    this._busy = true;
    this._dataSentToPrinter = false;
    var lastError = null;

    try {
      if (this._publicStatusInFlight) await this._publicStatusInFlight;
      for (var i = 0; i < channels.length; i++) {
        var channel = channels[i];
        try {
          var result;
          if (channel === 'wifi') {
            result = await this._printViaWifi(jpegBuffer, printOpts);
          } else {
            result = await this._printViaUsb(jpegBuffer, printOpts);
          }
          if (result.success) {
            return result;
          }
          if (this._dataSentToPrinter) {
            return Object.assign({}, result, { completed: false, uncertain: true });
          }
          lastError = new Error('Print returned failure on ' + channel + ': ' + (result.error || result.response || ''));
          console.log('[VC-500W] Print failed on', channel, '— trying next channel...');
          this._demoteChannel(channel);
        } catch (e) {
          console.log('[VC-500W] Print error on', channel + ':', e.message);
          lastError = e;
          this._demoteChannel(channel);
          if (channel === 'wifi') this.wifiConnected = false;
          if (channel === 'usb') this.usbConnected = false;
          // If data was already sent to the printer (WiFi sent JPEG before response timeout),
          // do NOT retry on another channel — the printer is already printing physically.
          if (this._dataSentToPrinter) {
            return uncertainPrint(channel, 'Submission response lost; completion unknown: ' + e.message);
          }
        }
      }
    } finally {
      this._busy = false;
    }

    throw lastError || new Error('Print failed on all channels');
  }

  async printBatch(labels, options, cancelToken) {
    options = options || {};
    if (this._busy || (this._requestBusy && !options.requestLease)) {
      throw new Error('A print is already in progress');
    }
    var quality = options.quality || 'vivid';
    var finalCutMode = options.finalCutMode || 'full';

    console.log('[VC-500W] Batch print:', labels.length, 'labels, mode:', this.mode);

    var channels = this._buildChannelOrder();

    if (channels.length === 0) {
      throw new Error('No printer connection available');
    }

    this._busy = true;
    this._dataSentToPrinter = false;
    var lastError = null;

    try {
      for (var ci = 0; ci < channels.length; ci++) {
        var channel = channels[ci];
        try {
          if (cancelToken && cancelToken.cancelled) throw new Error('Print cancelled');

          if (channel === 'usb') {
            await this._refreshUsbIfNeeded();
            if (!this.usbConnected || !this.devicePath) {
              throw new Error('USB printer disconnected after refresh');
            }
            var jobs = [];
            for (var i = 0; i < labels.length; i++) {
              var tmpJpeg = path.join(os.tmpdir(), 'sunburn-batch-' + Date.now() + '-' + i + '.jpg');
              fs.writeFileSync(tmpJpeg, labels[i].jpegBuffer);
              jobs.push({
                jpegPath: tmpJpeg,
                quality: quality,
                cutMode: labels[i].cutMode || 'full',
                finalCutMode: finalCutMode,
                imgWidth: labels[i].imgWidth || 0,
                imgHeight: labels[i].imgHeight || 0
              });
            }
            try {
              this._dataSentToPrinter = true;
              var result = await usbDirectBatchPrint(this.devicePath, jobs);
              jobs.forEach(function (j) { try { fs.unlinkSync(j.jpegPath); } catch (e) {} });
              console.log('[VC-500W] USB batch result:', result);
              var hasError = result.indexOf('ERROR:') >= 0;
              if (hasError) {
                this._demoteChannel('usb');
              }
              return {
                success: !hasError && result.indexOf('BATCH_OK') >= 0,
                response: result,
                error: hasError ? (result.match(/ERROR:(.*)/)||[])[1] : null,
                connection: 'usb'
              };
            } catch (e) {
              jobs.forEach(function (j) { try { fs.unlinkSync(j.jpegPath); } catch (e2) {} });
              throw e;
            }
          } else {
            var results = [];
            var printed = 0;
            for (var i = 0; i < labels.length; i++) {
              if (cancelToken && cancelToken.cancelled) {
                console.log('[VC-500W] Batch cancelled after', i, 'of', labels.length, 'labels');
                return {
                  success: false,
                  cancelled: true,
                  printed: printed,
                  total: labels.length,
                  results: results,
                  connection: 'wifi'
                };
              }
              if (i > 0) {
                console.log('[VC-500W] Waiting for printer ready before label', i + 1, '...');
                await this._waitForWifiReady(cancelToken, i);
              }
              var isLast = (i === labels.length - 1);
              var cut = isLast ? finalCutMode : (labels[i].cutMode || 'full');
              console.log('[VC-500W] Printing label', i + 1, 'of', labels.length);
              var r = await this._printViaWifi(labels[i].jpegBuffer, {
                quality: quality,
                cutMode: cut,
                imgWidth: labels[i].imgWidth || 0,
                imgHeight: labels[i].imgHeight || 0
              });
              if (!r.success) {
                results.push(r);
                return {
                  success: false, completed: false, uncertain: true,
                  printed: printed, total: labels.length,
                  results: results, connection: 'wifi'
                };
              }
              results.push(r);
              printed++;
              if (this._batchProgressCallback) {
                this._batchProgressCallback({ printed: printed, total: labels.length, labelIndex: i });
              }
            }
            return {
              success: results.every(function (r) { return r.success; }),
              printed: printed,
              total: labels.length,
              results: results,
              connection: 'wifi'
            };
          }
        } catch (e) {
          console.log('[VC-500W] Batch print error on', channel + ':', e.message);
          lastError = e;
          this._demoteChannel(channel);
          if (channel === 'wifi') this.wifiConnected = false;
          if (channel === 'usb') this.usbConnected = false;
          if (this._dataSentToPrinter) {
            return Object.assign(uncertainPrint(channel, e.message), {
              printed: printed || 0, total: labels.length, results: results || []
            });
          }
        }
      }
    } finally {
      this._busy = false;
    }

    throw lastError || new Error('Batch print failed on all channels');
  }

  // Attempt to reconnect any dropped channels in the background
  async _tryReconnect() {
    if (this._reconnecting || this._connecting || this._busy || this._requestBusy) return;
    this._reconnecting = true;
    var epoch = ++this._connectionEpoch;

    try {
      var discovery = await discoverPrinter({ savedIp: this.host || undefined });
      this._requireConnection(epoch);
      if (this._busy || this._requestBusy) return;

      if (!this.wifiConnected && discovery.wifi) {
        try {
          this.host = discovery.wifi.host;
          this.port = discovery.wifi.port || 9100;
          if (this.wifiConnection) this.wifiConnection.close();
          this.wifiConnection = new TCPConnection(this.host, this.port);
          this.wifiConnection.driver = this;
          var connection = this.wifiConnection;
          await connection.connect();
          if (this._connectionEpoch !== epoch) connection.close();
          this._requireConnection(epoch);
          this.wifiConnected = true;
          console.log('[VC-500W] WiFi reconnected to', this.host);
        } catch (e) {
          this._requireConnection(epoch);
          console.log('[VC-500W] WiFi reconnect failed:', e.message);
        }
      }

      if (this._busy || this._requestBusy) return;
      if (!this.usbConnected && discovery.usb) {
        try {
          if (this.usbConnection) this.usbConnection.close();
          this.usbConnection = new DirectUSBConnection();
          var connection = this.usbConnection;
          await connection.connect();
          if (this._connectionEpoch !== epoch) connection.close();
          this._requireConnection(epoch);
          this.devicePath = this.usbConnection.devicePath;
          this.usbConnected = true;
          console.log('[VC-500W] USB reconnected');
        } catch (e) {
          this._requireConnection(epoch);
          console.log('[VC-500W] USB reconnect failed:', e.message);
        }
      }
    } finally {
      if (this._connectionEpoch === epoch) this._reconnecting = false;
    }
  }

  close() {
    this._connectionEpoch++;
    this._connecting = false;
    this._reconnecting = false;
    if (this.wifiConnection) {
      this.wifiConnection.close();
      this.wifiConnection = null;
    }
    if (this.usbConnection) {
      this.usbConnection.close();
      this.usbConnection = null;
    }
    this.wifiConnected = false;
    this.usbConnected = false;
  }
}

// ---------- HTTP Helpers (for WiFi setup web interface) ----------

function httpGet(url, options) {
  options = options || {};
  return new Promise(function (resolve, reject) {
    var parsed = new URL(url);
    var reqOpts = {
      hostname: parsed.hostname,
      port: parsed.port || 80,
      path: parsed.pathname + parsed.search,
      method: 'GET',
      timeout: options.timeout || 10000,
      headers: {}
    };
    if (options.auth) {
      reqOpts.headers['Authorization'] = 'Basic ' +
        Buffer.from(options.auth.user + ':' + options.auth.pass).toString('base64');
    }

    var req = http.request(reqOpts, function (res) {
      // Follow redirects (up to 5)
      if ((res.statusCode === 301 || res.statusCode === 302 || res.statusCode === 303) && res.headers.location) {
        var redirects = (options._redirects || 0) + 1;
        if (redirects > 5) {
          reject(new Error('Too many redirects'));
          return;
        }
        var nextUrl = res.headers.location;
        if (nextUrl.startsWith('/')) {
          nextUrl = parsed.protocol + '//' + parsed.host + nextUrl;
        }
        resolve(httpGet(nextUrl, Object.assign({}, options, { _redirects: redirects })));
        return;
      }

      var body = '';
      res.setEncoding('utf8');
      res.on('data', function (chunk) { body += chunk; });
      res.on('end', function () { resolve(body); });
    });
    req.on('error', reject);
    req.on('timeout', function () {
      req.destroy();
      reject(new Error('HTTP GET timeout'));
    });
    req.end();
  });
}

function httpPost(url, body, contentType, options) {
  options = options || {};
  return new Promise(function (resolve, reject) {
    var parsed = new URL(url);
    var postData = typeof body === 'string' ? body : '';
    var reqOpts = {
      hostname: parsed.hostname,
      port: parsed.port || 80,
      path: parsed.pathname + parsed.search,
      method: 'POST',
      timeout: options.timeout || 10000,
      headers: {
        'Content-Type': contentType || 'application/x-www-form-urlencoded',
        'Content-Length': Buffer.byteLength(postData)
      }
    };
    if (options.auth) {
      reqOpts.headers['Authorization'] = 'Basic ' +
        Buffer.from(options.auth.user + ':' + options.auth.pass).toString('base64');
    }

    var req = http.request(reqOpts, function (res) {
      var responseBody = '';
      res.setEncoding('utf8');
      res.on('data', function (chunk) { responseBody += chunk; });
      res.on('end', function () { resolve(responseBody); });
    });
    req.on('error', reject);
    req.on('timeout', function () {
      req.destroy();
      reject(new Error('HTTP POST timeout'));
    });
    req.write(postData);
    req.end();
  });
}

/**
 * Probe the printer's web management interface at 192.168.0.1
 * to find the WiFi setup form. Follows links matching wireless/wifi/network
 * keywords, then parses <form> elements and input fields.
 *
 * Returns { actionUrl, fields: [{ name, type, value }] } or throws.
 */
async function probeWifiSetupForm(options) {
  var baseUrl = 'http://192.168.0.1';
  var authOpts = options && options.auth ? { auth: options.auth } : {};

  console.log('[WiFi Probe] Fetching root page...');
  var rootHtml = await httpGet(baseUrl + '/', authOpts);

  // Look for links to wireless/wifi/network setup pages
  var linkPattern = /<a\s[^>]*href=["']([^"']+)["'][^>]*>/gi;
  var candidateUrls = [];
  var linkMatch;
  while ((linkMatch = linkPattern.exec(rootHtml)) !== null) {
    var href = linkMatch[1];
    var fullTag = linkMatch[0].toLowerCase();
    if (/wi-?fi|wireless|network|wlan/i.test(href) || /wi-?fi|wireless|network|wlan/i.test(fullTag)) {
      candidateUrls.push(href);
    }
  }

  // Also check the root page itself for a form
  var pages = [{ url: '/', html: rootHtml }];

  // Fetch candidate pages
  for (var ci = 0; ci < candidateUrls.length; ci++) {
    var candidateUrl = candidateUrls[ci];
    if (!candidateUrl.startsWith('http')) {
      candidateUrl = candidateUrl.startsWith('/') ? baseUrl + candidateUrl : baseUrl + '/' + candidateUrl;
    }
    try {
      console.log('[WiFi Probe] Checking:', candidateUrl);
      var pageHtml = await httpGet(candidateUrl, authOpts);
      pages.push({ url: candidateUrl, html: pageHtml });
    } catch (e) {
      console.log('[WiFi Probe] Failed to fetch', candidateUrl, e.message);
    }
  }

  // Search all fetched pages for forms with SSID-related inputs
  for (var pi = 0; pi < pages.length; pi++) {
    var page = pages[pi];
    var formResult = parseWifiForm(page.html, page.url, baseUrl);
    if (formResult) {
      console.log('[WiFi Probe] Found WiFi form on', page.url, 'action:', formResult.actionUrl);
      return formResult;
    }
  }

  // Second pass: look for any form on wireless-related pages
  for (var pi2 = 0; pi2 < pages.length; pi2++) {
    if (pi2 === 0) continue; // skip root page in second pass
    var page2 = pages[pi2];
    var anyForm = parseAnyForm(page2.html, page2.url, baseUrl);
    if (anyForm) {
      console.log('[WiFi Probe] Found form on wireless page', page2.url, 'action:', anyForm.actionUrl);
      return anyForm;
    }
  }

  throw new Error('Could not find WiFi setup form on printer web interface');
}

function parseWifiForm(html, pageUrl, baseUrl) {
  // Find forms
  var formPattern = /<form\s[^>]*>([\s\S]*?)<\/form>/gi;
  var formMatch;
  while ((formMatch = formPattern.exec(html)) !== null) {
    var formTag = formMatch[0];
    var formBody = formMatch[1];
    // Check if this form has SSID-related inputs
    if (/ssid|network.?name|wireless/i.test(formBody)) {
      var actionMatch = formTag.match(/action=["']([^"']*?)["']/i);
      var actionUrl = actionMatch ? actionMatch[1] : pageUrl;
      if (!actionUrl.startsWith('http')) {
        actionUrl = actionUrl.startsWith('/') ? baseUrl + actionUrl : baseUrl + '/' + actionUrl;
      }
      var fields = parseFormFields(formBody);
      return { actionUrl: actionUrl, fields: fields };
    }
  }
  return null;
}

function parseAnyForm(html, pageUrl, baseUrl) {
  var formPattern = /<form\s[^>]*>([\s\S]*?)<\/form>/gi;
  var formMatch = formPattern.exec(html);
  if (formMatch) {
    var formTag = formMatch[0];
    var formBody = formMatch[1];
    var actionMatch = formTag.match(/action=["']([^"']*?)["']/i);
    var actionUrl = actionMatch ? actionMatch[1] : pageUrl;
    if (!actionUrl.startsWith('http')) {
      actionUrl = actionUrl.startsWith('/') ? baseUrl + actionUrl : baseUrl + '/' + actionUrl;
    }
    var fields = parseFormFields(formBody);
    return { actionUrl: actionUrl, fields: fields };
  }
  return null;
}

function parseFormFields(formBody) {
  var fields = [];
  // Parse <input> elements
  var inputPattern = /<input\s[^>]*>/gi;
  var inputMatch;
  while ((inputMatch = inputPattern.exec(formBody)) !== null) {
    var tag = inputMatch[0];
    var name = (tag.match(/name=["']([^"']*?)["']/i) || [])[1];
    var type = (tag.match(/type=["']([^"']*?)["']/i) || [])[1] || 'text';
    var value = (tag.match(/value=["']([^"']*?)["']/i) || [])[1] || '';
    if (name) {
      fields.push({ name: name, type: type.toLowerCase(), value: value });
    }
  }
  // Parse <select> elements
  var selectPattern = /<select\s[^>]*name=["']([^"']*?)["'][^>]*>[\s\S]*?<\/select>/gi;
  var selectMatch;
  while ((selectMatch = selectPattern.exec(formBody)) !== null) {
    var selectName = selectMatch[1];
    // Try to find selected option
    var selectedOpt = (selectMatch[0].match(/<option[^>]*selected[^>]*value=["']([^"']*?)["']/i) || [])[1];
    fields.push({ name: selectName, type: 'select', value: selectedOpt || '' });
  }
  return fields;
}

/**
 * Submit WiFi credentials to the printer's web setup form.
 * Fills SSID and password into the parsed form fields, then POSTs.
 */
async function submitWifiSetupForm(actionUrl, fields, ssid, password, options) {
  var authOpts = options && options.auth ? { auth: options.auth } : {};

  // Build form data, filling in SSID and password
  var params = [];
  var ssidFilled = false;
  var passFilled = false;

  for (var i = 0; i < fields.length; i++) {
    var f = fields[i];
    var val = f.value;

    // Detect SSID field
    if (!ssidFilled && /ssid|network.?name/i.test(f.name)) {
      val = ssid;
      ssidFilled = true;
    }
    // Detect password field
    else if (!passFilled && /pass|key|psk|secret|wpa/i.test(f.name)) {
      val = password;
      passFilled = true;
    }
    // Detect password by input type
    else if (!passFilled && f.type === 'password') {
      val = password;
      passFilled = true;
    }

    // Skip submit buttons
    if (f.type === 'submit' || f.type === 'button') continue;

    params.push(encodeURIComponent(f.name) + '=' + encodeURIComponent(val));
  }

  // If SSID/pass fields weren't auto-detected, try common field names
  if (!ssidFilled) {
    params.push('ssid=' + encodeURIComponent(ssid));
  }
  if (!passFilled) {
    params.push('passphrase=' + encodeURIComponent(password));
  }

  var postBody = params.join('&');
  console.log('[WiFi Probe] Submitting to:', actionUrl, '(fields:', params.length + ')');

  var response = await httpPost(actionUrl, postBody, 'application/x-www-form-urlencoded', authOpts);
  return response;
}

module.exports = {
  VC500WDriver,
  TCPConnection,
  DirectUSBConnection,
  discoverPrinter,
  runPowerShellInline,
  httpGet,
  httpPost,
  probeWifiSetupForm,
  submitWifiSetupForm,
  PRINT_MODES,
  CUT_MODES
};
