#!/usr/bin/env node
/**
 * Run the FX workbench <-> TouchDesigner bridge.
 *
 *   node tools/fx-td-bridge.mjs [--ws-port 9120] [--host 127.0.0.1]
 *        [--osc-port 7001] [--osc-host 127.0.0.1]     where TouchDesigner's OSC Out sends to
 *        [--td-port 7000] [--td-host 127.0.0.1]       where TouchDesigner's OSC In listens
 *        [--allow-origin https://example.site]        repeatable; loopback pages are always allowed
 *
 * See docs/FX-TOUCHDESIGNER.md.
 */
import {parseArgs} from 'node:util';
import {startBridge} from './fx-bridge/bridge.mjs';

const {values} = parseArgs({options: {
  host: {type: 'string', default: '127.0.0.1'}, 'ws-port': {type: 'string', default: '9120'},
  'osc-host': {type: 'string', default: '127.0.0.1'}, 'osc-port': {type: 'string', default: '7001'},
  'td-host': {type: 'string', default: '127.0.0.1'}, 'td-port': {type: 'string', default: '7000'},
  'allow-origin': {type: 'string', multiple: true, default: []}, help: {type: 'boolean', short: 'h'},
}});
if (values.help) {console.log(process.argv[1] + ' — see docs/FX-TOUCHDESIGNER.md'); process.exit(0);}
const port = name => {
  const value = Number(values[name]);
  if (!Number.isInteger(value) || value < 1 || value > 65535) {console.error(`--${name} must be a port number from 1 to 65535.`); process.exit(2);}
  return value;
};
const bridge = await startBridge({
  host: values.host, wsPort: port('ws-port'), allowedOrigins: values['allow-origin'],
  oscHost: values['osc-host'], oscPort: port('osc-port'), tdHost: values['td-host'], tdPort: port('td-port'),
  log: text => console.log(`[fx-bridge] ${text}`),
});
console.log(`[fx-bridge] WebSocket ws://${values.host}:${bridge.wsPort}  |  OSC in ${values['osc-host']}:${bridge.oscPort}  |  OSC out ${values['td-host']}:${values['td-port']}`);
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, async () => {await bridge.close(); process.exit(0);});
