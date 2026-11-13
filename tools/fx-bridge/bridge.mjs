/**
 * FX control bridge: WebSocket (the workbench page) <-> OSC over UDP (TouchDesigner).
 *
 * A browser cannot send UDP, so the workbench talks JSON over a WebSocket to this
 * process and this process speaks OSC to TouchDesigner:
 *
 *   workbench page --JSON/WebSocket--> bridge --OSC/UDP--> TouchDesigner  (state feedback)
 *   workbench page <--JSON/WebSocket-- bridge <--OSC/UDP-- TouchDesigner  (control)
 *
 * Wire format on the WebSocket, one JSON object per text frame:
 *   {"address": "/fx/node/fx-1/intensity", "args": [0.5]}        an OSC message either way
 *   {"bridge": "fx-td", "version": 1, ...}                        sent once to each new client
 * Arguments are numbers, strings, booleans or null. OSC blobs and bundles' time tags are
 * not forwarded. Everything is bounded: frame size, message size, arguments, clients.
 */
import {createSocket} from 'node:dgram';
import {decodePacket, encodeMessage, MAX_ARGS} from './osc-codec.mjs';
import {listen} from './ws-server.mjs';

export const BRIDGE_PROTOCOL = 1;
const MAX_ADDRESS = 512;
const MAX_STRING = 1024;

/** Validate and normalise one inbound WebSocket message into {address, args}; throws on anything else. */
export function parseWireMessage(text) {
  let value;
  try {value = JSON.parse(text);} catch {throw Error('Not JSON.');}
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('Expected an object.');
  const {address, args = []} = value;
  if (typeof address !== 'string' || !address.startsWith('/') || address.length > MAX_ADDRESS || /[\s\0]/.test(address)) throw Error('Invalid OSC address.');
  if (!Array.isArray(args) || args.length > MAX_ARGS) throw Error('Invalid arguments.');
  for (const arg of args) {
    const type = typeof arg;
    if (arg === null || type === 'boolean') continue;
    if (type === 'number' && Number.isFinite(arg)) continue;
    if (type === 'string' && arg.length <= MAX_STRING && !arg.includes('\0')) continue;
    throw Error('Arguments must be finite numbers, short strings, booleans or null.');
  }
  return {address, args};
}

/** The JSON form of a decoded OSC message, or null when it carries something JSON cannot (blobs). */
export function toWireMessage(message) {
  const args = [];
  for (const arg of message.args) {
    if (arg instanceof Uint8Array) return null;
    args.push(typeof arg === 'number' && !Number.isFinite(arg) ? null : arg);
  }
  return JSON.stringify({address: message.address, args});
}

/**
 * options: {host, wsPort, allowedOrigins, oscHost, oscPort (receive), tdHost, tdPort (send), log}
 * Returns {wsPort, oscPort, stats, close()}.
 */
export async function startBridge({
  host = '127.0.0.1', wsPort = 9120, allowedOrigins = [],
  oscHost = '127.0.0.1', oscPort = 7001, tdHost = '127.0.0.1', tdPort = 7000, log = () => {},
} = {}) {
  const stats = {toTouchDesigner: 0, fromTouchDesigner: 0, dropped: 0, clients: 0};
  const udp = createSocket({type: tdHost.includes(':') ? 'udp6' : 'udp4'});
  const receiver = createSocket({type: oscHost.includes(':') ? 'udp6' : 'udp4'});
  let ws = null;

  receiver.on('message', packet => {
    let messages;
    try {messages = decodePacket(new Uint8Array(packet.buffer, packet.byteOffset, packet.length));} catch (error) {stats.dropped++; log(`dropped OSC packet: ${error.message}`); return;}
    for (const message of messages) {
      const text = toWireMessage(message);
      if (text === null) {stats.dropped++; continue;}
      stats.fromTouchDesigner++;
      ws?.broadcast(text);
    }
  });
  receiver.on('error', error => log(`OSC receive error: ${error.message}`));
  udp.on('error', error => log(`OSC send error: ${error.message}`));
  await new Promise((resolve, reject) => {receiver.once('error', reject); receiver.bind(oscPort, oscHost, resolve);});
  // `connect` fixes the destination, so a stray reply can never be sent elsewhere.
  await new Promise((resolve, reject) => {udp.once('error', reject); udp.connect(tdPort, tdHost, resolve);});

  try {
    ws = await listen({
      host, port: wsPort, allowedOrigins,
      onOpen(client) {
        stats.clients = ws.clients.size;
        client.send(JSON.stringify({bridge: 'fx-td', version: BRIDGE_PROTOCOL, touchdesigner: {host: tdHost, port: tdPort}, listening: {host: oscHost, port: receiver.address().port}}));
        log(`workbench connected${client.origin ? ` from ${client.origin}` : ''}`);
      },
      onClose() {stats.clients = ws.clients.size; log('workbench disconnected');},
      onMessage(text, client) {
        let message, packet;
        try {message = parseWireMessage(text); packet = encodeMessage(message.address, message.args);} catch (error) {stats.dropped++; log(`dropped WebSocket message: ${error.message}`); return;}
        udp.send(packet, error => {if (error) {stats.dropped++; log(`OSC send failed: ${error.message}`);}});
        stats.toTouchDesigner++;
      },
    });
  } catch (error) {receiver.close(); udp.close(); throw error;}

  return {
    wsPort: ws.port, oscPort: receiver.address().port, stats,
    async close() {
      await ws.close();
      await Promise.all([new Promise(resolve => receiver.close(resolve)), new Promise(resolve => udp.close(resolve))]);
    },
  };
}
