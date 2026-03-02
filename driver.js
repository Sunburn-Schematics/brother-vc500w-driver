/**
 * Brother VC-500W WiFi Driver
 *
 * Cross-platform TCP driver for the Brother VC-500W color label printer.
 * Communicates via XML commands over a raw TCP socket on port 9100.
 *
 * Usage:
 *   const { VC500WDriver } = require('./driver');
 *   const printer = new VC500WDriver({ host: '192.168.1.243' });
 *   await printer.connect();
 *   await printer.printJpeg(jpegBuffer, { quality: 'vivid', cutMode: 'full' });
 *   printer.close();
 */

'use strict';

var net = require('net');
var os = require('os');
var fs = require('fs');

var XML_PREFIX = '<?xml version="1.0" encoding="UTF-8"?>\n';

/**
 * Print quality modes.
 * - vivid: 317 LPI, slower, best color reproduction
 * - normal: 264 LPI, faster, slightly less vibrant
 */
var PRINT_MODES = {
  vivid: { name: 'vivid', speed: 0, lpi: 317 },
  normal: { name: 'color', speed: 1, lpi: 264 }
};

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

// ---------- TCP Connection ----------

/**
 * Low-level TCP connection to the printer.
 * Commands are serialized via an internal queue to prevent interleaving.
 */
class TCPConnection {
  constructor(host, port) {
    this.host = host;
    this.port = port || 9100;
    this.socket = null;
    this._cmdQueue = Promise.resolve();
  }

  /**
   * Open the TCP connection.
   * @param {number} [timeout=5000] Connection timeout in ms
   */
  connect(timeout) {
    var self = this;
    return new Promise(function (resolve, reject) {
      var settled = false;
      self.socket = new net.Socket();
      self.socket.setTimeout(timeout || 5000);
      self.socket.connect(self.port, self.host, function () {
        settled = true;
        self.socket.setKeepAlive(true, 30000);
        self.socket.setTimeout(0);
        resolve();
      });
      self.socket.on('error', function (err) {
        if (!settled) { settled = true; reject(err); }
      });
      self.socket.on('timeout', function () {
        self.socket.destroy();
        if (!settled) { settled = true; reject(new Error('Connection timeout')); }
      });
    });
  }

  /**
   * Send an XML command and wait for the response.
   * Responses are detected by closing tags: </status>, </config>, </lock>.
   */
  sendCommand(xml, timeout) {
    var self = this;
    self._cmdQueue = self._cmdQueue.then(
      function () { return self._sendCommandInner(xml, timeout); },
      function () { return self._sendCommandInner(xml, timeout); }
    );
    return self._cmdQueue;
  }

  _sendCommandInner(xml, timeout) {
    var self = this;
    return new Promise(function (resolve, reject) {
      var fullXml = XML_PREFIX + xml;
      var responseData = '';
      var timer = setTimeout(function () {
        if (self.socket) self.socket.removeListener('data', onData);
        reject(new Error('Response timeout'));
      }, timeout || 10000);

      function onData(data) {
        responseData += data.toString('utf8');
        if (responseData.includes('</status>') ||
            responseData.includes('</config>') ||
            responseData.includes('</lock>')) {
          clearTimeout(timer);
          if (self.socket) self.socket.removeListener('data', onData);
          resolve(responseData);
        }
      }
      self.socket.on('data', onData);
      self.socket.write(fullXml, 'utf8');
    });
  }

  /**
   * Send the print XML command, then raw JPEG data, and wait for completion.
   * This is an atomic operation — XML + JPEG + response in one queue slot.
   */
  sendPrintJob(xml, jpegBuffer, timeout) {
    var self = this;
    self._cmdQueue = self._cmdQueue.then(
      function () { return self._sendPrintJobInner(xml, jpegBuffer, timeout); },
      function () { return self._sendPrintJobInner(xml, jpegBuffer, timeout); }
    );
    return self._cmdQueue;
  }

  _sendPrintJobInner(xml, jpegBuffer, timeout) {
    var self = this;
    var printTimeout = timeout || 120000;

    // Step 1: Send print XML, wait for "ready" status
    return self._sendCommandInner(xml, 30000).then(function (readyResponse) {
      var initCode = parseStatusCode(readyResponse);
      var comment = parseXmlValue(readyResponse, 'comment') || '';
      if (initCode > 0) {
        throw new Error('Printer not ready (code ' + initCode + '): ' + comment);
      }

      // Step 2: Send raw JPEG bytes
      return new Promise(function (resolve, reject) {
        self.socket.write(jpegBuffer, function (err) {
          if (err) reject(err);
          else resolve();
        });
      });
    }).then(function () {
      // Step 3: Wait for print completion status
      return new Promise(function (resolve, reject) {
        var responseData = '';
        var timer = setTimeout(function () {
          if (self.socket) self.socket.removeListener('data', onData);
          reject(new Error('Print response timeout'));
        }, printTimeout);

        function onData(data) {
          responseData += data.toString('utf8');
          if (responseData.includes('</status>')) {
            clearTimeout(timer);
            if (self.socket) self.socket.removeListener('data', onData);
            resolve(responseData);
          }
        }
        self.socket.on('data', onData);
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

// ---------- USB Connection ----------

/**
 * USB connection to the printer via driver-usb.js.
 * Write-only: status/config reads are not supported over USB.
 */
class USBConnection {
  constructor(devicePath) {
    this.devicePath = devicePath;
  }

  connect() {
    var usb = require('./driver-usb');
    return usb.checkConnection(this.devicePath).then(function (r) {
      if (!r.alive) throw new Error('USB printer not connected');
    });
  }

  sendCommand(xml, timeout) {
    throw new Error('Status/config reads not supported over USB');
  }

  sendPrintJob(xml, jpegBuffer, timeout) {
    var usb = require('./driver-usb');
    return usb.printBuffer(this.devicePath, jpegBuffer, {}).then(function (r) {
      if (!r.success) throw new Error('USB print failed: ' + r.output);
      return '<status><code>0</code></status>';
    });
  }

  close() {
    // no-op — USB connections are per-operation
  }
}

// ---------- Print XML Builder ----------

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

// ---------- Network Discovery ----------

/**
 * Scan a /24 subnet for devices listening on port 9100,
 * then verify each is a Brother VC-500W by querying config.
 */
async function scanSubnetForPrinter(prefix) {
  var BATCH_SIZE = 50;
  var CONNECT_TIMEOUT = 500;
  var SCAN_TOTAL_TIMEOUT = 10000;

  console.log('[scanSubnet] Scanning subnet ' + prefix + '.0/24 (timeout=' + SCAN_TOTAL_TIMEOUT + 'ms)');

  var scanStart = Date.now();

  function probeHost(ip) {
    var socketPromise = new Promise(function (resolve) {
      var settled = false;
      var socket = new net.Socket();
      socket.setTimeout(CONNECT_TIMEOUT);
      socket.connect(9100, ip, function () {
        if (!settled) { settled = true; console.log('[scanSubnet] ' + ip + ' -> OPEN'); resolve(ip); }
        socket.destroy();
      });
      socket.on('error', function () {
        if (!settled) { settled = true; resolve(null); }
        socket.destroy();
      });
      socket.on('timeout', function () {
        if (!settled) { settled = true; resolve(null); }
        socket.destroy();
      });
    });
    var hardTimeout = new Promise(function (resolve) {
      setTimeout(function () { resolve(null); }, CONNECT_TIMEOUT + 200);
    });
    return Promise.race([socketPromise, hardTimeout]);
  }

  var totalResponders = 0;
  for (var start = 1; start <= 254; start += BATCH_SIZE) {
    if (Date.now() - scanStart > SCAN_TOTAL_TIMEOUT) {
      console.log('[scanSubnet] Total scan timeout reached (' + SCAN_TOTAL_TIMEOUT + 'ms), aborting.');
      break;
    }
    var end = Math.min(start + BATCH_SIZE - 1, 254);
    console.log('[scanSubnet] Probing batch ' + prefix + '.' + start + ' - ' + prefix + '.' + end);
    var batch = [];
    for (var i = start; i < start + BATCH_SIZE && i <= 254; i++) {
      batch.push(probeHost(prefix + '.' + i));
    }
    var results = await Promise.all(batch);
    for (var r = 0; r < results.length; r++) {
      if (results[r]) {
        totalResponders++;
        console.log('[scanSubnet] Port 9100 open on ' + results[r] + ', verifying if Brother VC-500W...');
        // Verify it's a Brother printer
        try {
          var conn = new TCPConnection(results[r], 9100);
          await conn.connect(2000);
          var resp = await conn.sendCommand('<read>\n<path>/config.xml</path>\n</read>', 3000);
          conn.close();
          var model = parseXmlValue(resp, 'model_name');
          if (model && (model.indexOf('VC-500W') >= 0 || model === 'Wedge')) {
            console.log('[scanSubnet] Found VC-500W at ' + results[r] + ' (model=' + model + ')');
            return results[r];
          } else {
            console.log('[scanSubnet] ' + results[r] + ' responded but model=' + (model || '(none)') + ', not VC-500W');
          }
        } catch (e) {
          console.log('[scanSubnet] ' + results[r] + ' verification failed: ' + e.message);
        }
      }
    }
  }
  console.log('[scanSubnet] Scan complete in ' + (Date.now() - scanStart) + 'ms. Hosts with port 9100 open: ' + totalResponders + '. No VC-500W found.');
  return null;
}

/**
 * Discover a Brother VC-500W on the local network.
 * Tries hostname first, then saved IP, then subnet scan.
 *
 * @param {object} [options]
 * @param {string} [options.hostname] - mDNS hostname (e.g., 'BRVC-500W-5843')
 * @param {string} [options.savedIp] - Previously known IP address
 * @returns {Promise<{host: string, port: number}|null>}
 */
async function discoverPrinter(options) {
  options = options || {};
  console.log('[discover] Starting printer discovery (hostname=' + (options.hostname || 'none') + ', savedIp=' + (options.savedIp || 'none') + ')');

  // Tier 1: Try hostname
  var hosts = [];
  if (options.hostname) hosts.push(options.hostname);
  if (options.savedIp) hosts.push(options.savedIp);

  if (hosts.length === 0) {
    console.log('[discover] Tier 1: No hostname or savedIp provided, skipping direct connect');
  }

  for (var hi = 0; hi < hosts.length; hi++) {
    console.log('[discover] Tier 1: Trying direct connect to ' + hosts[hi] + ':9100...');
    try {
      var socket = new net.Socket();
      var ip = await new Promise(function (resolve) {
        socket.setTimeout(2000);
        socket.connect(9100, hosts[hi], function () {
          resolve(socket.remoteAddress);
          socket.destroy();
        });
        socket.on('error', function (err) {
          console.log('[discover] Tier 1: ' + hosts[hi] + ' error: ' + err.message);
          resolve(null);
          socket.destroy();
        });
        socket.on('timeout', function () {
          console.log('[discover] Tier 1: ' + hosts[hi] + ' timed out after 2000ms');
          resolve(null);
          socket.destroy();
        });
      });
      if (ip) {
        console.log('[discover] Tier 1: Connected to ' + hosts[hi] + ' (resolved IP: ' + ip + ')');
        return { host: ip, port: 9100 };
      }
      console.log('[discover] Tier 1: ' + hosts[hi] + ' failed (no connection)');
    } catch (e) {
      console.log('[discover] Tier 1: ' + hosts[hi] + ' exception: ' + e.message);
    }
  }

  // Tier 2: Subnet scan
  var ifaces = os.networkInterfaces();
  var keys = Object.keys(ifaces);
  console.log('[discover] Tier 2: Scanning subnets. Network interfaces: ' + keys.join(', '));
  var scannedSubnets = 0;
  for (var i = 0; i < keys.length; i++) {
    var addrs = ifaces[keys[i]];
    for (var j = 0; j < addrs.length; j++) {
      var addr = addrs[j];
      if (addr.family === 'IPv4' && !addr.internal) {
        // Skip Tailscale and other VPN interfaces (100.64.0.0/10)
        if (keys[i].toLowerCase().indexOf('tailscale') >= 0 || addr.address.startsWith('100.')) {
          console.log('[discover] Tier 2: Skipping VPN/Tailscale interface ' + keys[i] + ' (' + addr.address + ')');
          continue;
        }
        var parts = addr.address.split('.');
        if (parts.length === 4) {
          var subnet = parts.slice(0, 3).join('.');
          console.log('[discover] Tier 2: Scanning interface ' + keys[i] + ' (' + addr.address + ') -> subnet ' + subnet + '.0/24');
          scannedSubnets++;
          var found = await scanSubnetForPrinter(subnet);
          if (found) {
            console.log('[discover] Tier 2: Found printer at ' + found);
            return { host: found, port: 9100 };
          }
        }
      } else {
        console.log('[discover] Tier 2: Skipping interface ' + keys[i] + ' (' + addr.address + ', family=' + addr.family + ', internal=' + addr.internal + ')');
      }
    }
  }

  // Tier 3: USB fallback
  console.log('[discover] Tier 3: Trying USB detection...');
  var usb = require('./driver-usb');
  try {
    var devicePath = await usb.findPrinter();
    console.log('[discover] Found via USB: ' + devicePath);
    return { method: 'usb', devicePath: devicePath };
  } catch (e) {
    console.log('[discover] USB scan failed: ' + e.message);
  }

  console.log('[discover] Discovery failed. Scanned ' + scannedSubnets + ' subnet(s) and USB, no VC-500W found.');
  return { error: 'not_found', message: 'Brother VC-500W not found on network or USB. Please check if the printer is powered on, connected to WiFi (same network as this machine), or plugged in via USB.' };
}

// ---------- Main Driver ----------

class VC500WDriver {
  /**
   * @param {object} [options]
   * @param {string} [options.host] - Printer IP or hostname
   * @param {number} [options.port=9100] - TCP port
   * @param {string} [options.method] - 'tcp' or 'usb' (default: 'tcp')
   * @param {string} [options.devicePath] - USB device path (required if method='usb')
   */
  constructor(options) {
    options = options || {};
    this.host = options.host || '';
    this.port = options.port || 9100;
    this.method = options.method || 'tcp';
    this.devicePath = options.devicePath || '';
    this.connection = null;
    this.connected = false;
  }

  /**
   * Connect to the printer.
   * If no host was provided, attempts auto-discovery.
   */
  async connect() {
    if (this.method === 'usb') {
      this.connection = new USBConnection(this.devicePath);
      await this.connection.connect();
      this.connected = true;
      return true;
    }

    if (!this.host) {
      // Check config for manual host before running discovery
      try {
        var configRaw = fs.readFileSync(require('path').join(os.homedir(), '.sunburn-app', 'config.json'), 'utf8');
        var config = JSON.parse(configRaw);
        if (config.printerBrotherHost) {
          console.log('[VC500W] Using manual config host: ' + config.printerBrotherHost);
          this.host = config.printerBrotherHost;
        }
      } catch (e) {
        // Config file missing or invalid — continue to discovery
      }
    }

    if (!this.host) {
      var found = await discoverPrinter();
      if (found && found.error) {
        // Discovery returned a structured error — try printerIp as last resort
        try {
          var configRaw2 = fs.readFileSync(require('path').join(os.homedir(), '.sunburn-app', 'config.json'), 'utf8');
          var config2 = JSON.parse(configRaw2);
          if (config2.printerIp) {
            console.log('[VC500W] Discovery failed, falling back to config printerIp: ' + config2.printerIp);
            this.host = config2.printerIp;
          }
        } catch (e) {
          // Config file missing or invalid — ignore
        }
        if (!this.host) throw new Error(found.message);
      } else if (found && found.method === 'usb') {
        // Discovery found printer via USB
        this.method = 'usb';
        this.devicePath = found.devicePath;
        this.connection = new USBConnection(this.devicePath);
        await this.connection.connect();
        this.connected = true;
        return true;
      } else if (found) {
        this.host = found.host;
        this.port = found.port;
      } else {
        throw new Error('No Brother VC-500W found on the network');
      }
    }

    this.connection = new TCPConnection(this.host, this.port);
    await this.connection.connect();
    this.connected = true;
    return true;
  }

  /**
   * Reconnect (e.g., after closing socket for cut cycle).
   */
  async reconnect() {
    if (this.connection) this.connection.close();
    this.connection = new TCPConnection(this.host, this.port);
    await this.connection.connect();
    this.connected = true;
  }

  /**
   * Get printer configuration (model, serial, tape info).
   * @returns {Promise<object>}
   */
  async getConfig() {
    if (this.method === 'usb') {
      throw new Error('Not supported over USB');
    }
    var response = await this.connection.sendCommand(
      '<read>\n<path>/config.xml</path>\n</read>'
    );
    return {
      model: parseXmlValue(response, 'model_name') || 'VC-500W',
      serial: parseXmlValue(response, 'serial_number'),
      firmware: parseXmlValue(response, 'fw_version_number'),
      macAddress: parseXmlValue(response, 'wlan0_mac_address'),
      tapeType: parseXmlValue(response, 'cassette_type'),
      tapeWidthMm: parseFloat(parseXmlValue(response, 'width_inches') || '0') * 25.4,
      tapeLengthMm: parseFloat(parseXmlValue(response, 'media_length_initial') || '0') * 25.4,
    };
  }

  /**
   * Get printer status (print state, tape remaining).
   * @returns {Promise<object>}
   */
  async getStatus() {
    if (this.method === 'usb') {
      throw new Error('Not supported over USB');
    }
    var response = await this.connection.sendCommand(
      '<read>\n<path>/status.xml</path>\n</read>'
    );
    return {
      state: parseXmlValue(response, 'print_state') || 'unknown',
      stage: parseXmlValue(response, 'print_job_stage'),
      error: parseXmlValue(response, 'print_job_error'),
      tapeRemainingMm: parseFloat(parseXmlValue(response, 'remain') || '0') * 25.4,
    };
  }

  /**
   * Wait for the printer to become IDLE.
   * Polls status every 2.5 seconds, up to the given timeout.
   *
   * @param {number} [timeout=30000] Max wait time in ms
   * @returns {Promise<string>} Final print_state
   */
  async waitForIdle(timeout) {
    timeout = timeout || 30000;
    var start = Date.now();
    while (Date.now() - start < timeout) {
      try {
        var status = await this.getStatus();
        if (!status.state || status.state === 'IDLE' ||
            status.state === 'idle' || status.state === 'ready') {
          return status.state;
        }
      } catch (e) {
        // Status poll failed — connection might be dead
      }
      await new Promise(function (r) { setTimeout(r, 2500); });
    }
    throw new Error('Printer did not become idle within ' + (timeout / 1000) + ' seconds');
  }

  /**
   * Print a JPEG image.
   *
   * After printing, the TCP connection is closed to trigger the cut cycle,
   * then the driver reconnects and polls until the printer is IDLE.
   *
   * @param {Buffer} jpegBuffer - JPEG image data
   * @param {object} [options]
   * @param {string} [options.quality='vivid'] - 'vivid' or 'normal'
   * @param {string} [options.cutMode='full'] - 'full', 'half', or 'none'
   * @param {number} [options.imgWidth=0] - Image width (0 = autofit)
   * @param {number} [options.imgHeight=0] - Image height (0 = autofit)
   * @returns {Promise<{success: boolean, code: number}>}
   */
  async printJpeg(jpegBuffer, options) {
    options = options || {};
    var printXml = buildPrintXml(jpegBuffer.length, options);
    var response = await this.connection.sendPrintJob(printXml, jpegBuffer, 120000);
    var code = parseStatusCode(response);

    if (code === 0 && this.method === 'usb') {
      // USB is fire-and-forget — no close/reconnect/status polling.
      // Wait ~5s to estimate print+cut time before allowing next job.
      await new Promise(function (r) { setTimeout(r, 5000); });
    } else if (code === 0) {
      // Close TCP to trigger cut cycle
      this.connection.close();
      this.connection = null;
      this.connected = false;

      // Wait for cut cycle to start, then reconnect
      await new Promise(function (r) { setTimeout(r, 3000); });
      await this.reconnect();

      // Poll until printer is IDLE (label printed and ready for next)
      try {
        await this.waitForIdle(30000);
      } catch (e) {
        // Timeout waiting for idle — printer may need manual attention
      }
    }

    return {
      success: code === 0,
      code: code,
      response: response
    };
  }

  /**
   * Print multiple labels sequentially.
   * Each label is printed, cut, and confirmed IDLE before the next.
   *
   * @param {Buffer[]} jpegBuffers - Array of JPEG buffers
   * @param {object} [options] - Same as printJpeg options
   * @param {function} [onProgress] - Called with { printed, total, current }
   * @returns {Promise<{success: boolean, printed: number, failed: number, total: number}>}
   */
  async printBatch(jpegBuffers, options, onProgress) {
    var printed = 0;
    var failed = 0;
    var total = jpegBuffers.length;

    for (var i = 0; i < total; i++) {
      if (onProgress) onProgress({ printed: printed, total: total, current: i + 1 });

      try {
        var result = await this.printJpeg(jpegBuffers[i], options);
        if (result.success) {
          printed++;
        } else {
          failed++;
        }
      } catch (e) {
        failed++;
        // Try to reconnect for the next label
        try { await this.reconnect(); } catch (re) {}
      }
    }

    return { success: failed === 0, printed: printed, failed: failed, total: total };
  }

  /**
   * Close the connection.
   */
  close() {
    if (this.connection) {
      this.connection.close();
      this.connection = null;
    }
    this.connected = false;
  }
}

// ---------- Exports ----------

module.exports = {
  VC500WDriver: VC500WDriver,
  TCPConnection: TCPConnection,
  USBConnection: USBConnection,
  discoverPrinter: discoverPrinter,
  scanSubnetForPrinter: scanSubnetForPrinter,
  buildPrintXml: buildPrintXml,
  parseXmlValue: parseXmlValue,
  parseStatusCode: parseStatusCode,
  PRINT_MODES: PRINT_MODES
};
