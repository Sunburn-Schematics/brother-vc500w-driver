/**
 * Example: Query printer status and configuration
 *
 * Usage: node get-status.js [printer-ip]
 */

var { VC500WDriver } = require('../driver');

async function main() {
  var printerIp = process.argv[2];
  var options = {};
  if (printerIp) options.host = printerIp;

  var printer = new VC500WDriver(options);
  console.log('Connecting...');
  await printer.connect();

  // Configuration
  var config = await printer.getConfig();
  console.log('\n--- Configuration ---');
  console.log('Model:       ', config.model);
  console.log('Serial:      ', config.serial);
  console.log('Firmware:    ', config.firmware);
  console.log('MAC Address: ', config.macAddress);
  console.log('Tape Type:   ', config.tapeType);
  console.log('Tape Width:  ', config.tapeWidthMm, 'mm');
  console.log('Tape Length: ', config.tapeLengthMm, 'mm');

  // Status
  var status = await printer.getStatus();
  console.log('\n--- Status ---');
  console.log('State:          ', status.state);
  console.log('Job Stage:      ', status.stage);
  console.log('Job Error:      ', status.error);
  console.log('Tape Remaining: ', Math.round(status.tapeRemainingMm), 'mm');

  if (status.tapeRemainingMm > 0 && config.tapeLengthMm > 0) {
    var percent = Math.round((status.tapeRemainingMm / config.tapeLengthMm) * 100);
    console.log('Tape Used:      ', (100 - percent) + '%');
  }

  // JSON output for scripting
  if (process.argv.includes('--json')) {
    console.log('\n' + JSON.stringify({ config: config, status: status }, null, 2));
  }

  printer.close();
}

main().catch(function (err) {
  console.error('Error:', err.message);
  process.exit(1);
});
