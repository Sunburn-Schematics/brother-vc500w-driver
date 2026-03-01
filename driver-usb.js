/**
 * Brother VC-500W USB Driver (Windows)
 *
 * Uses a C# helper (usb-printer-io.cs) invoked via PowerShell to communicate
 * with the printer over the Windows usbprint.sys driver.
 *
 * IMPORTANT: USB communication with the VC-500W is effectively WRITE-ONLY.
 * The printer does not reliably send read responses over USB. Status queries
 * and print confirmation only work over WiFi (TCP port 9100).
 *
 * Usage:
 *   const { findPrinter, printLabel } = require('./driver-usb');
 *
 *   const devicePath = await findPrinter();
 *   const result = await printLabel(devicePath, 'label.jpg', {
 *     quality: 'vivid', cutMode: 'full'
 *   });
 */

'use strict';

var { execFile } = require('child_process');
var path = require('path');
var fs = require('fs');
var os = require('os');

var XML_PREFIX = '<?xml version="1.0" encoding="UTF-8"?>\n';

var PRINT_MODES = {
  vivid: { name: 'vivid', speed: 0, lpi: 317 },
  normal: { name: 'color', speed: 1, lpi: 264 }
};

// Load the C# USB I/O helper
var USB_CSHARP = fs.readFileSync(path.join(__dirname, 'usb-printer-io.cs'), 'utf8');

// ---------- PowerShell Execution ----------

function runPowerShell(script, timeout) {
  return new Promise(function (resolve, reject) {
    var tmpScript = path.join(os.tmpdir(), 'vc500w-ps-' + Date.now() + '.ps1');
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

// ---------- Build Print XML ----------

function buildPrintXml(jpegSize, options) {
  options = options || {};
  var quality = options.quality || 'vivid';
  var cutMode = options.cutMode || 'full';
  var m = PRINT_MODES[quality] || PRINT_MODES.vivid;

  return XML_PREFIX +
    '<print>\n' +
    '<mode>' + m.name + '</mode>\n' +
    '<speed>' + m.speed + '</speed>\n' +
    '<lpi>' + m.lpi + '</lpi>\n' +
    '<width>0</width>\n' +
    '<height>0</height>\n' +
    '<dataformat>jpeg</dataformat>\n' +
    '<autofit>1</autofit>\n' +
    '<datasize>' + jpegSize + '</datasize>\n' +
    '<cutmode>' + cutMode + '</cutmode>\n' +
    '</print>';
}

// ---------- Public API ----------

/**
 * Find a Brother USB printer using Windows SetupDi enumeration.
 * Looks for USB devices with vendor ID 04F9 (Brother).
 *
 * @returns {Promise<string>} Device path (e.g., \\?\usb#vid_04f9&pid_20b0#...)
 * @throws {Error} If no Brother printer is found
 */
async function findPrinter() {
  var script = "Add-Type @'\n" + USB_CSHARP + "\n'@\n" +
    'Write-Output ([UsbPrinterIO]::FindBrotherPrinter())';
  var result = await runPowerShell(script);

  if (result.startsWith('ERROR:')) {
    throw new Error('USB printer not found: ' + result);
  }
  return result;
}

/**
 * Check if a printer is still connected at the given device path.
 *
 * @param {string} devicePath - Device path from findPrinter()
 * @returns {Promise<{alive: boolean, devicePath: string|null}>}
 */
async function checkConnection(devicePath) {
  try {
    var current = await findPrinter();
    return { alive: true, devicePath: current };
  } catch (e) {
    return { alive: false, devicePath: null, error: e.message };
  }
}

/**
 * Print a single JPEG label via USB.
 *
 * Uses "fire-and-forget" mode because the VC-500W does not send
 * read responses over USB. The function returns after writing the
 * data — there is no confirmation that the print succeeded.
 *
 * @param {string} devicePath - Device path from findPrinter()
 * @param {string} jpegPath - Path to the JPEG file to print
 * @param {object} [options]
 * @param {string} [options.quality='vivid'] - 'vivid' or 'normal'
 * @param {string} [options.cutMode='full'] - 'full', 'half', or 'none'
 * @returns {Promise<{success: boolean, output: string}>}
 */
async function printLabel(devicePath, jpegPath, options) {
  var jpegSize = fs.statSync(jpegPath).size;
  var printXml = buildPrintXml(jpegSize, options);

  var tmpXml = path.join(os.tmpdir(), 'vc500w-xml-' + Date.now() + '.bin');
  fs.writeFileSync(tmpXml, printXml, 'utf8');

  var escapedDevice = devicePath.replace(/'/g, "''");
  var escapedXml = tmpXml.replace(/\\/g, '\\\\');
  var escapedJpeg = jpegPath.replace(/\\/g, '\\\\');

  var script = "Add-Type @'\n" + USB_CSHARP + "\n'@\n" +
    '$r = [UsbPrinterIO]::PrintJobFireAndForget("' + escapedDevice + '", "' +
    escapedXml + '", "' + escapedJpeg + '")\n' +
    'Write-Output $r';

  try {
    var result = await runPowerShell(script, 30000);
    try { fs.unlinkSync(tmpXml); } catch (e) {}

    var hasError = result.indexOf('ERROR:') >= 0;
    var success = !hasError && (result.indexOf('PRINT_OK') >= 0);

    return { success: success, output: result };
  } catch (e) {
    try { fs.unlinkSync(tmpXml); } catch (e2) {}
    throw e;
  }
}

/**
 * Print a label from a Buffer (writes to temp file, prints, cleans up).
 *
 * @param {string} devicePath - Device path from findPrinter()
 * @param {Buffer} jpegBuffer - JPEG image data
 * @param {object} [options] - Same as printLabel options
 * @returns {Promise<{success: boolean, output: string}>}
 */
async function printBuffer(devicePath, jpegBuffer, options) {
  var tmpJpeg = path.join(os.tmpdir(), 'vc500w-jpeg-' + Date.now() + '.jpg');
  fs.writeFileSync(tmpJpeg, jpegBuffer);
  try {
    var result = await printLabel(devicePath, tmpJpeg, options);
    try { fs.unlinkSync(tmpJpeg); } catch (e) {}
    return result;
  } catch (e) {
    try { fs.unlinkSync(tmpJpeg); } catch (e2) {}
    throw e;
  }
}

// ---------- Exports ----------

module.exports = {
  findPrinter: findPrinter,
  checkConnection: checkConnection,
  printLabel: printLabel,
  printBuffer: printBuffer,
  buildPrintXml: buildPrintXml
};
