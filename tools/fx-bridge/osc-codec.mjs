/**
 * Open Sound Control 1.0 codec: messages and bundles, no dependencies.
 *
 * Decoding is strict and bounded because the bytes arrive from the network: every
 * length is checked against the packet, strings must be NUL-terminated and
 * four-byte padded, and bundles nest at most MAX_BUNDLE_DEPTH deep. Anything that
 * does not parse throws an Error; the bridge drops such packets.
 *
 * Supported argument types: i (int32), f (float32), s (string), b (blob), h (int64),
 * d (float64), T, F, N, plus t (time tag) and I (infinitum) which are decoded and
 * never produced. Arguments decode to plain JavaScript values:
 * numbers, strings, booleans, null, and Uint8Array for blobs.
 */
export const MAX_BUNDLE_DEPTH = 4;
export const MAX_ARGS = 64;
const pad4 = length => (4 - (length % 4)) % 4;
const textEncoder = new TextEncoder();
const utf8 = new TextDecoder('utf-8', {fatal: true});

function stringBytes(text) {
  if (typeof text !== 'string' || text.includes('\0')) throw Error('OSC strings must not contain NUL.');
  const body = textEncoder.encode(text);
  const out = new Uint8Array(body.length + 1 + pad4(body.length + 1));
  out.set(body);
  return out;
}

/** Map one JavaScript argument to an OSC type tag and its bytes. */
function encodeArg(value) {
  if (value === true) return ['T', new Uint8Array(0)];
  if (value === false) return ['F', new Uint8Array(0)];
  if (value === null) return ['N', new Uint8Array(0)];
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw Error('OSC numbers must be finite.');
    const bytes = new Uint8Array(4), view = new DataView(bytes.buffer);
    if (Number.isInteger(value) && Math.abs(value) <= 2 ** 31 - 1) {view.setInt32(0, value); return ['i', bytes];}
    view.setFloat32(0, value);
    return ['f', bytes];
  }
  if (typeof value === 'string') return ['s', stringBytes(value)];
  if (value instanceof Uint8Array) {
    const out = new Uint8Array(4 + value.length + pad4(value.length));
    new DataView(out.buffer).setInt32(0, value.length);
    out.set(value, 4);
    return ['b', out];
  }
  throw Error(`Cannot encode ${typeof value} as an OSC argument.`);
}

/** Encode `{address, args}` as one OSC message packet. */
export function encodeMessage(address, args = []) {
  if (typeof address !== 'string' || !address.startsWith('/') || address.length > 512) throw Error('An OSC address must start with "/" and be at most 512 characters.');
  if (!Array.isArray(args) || args.length > MAX_ARGS) throw Error(`An OSC message carries at most ${MAX_ARGS} arguments.`);
  const encoded = args.map(encodeArg);
  const parts = [stringBytes(address), stringBytes(`,${encoded.map(item => item[0]).join('')}`), ...encoded.map(item => item[1])];
  const out = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0));
  let offset = 0;
  for (const part of parts) {out.set(part, offset); offset += part.length;}
  return out;
}

function readString(bytes, offset, end) {
  let stop = offset;
  while (stop < end && bytes[stop] !== 0) stop++;
  if (stop >= end) throw Error('Unterminated OSC string.');
  const next = stop + 1 + pad4(stop + 1 - offset);
  if (next > end) throw Error('OSC string is missing its padding.');
  for (let i = stop; i < next; i++) if (bytes[i] !== 0) throw Error('OSC string padding must be NUL.');
  return [utf8.decode(bytes.subarray(offset, stop)), next];
}

function decodeMessage(bytes, start, end) {
  const [address, afterAddress] = readString(bytes, start, end);
  if (!address.startsWith('/')) throw Error('An OSC message address must start with "/".');
  if (afterAddress === end) return {address, args: []};  // OSC 1.0 allows a missing type-tag string
  const [tags, afterTags] = readString(bytes, afterAddress, end);
  if (!tags.startsWith(',')) throw Error('OSC type-tag string must start with ",".');
  if (tags.length - 1 > MAX_ARGS) throw Error(`An OSC message carries at most ${MAX_ARGS} arguments.`);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const args = [];
  let offset = afterTags;
  const need = size => {if (offset + size > end) throw Error('OSC argument runs past the end of the packet.');};
  for (const tag of tags.slice(1)) {
    switch (tag) {
      case 'i': need(4); args.push(view.getInt32(offset)); offset += 4; break;
      case 'f': need(4); args.push(view.getFloat32(offset)); offset += 4; break;
      case 'h': {need(8); const big = view.getBigInt64(offset); args.push(Number(big)); offset += 8; break;}
      case 'd': need(8); args.push(view.getFloat64(offset)); offset += 8; break;
      case 't': need(8); args.push(Number(view.getBigUint64(offset))); offset += 8; break;
      case 's': {const [text, next] = readString(bytes, offset, end); args.push(text); offset = next; break;}
      case 'b': {
        need(4);
        const length = view.getInt32(offset); offset += 4;
        if (length < 0) throw Error('OSC blob length is negative.');
        need(length + pad4(length));
        args.push(bytes.slice(offset, offset + length));
        offset += length + pad4(length);
        break;
      }
      case 'T': args.push(true); break;
      case 'F': args.push(false); break;
      case 'N': args.push(null); break;
      case 'I': args.push(Infinity); break;
      default: throw Error(`Unsupported OSC type tag "${tag}".`);
    }
  }
  if (offset !== end) throw Error('OSC message has trailing bytes.');
  return {address, args};
}

function decodeInto(bytes, start, end, out, depth) {
  if (end - start < 4 || (end - start) % 4 !== 0) throw Error('OSC packet length must be a positive multiple of four.');
  if (bytes[start] === 0x23) {  // "#bundle"
    if (depth >= MAX_BUNDLE_DEPTH) throw Error('OSC bundles nest too deeply.');
    const [tag, afterTag] = readString(bytes, start, end);
    if (tag !== '#bundle') throw Error('Unknown OSC packet type.');
    let offset = afterTag + 8;  // time tag
    if (offset > end) throw Error('OSC bundle is truncated.');
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    while (offset < end) {
      if (offset + 4 > end) throw Error('OSC bundle element is truncated.');
      const size = view.getInt32(offset); offset += 4;
      if (size <= 0 || offset + size > end) throw Error('OSC bundle element size is invalid.');
      decodeInto(bytes, offset, offset + size, out, depth + 1);
      offset += size;
    }
    return;
  }
  out.push(decodeMessage(bytes, start, end));
}

/** Decode a packet (message or bundle) into a flat list of `{address, args}`. */
export function decodePacket(bytes) {
  if (!(bytes instanceof Uint8Array)) throw Error('OSC packets are byte arrays.');
  const out = [];
  decodeInto(bytes, 0, bytes.length, out, 0);
  return out;
}

/** Encode several messages as one bundle with the "immediately" time tag. */
export function encodeBundle(messages) {
  const elements = messages.map(message => encodeMessage(message.address, message.args));
  const head = stringBytes('#bundle');
  const out = new Uint8Array(head.length + 8 + elements.reduce((sum, item) => sum + 4 + item.length, 0));
  const view = new DataView(out.buffer);
  out.set(head);
  let offset = head.length;
  view.setUint32(offset + 4, 1);  // time tag 1 = immediately
  offset += 8;
  for (const element of elements) {view.setInt32(offset, element.length); out.set(element, offset + 4); offset += 4 + element.length;}
  return out;
}
