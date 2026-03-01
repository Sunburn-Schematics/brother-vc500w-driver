/**
 * Example: Print multiple JPEG labels sequentially
 *
 * Usage: node print-batch.js <jpeg1> <jpeg2> ... [--ip=192.168.1.243]
 *
 * Each label is printed, the printer cuts it and returns to IDLE,
 * then the next label is sent. ~15 seconds per label.
 *
 * Tip: Tilt the printer upright so labels fall out by gravity —
 * the printer will complain if a label blocks the output slot.
 */

var fs = require('fs');
var { VC500WDriver } = require('../driver');

async function main() {
  var args = process.argv.slice(2);
  var printerIp = null;
  var jpegPaths = [];

  for (var i = 0; i < args.length; i++) {
    if (args[i].startsWith('--ip=')) {
      printerIp = args[i].substring(5);
    } else {
      jpegPaths.push(args[i]);
    }
  }

  if (jpegPaths.length === 0) {
    console.log('Usage: node print-batch.js <jpeg1> <jpeg2> ... [--ip=192.168.1.243]');
    process.exit(1);
  }

  // Validate all files exist before starting
  for (var j = 0; j < jpegPaths.length; j++) {
    if (!fs.existsSync(jpegPaths[j])) {
      console.error('File not found:', jpegPaths[j]);
      process.exit(1);
    }
  }

  var options = {};
  if (printerIp) options.host = printerIp;

  var printer = new VC500WDriver(options);
  console.log('Connecting...');
  await printer.connect();
  console.log('Connected to', printer.host);

  // Load all JPEGs
  var buffers = jpegPaths.map(function (p) { return fs.readFileSync(p); });

  // Print sequentially
  var result = await printer.printBatch(buffers, {
    quality: 'vivid',
    cutMode: 'full'
  }, function (progress) {
    console.log('Printing label', progress.current, 'of', progress.total,
      '(' + progress.printed + ' completed)');
  });

  console.log('\nBatch complete:', result.printed, 'printed,', result.failed, 'failed');
  printer.close();
}

main().catch(function (err) {
  console.error('Error:', err.message);
  process.exit(1);
});
