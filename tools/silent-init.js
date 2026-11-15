/* Silence-at-the-source init script. Injected by silent_browser.py and silent-browser.mjs into every Playwright
 * context before any page script runs, in every engine (Chromium, WebKit, Firefox). It is never part of the artwork.
 *
 *  - Media elements: the native muted and volume are pinned to true and 0 on every audio and video element,
 *    whenever it is created, parsed, loaded or played. The app keeps seeing the values it asked for (a shadow copy
 *    answers reads), so tests that read back muted or volume still hold.
 *  - An element routed into Web Audio through createMediaElementSource is released from the pin (a muted element
 *    would feed silence into the graph and break the analyser suites) and its sound is silenced in the graph.
 *  - Web Audio: every connection to a realtime AudioContext destination goes through a per-context GainNode at
 *    gain 0, so nothing reaches the device. OfflineAudioContext keeps its real destination: it renders to a buffer.
 *  - speechSynthesis utterances are spoken at volume 0.
 */
(() => {
  'use strict';
  if (window.__SILENT_BROWSER__) return;
  const shadow = new WeakMap(), captured = new WeakSet(), sinks = new WeakMap();
  const media = typeof HTMLMediaElement === 'undefined' ? null : HTMLMediaElement.prototype;
  const nativeMuted = media && Object.getOwnPropertyDescriptor(media, 'muted');
  const nativeVolume = media && Object.getOwnPropertyDescriptor(media, 'volume');

  const pin = element => {
    if (captured.has(element)) return;
    nativeMuted.set.call(element, true);
    nativeVolume.set.call(element, 0);
  };
  const ensure = element => {
    if (!(element instanceof HTMLMediaElement)) return element;
    if (!shadow.has(element)) {
      shadow.set(element, {muted: nativeMuted.get.call(element), volume: nativeVolume.get.call(element)});
    }
    pin(element);
    return element;
  };

  if (media && nativeMuted && nativeVolume) {
    Object.defineProperty(media, 'muted', {
      configurable: true, enumerable: nativeMuted.enumerable,
      get() { ensure(this); return shadow.get(this).muted; },
      set(value) {
        ensure(this); shadow.get(this).muted = Boolean(value);
        if (captured.has(this)) nativeMuted.set.call(this, value); else pin(this);
      },
    });
    Object.defineProperty(media, 'volume', {
      configurable: true, enumerable: nativeVolume.enumerable,
      get() { ensure(this); return shadow.get(this).volume; },
      set(value) {
        // The native setter validates the range and throws; run it first so the shadow never holds a bad value.
        nativeVolume.set.call(this, value);
        const asked = nativeVolume.get.call(this); ensure(this);
        shadow.get(this).volume = asked;
        if (!captured.has(this)) pin(this);
      },
    });
    for (const name of ['play', 'load']) {
      const original = media[name];
      Object.defineProperty(media, name, {
        configurable: true, writable: true, enumerable: false,
        value: function (...args) { ensure(this); return original.apply(this, args); },
      });
    }
    const original = Document.prototype.createElement;
    Object.defineProperty(Document.prototype, 'createElement', {
      configurable: true, writable: true, enumerable: false,
      value: function (...args) { return ensure(original.apply(this, args)); },
    });
    if (typeof window.Audio === 'function') {
      const NativeAudio = window.Audio;
      const SilentAudio = function Audio(...args) { return ensure(Reflect.construct(NativeAudio, args, new.target || SilentAudio)); };
      SilentAudio.prototype = NativeAudio.prototype;
      Object.defineProperty(window, 'Audio', {configurable: true, writable: true, enumerable: false, value: SilentAudio});
    }
    // Parser-created and attached elements: catch them as they enter the document and as their loading begins.
    // document.open() (what setContent and document.write use on a blank page) drops window listeners and replaces
    // the root element, so the watch is armed again after every open.
    const sweep = root => {
      if (!root || root.nodeType !== 1) return;
      if (root instanceof HTMLMediaElement) ensure(root);
      for (const element of root.querySelectorAll('audio,video')) ensure(element);
    };
    const onMediaEvent = event => { if (event.target instanceof HTMLMediaElement) ensure(event.target); };
    let observer = null;
    const arm = () => {
      sweep(document.documentElement);
      if (observer) observer.disconnect();
      observer = new MutationObserver(records => { for (const record of records) for (const node of record.addedNodes) sweep(node); });
      observer.observe(document, {childList: true, subtree: true});
      for (const type of ['loadstart', 'loadedmetadata', 'canplay', 'play', 'playing']) window.addEventListener(type, onMediaEvent, true);
    };
    arm();
    const nativeOpen = Document.prototype.open;
    Object.defineProperty(Document.prototype, 'open', {
      configurable: true, writable: true, enumerable: true,
      value: function open(...args) { const result = nativeOpen.apply(this, args); arm(); return result; },
    });
  }

  const Realtime = [window.AudioContext, window.webkitAudioContext].filter(Boolean);
  const Offline = window.OfflineAudioContext || window.webkitOfflineAudioContext;
  const isRealtime = context => Realtime.some(Context => context instanceof Context) && !(Offline && context instanceof Offline);
  const sinkFor = context => {
    let sink = sinks.get(context);
    if (!sink) {
      sink = context.createGain(); sink.gain.value = 0;
      originalConnect.call(sink, context.destination);
      sinks.set(context, sink);
    }
    return sink;
  };
  const targetOf = (node, destination) => {
    if (typeof AudioDestinationNode === 'undefined' || !(destination instanceof AudioDestinationNode)) return destination;
    return isRealtime(destination.context) ? sinkFor(destination.context) : destination;
  };
  let originalConnect = null;
  if (window.AudioNode && Realtime.length) {
    originalConnect = AudioNode.prototype.connect;
    const originalDisconnect = AudioNode.prototype.disconnect;
    Object.defineProperty(AudioNode.prototype, 'connect', {
      configurable: true, writable: true, enumerable: true,
      value: function connect(destination, ...rest) {
        const target = targetOf(this, destination);
        const result = originalConnect.call(this, target, ...rest);
        return target === destination ? result : destination;
      },
    });
    Object.defineProperty(AudioNode.prototype, 'disconnect', {
      configurable: true, writable: true, enumerable: true,
      value: function disconnect(destination, ...rest) {
        if (destination === undefined) return originalDisconnect.call(this);
        return originalDisconnect.call(this, targetOf(this, destination), ...rest);
      },
    });
    for (const Context of Realtime) {
      const createSource = Context.prototype.createMediaElementSource;
      if (typeof createSource !== 'function') continue;
      Object.defineProperty(Context.prototype, 'createMediaElementSource', {
        configurable: true, writable: true, enumerable: true,
        value: function createMediaElementSource(element) {
          const node = createSource.call(this, element);
          if (element instanceof HTMLMediaElement) {
            ensure(element); captured.add(element);
            const asked = shadow.get(element);
            nativeVolume.set.call(element, asked.volume); nativeMuted.set.call(element, asked.muted);
          }
          return node;
        },
      });
    }
  }

  if (window.speechSynthesis && window.SpeechSynthesis) {
    const speak = SpeechSynthesis.prototype.speak;
    Object.defineProperty(SpeechSynthesis.prototype, 'speak', {
      configurable: true, writable: true, enumerable: true,
      value: function (utterance) { try { utterance.volume = 0; } catch (error) { /* not an utterance */ } return speak.call(this, utterance); },
    });
  }

  Object.defineProperty(window, '__SILENT_BROWSER__', {
    configurable: false, enumerable: false,
    value: Object.freeze({
      version: 1,
      native: element => ({muted: nativeMuted.get.call(element), volume: nativeVolume.get.call(element)}),
      captured: element => captured.has(element),
      sink: context => (isRealtime(context) ? sinkFor(context) : null),
    }),
  });
})();
