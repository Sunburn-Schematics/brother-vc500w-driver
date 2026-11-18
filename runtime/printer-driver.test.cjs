'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const net = require('node:net');
const { performance } = require('node:perf_hooks');

const SOURCE = process.env.PRINTER_SOURCE_DIR || __dirname;
const SCALE = 0.02;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function loadDriver() {
  const initial = Date.now();
  const started = performance.now();
  let offset = 0;
  const now = () => initial + offset + (performance.now() - started) / SCALE;
  class Clock extends Date {
    constructor(...args) { super(...(args.length ? args : [now()])); }
    static now() { return now(); }
  }
  const module = { exports: {} };
  const context = {
    module, exports: module.exports, require, __dirname: SOURCE,
    Buffer, process, Date: Clock,
    console: { log() {}, warn() {}, error() {} },
    setTimeout: (callback, ms, ...args) => setTimeout(callback, Math.max(1, ms * SCALE), ...args),
    clearTimeout,
  };
  const text = fs.readFileSync(path.join(SOURCE, 'printer-driver.js'), 'utf8');
  vm.runInNewContext(text + '\nmodule.exports.setTestDiscovery = fn => { discoverPrinter = fn; };\n',
    context, { filename: 'printer-driver.js' });
  module.exports.advanceTestClock = ms => { offset += ms; };
  return module.exports;
}

function status(state = 'IDLE', stage = 'SUCCESS', error = 'NONE', inches = 100) {
  return '<status><print_state>' + state + '</print_state><print_job_stage>' + stage +
    '</print_job_stage><print_job_error>' + error + '</print_job_error><remain>' +
    inches + '</remain></status>';
}

async function printer(t, response) {
  const sockets = new Set();
  const commands = [];
  let connections = 0;
  const server = net.createServer(socket => {
    connections++;
    sockets.add(socket);
    socket.on('error', () => {});
    socket.on('close', () => sockets.delete(socket));
    let buffer = '';
    socket.on('data', data => {
      buffer += data.toString();
      let end;
      while ((end = buffer.indexOf('</read>')) >= 0) {
        const command = buffer.slice(0, end + 7);
        buffer = buffer.slice(end + 7);
        commands.push(command);
        response(socket, commands.length, command);
      }
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    for (const socket of sockets) socket.destroy();
    await new Promise(resolve => server.close(resolve));
  });
  return { host: '127.0.0.1', port: server.address().port, sockets, commands,
    get connections() { return connections; } };
}

function assertReadOnly(fixture) {
  assert.ok(fixture.commands.length > 0);
  for (const command of fixture.commands) {
    assert.match(command, /<read>\s*<path>\/status\.xml<\/path>\s*<\/read>/);
    assert.doesNotMatch(command, /<print|<cut|JPEG|imageData/);
  }
}

test('a lost status response reconnects read-only and still requires activity plus two healthy idles', async t => {
  const fixture = await printer(t, (socket, number) => {
    if (number === 1) socket.write(status('BUSY', 'PRINTING'));
    else if (number === 2) socket.write('<status><path>/status.xml</path><code>0</code></status>');
    else socket.write(status());
  });
  const { VC500WDriver } = loadDriver();
  const driver = new VC500WDriver();
  t.after(() => driver.close());
  await driver.connectWifi(fixture.host, fixture.port);
  const result = await driver._verifyPrint(2540);
  assert.equal(result.completed, true);
  assert.equal(result.sawBusy, true);
  assert.equal(result.settled, true);
  assert.ok(fixture.connections >= 2);
  assert.equal(result.observations.at(-1).state, 'IDLE');
  assertReadOnly(fixture);
});


test('closing during completion cannot accept a late response or replace a newer socket', async t => {
  const { VC500WDriver } = loadDriver();
  let driver;
  const fixture = await printer(t, (socket, number) => {
    if (number === 1) socket.write(status('BUSY', 'PRINTING'));
    else {
      driver.close();
      socket.write(status());
    }
  });
  driver = new VC500WDriver();
  t.after(() => driver.close());
  await driver.connectWifi(fixture.host, fixture.port);
  const result = await driver._verifyPrint(2540);
  assert.equal(result.completed, false);
  assert.equal(driver.connected, false);
  await sleep(20);
  assert.equal(fixture.sockets.size, 0);
  assertReadOnly(fixture);
});


test('idle samples without print activity never prove completion', async t => {
  const fixture = await printer(t, socket => socket.write(status()));
  const { VC500WDriver } = loadDriver();
  const driver = new VC500WDriver();
  t.after(() => driver.close());
  await driver.connectWifi(fixture.host, fixture.port);
  const result = await driver._verifyPrint(2540);
  assert.equal(result.completed, false);
  assert.equal(result.verified, false);
  assert.ok(result.elapsedMs <= 61000);
  assertReadOnly(fixture);
});

test('printer errors remain uncertain and stop completion checks', async t => {
  const fixture = await printer(t, socket => socket.write(status('ERROR', 'PRINTING', 'COVER OPEN')));
  const { VC500WDriver } = loadDriver();
  const driver = new VC500WDriver();
  t.after(() => driver.close());
  await driver.connectWifi(fixture.host, fixture.port);
  const result = await driver._verifyPrint(2540);
  assert.equal(result.completed, false);
  assert.match(result.reason, /COVER OPEN/);
  assert.equal(fixture.commands.length, 1);
});

test('unresponsive status recovery cannot extend the original completion deadline', async t => {
  const fixture = await printer(t, () => {});
  const { VC500WDriver } = loadDriver();
  const driver = new VC500WDriver();
  t.after(() => driver.close());
  await driver.connectWifi(fixture.host, fixture.port);
  const result = await driver._verifyPrint(2540);
  assert.equal(result.completed, false);
  assert.ok(result.elapsedMs <= 61000, 'completion deadline stays bounded: ' + result.elapsedMs);
  assertReadOnly(fixture);
});

test('reopening one TCPConnection closes the old persistent socket', async t => {
  const fixture = await printer(t, () => {});
  const { TCPConnection } = loadDriver();
  const connection = new TCPConnection(fixture.host, fixture.port);
  t.after(() => connection.close());
  await connection.connect();
  await connection.connect();
  await sleep(20);
  assert.equal(fixture.sockets.size, 1);
  connection.close();
  await sleep(20);
  assert.equal(fixture.sockets.size, 0);
});

test('closing during discovery cancels its late connection instead of leaking a socket', async t => {
  const fixture = await printer(t, () => {});
  const { VC500WDriver, setTestDiscovery } = loadDriver();
  let release;
  setTestDiscovery(() => new Promise(resolve => { release = resolve; }));
  const driver = new VC500WDriver();
  t.after(() => driver.close());
  const pending = driver.connect();
  driver.close();
  release({ wifi: { host: fixture.host, port: fixture.port }, usb: null });
  await assert.rejects(pending, /cancel|closed|supersed/i);
  await sleep(20);
  assert.equal(fixture.sockets.size, 0);
  assert.equal(driver.connected, false);
});

test('manual connection cannot overtake startup discovery', async t => {
  const fixture = await printer(t, () => {});
  const { VC500WDriver, setTestDiscovery } = loadDriver();
  let release;
  setTestDiscovery(() => new Promise(resolve => { release = resolve; }));
  const driver = new VC500WDriver();
  t.after(() => driver.close());
  const pending = driver.connect();
  assert.equal(driver._connecting, true);
  await assert.rejects(driver.connectWifi(fixture.host, fixture.port), /connection.*progress/i);
  release({ wifi: { host: fixture.host, port: fixture.port }, usb: null });
  await pending;
  assert.equal(driver._connecting, false);
  await sleep(20);
  assert.equal(fixture.sockets.size, 1);
});

test('an old cancelled discovery cannot release a newer connection operation', async t => {
  const fixture = await printer(t, () => {});
  const { VC500WDriver, setTestDiscovery } = loadDriver();
  const releases = [];
  setTestDiscovery(() => new Promise(resolve => releases.push(resolve)));
  const driver = new VC500WDriver();
  t.after(() => driver.close());
  const old = driver.connect();
  driver.close();
  const current = driver.connect();
  releases[0]({ wifi: { host: fixture.host, port: fixture.port }, usb: null });
  await assert.rejects(old, /cancel|closed|supersed/i);
  assert.equal(driver._connecting, true);
  releases[1]({ wifi: { host: fixture.host, port: fixture.port }, usb: null });
  await current;
  assert.equal(driver._connecting, false);
  await sleep(20);
  assert.equal(fixture.sockets.size, 1);
});



test('a final idle response arriving beyond the completion deadline is not accepted', async t => {
  const { VC500WDriver, advanceTestClock } = loadDriver();
  const fixture = await printer(t, (socket, number) => {
    if (number === 1) socket.write(status('BUSY', 'PRINTING'));
    else {
      if (number === 3) advanceTestClock(61000);
      socket.write(status());
    }
  });
  const driver = new VC500WDriver();
  t.after(() => driver.close());
  await driver.connectWifi(fixture.host, fixture.port);
  const result = await driver._verifyPrint(2540);
  assert.equal(result.completed, false);
  assert.equal(result.settled, false);
  assertReadOnly(fixture);
});

test('concurrent status/config reads match payloads after fragmented or combined ACK frames', async t => {
  // Use actual local TCP sockets, with no physical-printer address or discovery.
  const { TCPConnection } = require('./printer-driver');
  const sockets = new Set();
  let requests = 0;
  let active = 0;
  let maximumActive = 0;
  const configuration = '<config><model_name>Wedge</model_name><serial_number>TEST</serial_number></config>';
  const state = status();
  const ack = '<status><code>0</code><datasize>0</datasize></status>\0';
  const server = net.createServer(socket => {
    sockets.add(socket);
    socket.setNoDelay(true);
    socket.on('error', () => {});
    socket.on('close', () => sockets.delete(socket));
    let buffer = '';
    socket.on('data', data => {
      buffer += data;
      let end;
      while ((end = buffer.indexOf('</read>')) >= 0) {
        const request = buffer.slice(0, end + 7);
        buffer = buffer.slice(end + 7);
        const response = request.includes('/config.xml') ? configuration : state;
        requests++;
        active++;
        maximumActive = Math.max(maximumActive, active);
        if (requests % 2) {
          socket.write(ack + response.slice(0, 13));
          setTimeout(() => { active--; socket.write(response.slice(13)); }, 1);
        } else {
          active--;
          socket.write(ack + response);
        }
      }
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const connection = new TCPConnection('127.0.0.1', server.address().port);
  t.after(async () => {
    connection.close();
    for (const socket of sockets) socket.destroy();
    await new Promise(resolve => server.close(resolve));
  });
  await connection.connect();
  connection.socket.setNoDelay(true);
  const responses = await Promise.all(Array.from({ length: 24 }, (_, index) =>
    connection.sendCommand('<read><path>/' + (index % 2 ? 'config' : 'status') + '.xml</path></read>', 1000)
  ));
  responses.forEach((response, index) => assert.equal(response, index % 2 ? configuration : state));
  assert.equal(requests, 24);
  assert.equal(maximumActive, 1);
});

test('an uncertain image submission is never repeated on a second channel', async () => {
  const { VC500WDriver } = loadDriver();
  const driver = new VC500WDriver();
  driver._buildChannelOrder = () => ['wifi', 'usb'];
  driver._demoteChannel = () => {};
  let wifiSubmissions = 0;
  let usbSubmissions = 0;
  driver._printViaWifi = async () => {
    wifiSubmissions++;
    driver._dataSentToPrinter = true;
    throw new Error('Submission response lost');
  };
  driver._printViaUsb = async () => { usbSubmissions++; return { success: true }; };
  const result = await driver.printJpeg(Buffer.from('synthetic image bytes'));
  assert.equal(result.completed, false);
  assert.equal(result.uncertain, true);
  assert.equal(wifiSubmissions, 1);
  assert.equal(usbSubmissions, 0);
  assert.equal(driver._busy, false);
});
