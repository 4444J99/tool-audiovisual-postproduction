import test from 'node:test';
import assert from 'node:assert/strict';
import {encodeMessage, encodeBundle, decodePacket, MAX_ARGS} from '../../tools/fx-bridge/osc-codec.mjs';

const hex = bytes => Buffer.from(bytes).toString('hex');

test('the OSC 1.0 specification example encodes to its published bytes', () => {
  // "/oscillator/4/frequency" with one float32 argument, 440.0
  assert.equal(hex(encodeMessage('/oscillator/4/frequency', [440.5])),
    '2f6f7363696c6c61746f722f342f6672657175656e6379' + '00' + '2c660000' + '43dc4000');
  assert.deepEqual(decodePacket(encodeMessage('/oscillator/4/frequency', [440.5])), [{address: '/oscillator/4/frequency', args: [440.5]}]);
});

test('every argument type survives a round trip and every packet is four-byte aligned', () => {
  const blob = Uint8Array.from([1, 2, 3, 4, 5]);
  const args = [7, -3, 0.25, 'héllo', '', true, false, null, blob, 2 ** 31 + 5];
  const packet = encodeMessage('/a/b', args);
  assert.equal(packet.length % 4, 0);
  const [message] = decodePacket(packet);
  assert.equal(message.address, '/a/b');
  // 2**31 + 5 does not fit int32, so it is sent as float32 and read back as such.
  assert.deepEqual(message.args.slice(0, 9), [7, -3, 0.25, 'héllo', '', true, false, null, blob]);
  assert.equal(message.args[9], Math.fround(2 ** 31 + 5));
  for (const text of ['', 'a', 'ab', 'abc', 'abcd', 'abcde']) assert.equal(encodeMessage('/s', [text]).length % 4, 0);
});

test('a message without arguments and one without a type-tag string both decode', () => {
  assert.deepEqual(decodePacket(encodeMessage('/ping')), [{address: '/ping', args: []}]);
  assert.deepEqual(decodePacket(Uint8Array.from([0x2f, 0x78, 0, 0])), [{address: '/x', args: []}]);
});

test('bundles flatten in order, including nested bundles', () => {
  const inner = encodeBundle([{address: '/b', args: [2]}, {address: '/c', args: [3]}]);
  const outerHead = encodeBundle([{address: '/a', args: [1]}]);
  // Splice the inner bundle in as a second element of the outer bundle.
  const outer = new Uint8Array(outerHead.length + 4 + inner.length);
  outer.set(outerHead);
  new DataView(outer.buffer).setInt32(outerHead.length, inner.length);
  outer.set(inner, outerHead.length + 4);
  assert.deepEqual(decodePacket(outer).map(m => [m.address, m.args[0]]), [['/a', 1], ['/b', 2], ['/c', 3]]);
});

test('unsupported or unrepresentable input is refused when encoding', () => {
  assert.throws(() => encodeMessage('no-slash', []), /address/);
  assert.throws(() => encodeMessage('/x', [NaN]), /finite/);
  assert.throws(() => encodeMessage('/x', [Infinity]), /finite/);
  assert.throws(() => encodeMessage('/x', [{}]), /Cannot encode/);
  assert.throws(() => encodeMessage('/x', ['a\0b']), /NUL/);
  assert.throws(() => encodeMessage('/x', new Array(MAX_ARGS + 1).fill(1)), /at most/);
  assert.throws(() => encodeMessage('/' + 'a'.repeat(600), []), /512/);
});

test('malformed packets throw instead of reading out of bounds', () => {
  const good = encodeMessage('/fx/x', [0.5, 'text']);
  for (const length of [0, 1, 2, 3, 5, good.length - 4]) assert.throws(() => decodePacket(good.slice(0, length)), Error, `truncated to ${length}`);
  assert.throws(() => decodePacket(Uint8Array.from([0x2f, 0x78, 0x78, 0x78])), /Unterminated/);
  assert.throws(() => decodePacket(Uint8Array.from([0x78, 0, 0, 0])), /start with "\/"/);
  assert.throws(() => decodePacket(Uint8Array.from([0x2f, 0, 0, 0, 0x69, 0x66, 0, 0])), /type-tag/);
  assert.throws(() => decodePacket(Uint8Array.from([0x2f, 0, 0, 0, 0x2c, 0x7a, 0, 0])), /Unsupported OSC type tag "z"/);
  assert.throws(() => decodePacket(new Uint8Array(5)), /multiple of four/);
  assert.throws(() => decodePacket('not bytes'), /byte arrays/);
  // A float argument promised but cut off.
  assert.throws(() => decodePacket(Uint8Array.from([0x2f, 0, 0, 0, 0x2c, 0x66, 0, 0, 0x3f, 0x80])), /multiple of four|past the end/);
  assert.throws(() => decodePacket(Uint8Array.from([0x2f, 0, 0, 0, 0x2c, 0x66, 0, 0])), /past the end/);
  // Trailing bytes after the last declared argument.
  assert.throws(() => decodePacket(Uint8Array.from([0x2f, 0, 0, 0, 0x2c, 0, 0, 0, 1, 2, 3, 4])), /trailing/);
});

test('blob lengths and bundle element sizes cannot lie about the packet', () => {
  const blob = encodeMessage('/b', [Uint8Array.from([1, 2, 3, 4])]);
  const forged = Uint8Array.from(blob);
  new DataView(forged.buffer).setInt32(forged.length - 8, 1 << 20);
  assert.throws(() => decodePacket(forged), /past the end/);
  new DataView(forged.buffer).setInt32(forged.length - 8, -1);
  assert.throws(() => decodePacket(forged), /negative/);
  const bundle = encodeBundle([{address: '/a', args: [1]}]);
  const bad = Uint8Array.from(bundle);
  new DataView(bad.buffer).setInt32(16, 9999);
  assert.throws(() => decodePacket(bad), /size is invalid/);
  new DataView(bad.buffer).setInt32(16, 0);
  assert.throws(() => decodePacket(bad), /size is invalid/);
  assert.throws(() => decodePacket(bundle.slice(0, 12)), Error);
});

test('bundle nesting is bounded', () => {
  let packet = encodeMessage('/deep', [1]);
  for (let depth = 0; depth < 6; depth++) {
    const head = encodeBundle([]);
    const wrapped = new Uint8Array(head.length + 4 + packet.length);
    wrapped.set(head);
    new DataView(wrapped.buffer).setInt32(head.length, packet.length);
    wrapped.set(packet, head.length + 4);
    packet = wrapped;
  }
  assert.throws(() => decodePacket(packet), /nest too deeply/);
});

test('a type-tag string may not declare more arguments than the limit', () => {
  const tags = ',' + 'i'.repeat(MAX_ARGS + 1);
  const bytes = new TextEncoder().encode(`/x\0\0${tags}\0`);
  const padded = new Uint8Array(Math.ceil(bytes.length / 4) * 4 + (MAX_ARGS + 1) * 4);
  padded.set(bytes);
  assert.throws(() => decodePacket(padded), /at most/);
});
