/**
 * Example: Print a single JPEG label via WiFi
 *
 * Usage: node print-label.js <jpeg-path> [printer-ip]
 */

var fs = require('fs');
var { VC500WDriver } = require('../driver');

async function main() {
  var jpegPath = process.argv[2];
  var printerIp = process.argv[3];

  if (!jpegPath) {
    console.log('Usage: node print-label.js <jpeg-path> [printer-ip]');
    console.log('  If no IP is given, the printer will be auto-discovered on the network.');
    process.exit(1);
  }

  if (!fs.existsSync(jpegPath)) {
    console.error('File not found:', jpegPath);
    process.exit(1);
  }

  var options = {};
  if (printerIp) options.host = printerIp;

  var printer = new VC500WDriver(options);

  console.log(printerIp
    ? 'Connecting to printer at ' + printerIp + '...'
    : 'Discovering printer on network...');
  await printer.connect();
  console.log('Connected to', printer.host);

  // Get printer info
  var config = await printer.getConfig();
  console.log('Model:', config.model, '| Serial:', config.serial);
  console.log('Tape:', config.tapeType, '(' + config.tapeWidthMm + 'mm)');

  // Print the label
  var jpeg = fs.readFileSync(jpegPath);
  console.log('Printing', jpegPath, '(' + Math.round(jpeg.length / 1024) + ' KB)...');

  var result = await printer.printJpeg(jpeg, {
    quality: 'vivid',
    cutMode: 'full'
  });

  if (result.success) {
    console.log('Print successful!');
  } else {
    console.error('Print failed with code:', result.code);
  }

  printer.close();
}

main().catch(function (err) {
  console.error('Error:', err.message);
  process.exit(1);
});
