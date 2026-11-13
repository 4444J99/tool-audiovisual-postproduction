/**
 * Link between the workbench and the TouchDesigner bridge (tools/fx-td-bridge.mjs).
 *
 * Owns one WebSocket, reconnects with a capped back-off while the user wants the link,
 * turns inbound OSC messages into rack actions, and pushes rack state changes back.
 * It has no DOM and takes the WebSocket constructor, timers and host as injected
 * dependencies, so it is tested without a browser or a network.
 *
 * host: {getView(), apply(action), gesture(key), select(delta), selectNode(id)}
 */
import {interpretOsc, OscFeedback} from './osc-control.mjs';

export const DEFAULT_BRIDGE_URL = 'ws://127.0.0.1:9120';
const MAX_FRAME_CHARS = 64 * 1024;
const MAX_ARGS = 64;
const RETRY_MS = [500, 1000, 2000, 4000, 8000];
const PUMP_MS = 40;

/** Accept ws: and wss: URLs only; returns the normalised string or throws. */
export function checkBridgeUrl(text) {
  let url;
  try {url = new URL(String(text).trim());} catch {throw Error('Enter a WebSocket address such as ws://127.0.0.1:9120.');}
  if (url.protocol !== 'ws:' && url.protocol !== 'wss:') throw Error('The bridge address must start with ws:// or wss://.');
  if (url.username || url.password || url.hash) throw Error('The bridge address must not carry credentials or a fragment.');
  return url.href;
}

/** Merge a change into the saved `osc` preferences without disturbing the other saved preferences. */
export function updateOscPrefs(storage, change) {
  if (!storage) return;
  try {storage.set({...storage.get(), osc: {...storage.get().osc, ...change}});} catch {/* storage is optional */}
}

function parseFrame(data) {
  if (typeof data !== 'string' || data.length > MAX_FRAME_CHARS) return null;
  let value;
  try {value = JSON.parse(data);} catch {return null;}
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  if (typeof value.bridge === 'string') return {hello: value};
  if (typeof value.address !== 'string' || !Array.isArray(value.args) || value.args.length > MAX_ARGS) return null;
  if (!value.args.every(arg => arg === null || ['number', 'string', 'boolean'].includes(typeof arg))) return null;
  return {message: {address: value.address, args: value.args}};
}

export class OscSession {
  constructor(host, {WebSocketImpl = globalThis.WebSocket, timers = globalThis, onStatus = () => {}, feedback = true} = {}) {
    this.host = host; this.WebSocketImpl = WebSocketImpl; this.timers = timers; this.onStatus = onStatus;
    this.feedbackOn = feedback; this.feedback = new OscFeedback();
    this.socket = null; this.url = null; this.wanted = false; this.attempt = 0;
    this.retryTimer = null; this.pumpTimer = null; this.state = 'off'; this.bridge = null; this.received = 0; this.sent = 0;
  }

  #set(state, detail = '') {this.state = state; this.onStatus({state, detail, received: this.received, sent: this.sent, bridge: this.bridge});}

  /** Start (or restart) the link to a bridge URL. */
  connect(url) {
    if (typeof this.WebSocketImpl !== 'function') {this.#set('unsupported', 'This browser has no WebSocket support.'); return false;}
    this.url = checkBridgeUrl(url);
    this.wanted = true; this.attempt = 0;
    this.#teardown();
    this.#open();
    return true;
  }

  disconnect() {
    this.wanted = false;
    this.#teardown();
    this.#set('off');
  }

  setFeedback(on) {
    this.feedbackOn = Boolean(on);
    if (this.feedbackOn) this.feedback.reset();
  }

  #open() {
    this.#set('connecting', this.url);
    let socket;
    try {socket = new this.WebSocketImpl(this.url);} catch (error) {this.#fail(error.message || 'Could not open the connection.'); return;}
    this.socket = socket;
    socket.onopen = () => {
      if (this.socket !== socket) return;
      this.attempt = 0; this.feedback.reset();
      this.#set('open', this.url);
      this.pump();
      this.pumpTimer = this.timers.setInterval(() => this.pump(), PUMP_MS);
    };
    socket.onmessage = event => {if (this.socket === socket) this.handle(event.data);};
    socket.onerror = () => {};  // the close event that follows carries the outcome
    socket.onclose = () => {
      if (this.socket !== socket) return;
      this.socket = null; this.#stopPump();
      this.#fail('The bridge closed the connection.');
    };
  }

  #fail(reason) {
    if (!this.wanted) return;
    const delay = RETRY_MS[Math.min(this.attempt++, RETRY_MS.length - 1)];
    this.#set('retrying', `${reason} Retrying in ${delay / 1000} s.`);
    this.retryTimer = this.timers.setTimeout(() => {this.retryTimer = null; if (this.wanted) this.#open();}, delay);
  }

  #stopPump() {if (this.pumpTimer !== null) {this.timers.clearInterval(this.pumpTimer); this.pumpTimer = null;}}

  #teardown() {
    this.#stopPump();
    if (this.retryTimer !== null) {this.timers.clearTimeout(this.retryTimer); this.retryTimer = null;}
    const socket = this.socket; this.socket = null;
    if (socket) {socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null; try {socket.close();} catch {/* already closed */}}
  }

  send(address, args) {
    if (!this.socket || this.socket.readyState !== 1) return false;
    this.socket.send(JSON.stringify({address, args}));
    this.sent++;
    return true;
  }

  /** Push whatever changed in the rack since the last pump. */
  pump({full = false} = {}) {
    if (!this.feedbackOn || !this.socket || this.socket.readyState !== 1) return 0;
    const messages = this.feedback.collect(this.host.getView(), {full});
    for (const message of messages) this.send(message.address, message.args);
    return messages.length;
  }

  /** One WebSocket text frame from the bridge. */
  handle(data) {
    const parsed = parseFrame(data);
    if (!parsed) return;
    if (parsed.hello) {this.bridge = parsed.hello; this.#set(this.state, this.url); return;}
    this.received++;
    const view = this.host.getView();
    const result = interpretOsc(parsed.message, view);
    if (result.sync) {this.pump({full: true}); return;}
    if (result.select) {
      if (result.select.nodeId) this.host.selectNode(result.select.nodeId); else this.host.select(result.select.delta);
    }
    for (const action of result.actions) {
      this.host.gesture(action.global ? `global:${action.global}` : `osc:${action.nodeId}:${action.path}`);
      this.host.apply(action);
    }
    if (result.actions.length || result.select) this.#set(this.state, this.url);
  }
}
