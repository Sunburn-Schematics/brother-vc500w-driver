/**
 * Example: Discover a Brother VC-500W on the local network
 *
 * Scans all local /24 subnets for devices listening on port 9100,
 * then verifies each by querying the printer's XML config endpoint.
 *
 * Usage: node discover-printer.js
 */

var { discoverPrinter, VC500WDriver } = require('../driver');
var os = require('os');

async function main() {
  // Show local network interfaces
  var ifaces = os.networkInterfaces();
  var keys = Object.keys(ifaces);
  console.log('Local network interfaces:');
  for (var i = 0; i < keys.length; i++) {
    var addrs = ifaces[keys[i]];
    for (var j = 0; j < addrs.length; j++) {
      if (addrs[j].family === 'IPv4' && !addrs[j].internal) {
        console.log('  ' + keys[i] + ': ' + addrs[j].address);
      }
    }
  }

  console.log('\nScanning for Brother VC-500W...');
  var start = Date.now();
  var result = await discoverPrinter();
  var elapsed = ((Date.now() - start) / 1000).toFixed(1);

  if (result) {
    console.log('\nFound printer at', result.host + ':' + result.port, '(' + elapsed + 's)');

    // Connect and get details
    var printer = new VC500WDriver({ host: result.host, port: result.port });
    await printer.connect();
    var config = await printer.getConfig();
    console.log('  Model:  ', config.model);
    console.log('  Serial: ', config.serial);
    console.log('  Tape:   ', config.tapeType, '(' + config.tapeWidthMm + 'mm)');
    printer.close();
  } else {
    console.log('\nNo printer found (' + elapsed + 's)');
    console.log('Make sure the printer is powered on and on the same network.');
  }
}

main().catch(function (err) {
  console.error('Error:', err.message);
  process.exit(1);
});
