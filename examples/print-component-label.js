/**
 * Example: Print a component label on the Brother VC-500W
 *
 * Renders a label using the exact same layout as the Sunburn Schematics
 * web app — component symbol, QR code, footprint badge, specs — and
 * prints it on the Brother VC-500W over WiFi.
 *
 * Usage:
 *   node print-component-label.js                    # prints all samples
 *   node print-component-label.js --preview          # saves images without printing
 *   node print-component-label.js --host 192.168.1.243
 *
 * Requires: npm install (puppeteer, qrcode)
 */

var fs = require('fs');
var path = require('path');
var { renderComponentLabel, closeBrowser, SAMPLE_COMPONENTS } = require('../label-renderer');
var { VC500WDriver } = require('../driver');

var HOST = '192.168.1.243';
var PORT = 9100;
var PREVIEW_ONLY = false;

// Parse CLI args
var args = process.argv.slice(2);
for (var i = 0; i < args.length; i++) {
  if (args[i] === '--preview') PREVIEW_ONLY = true;
  if (args[i] === '--host' && args[i + 1]) { HOST = args[++i]; }
  if (args[i] === '--port' && args[i + 1]) { PORT = parseInt(args[++i]); }
}

async function main() {
  console.log('Brother VC-500W Component Label Printing');
  console.log('=========================================\n');

  // Pick a few representative components to print
  var components = SAMPLE_COMPONENTS.slice(0, 3);

  for (var idx = 0; idx < components.length; idx++) {
    var item = components[idx];
    console.log('[' + (idx + 1) + '/' + components.length + '] Rendering: ' + item.mpn + ' (' + item.component_type + ')');

    var jpeg = await renderComponentLabel(item, {
      printer: 'brother',
      widthInches: 1.0,
      heightInches: 1.5,
    });

    console.log('  Image: ' + jpeg.length + ' bytes JPEG');

    if (PREVIEW_ONLY) {
      var outFile = path.join(__dirname, 'preview-' + item.component_type.toLowerCase() + '-' + (idx + 1) + '.jpg');
      fs.writeFileSync(outFile, jpeg);
      console.log('  Saved: ' + outFile);
    } else {
      console.log('  Connecting to ' + HOST + ':' + PORT + '...');
      var printer = new VC500WDriver({ host: HOST, port: PORT });
      await printer.connect();

      console.log('  Printing (vivid, full cut)...');
      var result = await printer.printJpeg(jpeg, { quality: 'vivid', cutMode: 'full' });
      printer.close();

      if (result.success) {
        console.log('  Printed successfully!');
        // Wait for printer to cut and return to IDLE before next label
        if (idx < components.length - 1) {
          console.log('  Waiting for printer to become idle...');
          await waitForIdle(HOST, PORT);
        }
      } else {
        console.log('  Print failed: code ' + result.code);
      }
    }
    console.log('');
  }

  await closeBrowser();
  console.log('Done.');
}

async function waitForIdle(host, port) {
  await new Promise(function (r) { setTimeout(r, 3000); });
  var maxWait = 30000;
  var start = Date.now();
  while (Date.now() - start < maxWait) {
    try {
      var printer = new VC500WDriver({ host: host, port: port });
      await printer.connect();
      var status = await printer.getStatus();
      printer.close();
      if (status.state === 'IDLE' || status.state === 'ready') {
        return;
      }
      console.log('  State: ' + status.state + '...');
    } catch (e) {
      // Printer may be busy cutting
    }
    await new Promise(function (r) { setTimeout(r, 2000); });
  }
}

main().catch(function (err) {
  console.error('Error:', err.message);
  closeBrowser().then(function () { process.exit(1); });
});
