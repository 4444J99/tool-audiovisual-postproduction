/* Silence-at-the-source init script. Injected by silent_browser.py and silent-browser.mjs into every Playwright
 * context before any page script runs, in every engine (Chromium, WebKit, Firefox). It is never part of the published site.
 *
 *  - Media elements: the native muted and volume are pinned to true and 0 on every audio and video element,
 *    whenever it is created, parsed, loaded or played. The app keeps seeing the values it asked for (a shadow copy
 *    answers reads), so tests that read back muted or volume still hold.
 *  - An element routed into Web Audio through createMediaElementSource is released from the pin (a muted element
 *    would feed silence into the graph and break the analyser suites) and its sound is silenced in the graph.
 *  - Web Audio: every connection to a realtime AudioContext destination goes through a per-context GainNode at
 *    gain 0, so nothing reaches the device. OfflineAudioContext keeps its real destination: it renders to a buffer.
 *  - speechSynthesis utterances are spoken at volume 0, and an utterance whose text carries markup (SSML) is spoken as empty.
 *
 * Limit: media parsed inside a closed declarative shadow root (<template shadowrootmode="closed">) has no handle a page
 * script can reach, so it cannot be pinned here. Chromium covers it with --mute-audio. WebKit refuses such a page
 * at pre-flight (assert_no_closed_shadow_roots / assertNoClosedShadowRoots), so a closed root never reaches a
 * browser unnoticed. Firefox has no engine mute: Playwright cannot set the float pref media.volume_scale via
 * firefoxUserPrefs (a number breaks every launch with NS_ERROR_UNEXPECTED from setIntPref; a string is ignored
 * by the float getter), so Firefox relies on this script alone and no pref is injected.
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
    // volumechange is what a native change fires. The native value never changes for a pinned element, so the event
    // is fired here whenever the value the app sees changes, and the app's UI listeners keep working.
    const announce = element => { try { element.dispatchEvent(new Event('volumechange')); } catch (error) { /* detached or sealed */ } };
    // The volume range check runs on a detached probe, so a bad value throws exactly as natively and a good value
    // never touches (or fires events on) the pinned element.
    const NativeAudioCtor = typeof window.Audio === 'function' ? window.Audio : null;
    const volumeProbe = NativeAudioCtor ? new NativeAudioCtor() : null;
    const validated = (element, value) => {
      if (!volumeProbe) { nativeVolume.set.call(element, value); return nativeVolume.get.call(element); }
      nativeVolume.set.call(volumeProbe, value);
      return nativeVolume.get.call(volumeProbe);
    };
    Object.defineProperty(media, 'muted', {
      configurable: true, enumerable: nativeMuted.enumerable,
      get() { ensure(this); return shadow.get(this).muted; },
      set(value) {
        ensure(this);
        const state = shadow.get(this), asked = Boolean(value), changed = state.muted !== asked;
        state.muted = asked;
        if (captured.has(this)) nativeMuted.set.call(this, value);
        else { pin(this); if (changed) announce(this); }
      },
    });
    Object.defineProperty(media, 'volume', {
      configurable: true, enumerable: nativeVolume.enumerable,
      get() { ensure(this); return shadow.get(this).volume; },
      set(value) {
        const asked = validated(this, value);
        ensure(this);
        const state = shadow.get(this), changed = state.volume !== asked;
        state.volume = asked;
        if (captured.has(this)) nativeVolume.set.call(this, value);
        else { pin(this); if (changed) announce(this); }
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
    // Parser-created and attached elements: catch them as they enter the document (or a shadow root) and as their
    // loading begins. document.open() (what setContent and document.write use on a blank page) drops window listeners
    // and replaces the root element, so the document watch is armed again after every open.
    const watched = new WeakSet(), observers = [];
    const onMediaEvent = event => { if (event.target instanceof HTMLMediaElement) ensure(event.target); };
    // volumechange: native controls (or anything else) that change the native state of a pinned element are put back.
    const MEDIA_EVENTS = ['loadstart', 'loadedmetadata', 'canplay', 'play', 'playing', 'volumechange'];
    const sweep = root => {
      if (!root || root.nodeType !== 1) return;
      if (root instanceof HTMLMediaElement) ensure(root);
      for (const element of root.querySelectorAll('audio,video')) ensure(element);
      // Declarative shadow roots are attached by the parser, not by attachShadow.
      if (root.shadowRoot) watchRoot(root.shadowRoot);
      for (const element of root.querySelectorAll('*')) if (element.shadowRoot) watchRoot(element.shadowRoot);
    };
    function watchRoot(root) {
      if (watched.has(root)) return;
      watched.add(root);
      for (const element of root.querySelectorAll('audio,video')) ensure(element);
      const observer = new MutationObserver(records => { for (const record of records) for (const node of record.addedNodes) sweep(node); });
      observer.observe(root, {childList: true, subtree: true});
      observers.push(observer);
      for (const type of MEDIA_EVENTS) root.addEventListener(type, onMediaEvent, true);
      // Roots nested inside this one (declarative or already attached) are found here, so every depth is watched.
      for (const element of root.querySelectorAll('*')) if (element.shadowRoot) watchRoot(element.shadowRoot);
    }
    let documentObserver = null;
    const arm = () => {
      sweep(document.documentElement);
      if (documentObserver) documentObserver.disconnect();
      documentObserver = new MutationObserver(records => { for (const record of records) for (const node of record.addedNodes) sweep(node); });
      documentObserver.observe(document, {childList: true, subtree: true});
      for (const type of MEDIA_EVENTS) window.addEventListener(type, onMediaEvent, true);
    };
    arm();
    const nativeAttachShadow = Element.prototype.attachShadow;
    Object.defineProperty(Element.prototype, 'attachShadow', {
      configurable: true, writable: true, enumerable: true,
      value: function attachShadow(...args) { const root = nativeAttachShadow.apply(this, args); watchRoot(root); return root; },
    });
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
    // An element routed into the graph is released from the pin: its requested muted and volume apply natively, and the
    // graph is silenced at the destination. Both creation paths do this: the factory and the constructor.
    const capture = element => {
      if (!(element instanceof HTMLMediaElement)) return;
      ensure(element); captured.add(element);
      const asked = shadow.get(element);
      nativeVolume.set.call(element, asked.volume); nativeMuted.set.call(element, asked.muted);
    };
    for (const Context of Realtime) {
      const createSource = Context.prototype.createMediaElementSource;
      if (typeof createSource !== 'function') continue;
      Object.defineProperty(Context.prototype, 'createMediaElementSource', {
        configurable: true, writable: true, enumerable: true,
        value: function createMediaElementSource(element) {
          const node = createSource.call(this, element);
          capture(element);
          return node;
        },
      });
    }
    if (typeof window.MediaElementAudioSourceNode === 'function') {
      const NativeSourceNode = window.MediaElementAudioSourceNode;
      const SilentSourceNode = function MediaElementAudioSourceNode(context, options) {
        const node = Reflect.construct(NativeSourceNode, options === undefined ? [context] : [context, options], new.target || SilentSourceNode);
        capture(node.mediaElement);  // the element native construction bound (an accessor option is read once, by native code)
        return node;
      };
      SilentSourceNode.prototype = NativeSourceNode.prototype;
      Object.defineProperty(window, 'MediaElementAudioSourceNode', {configurable: true, writable: true, enumerable: false, value: SilentSourceNode});
    }
  }

  if (window.speechSynthesis && window.SpeechSynthesis) {
    const speak = SpeechSynthesis.prototype.speak;
    Object.defineProperty(SpeechSynthesis.prototype, 'speak', {
      configurable: true, writable: true, enumerable: true,
      value: function (utterance) {
        // utterance.volume does not bind SSML: markup can carry its own loud <prosody volume>, so markup is spoken as nothing.
        try { utterance.volume = 0; if (/<[^>]+>/.test(String(utterance.text))) utterance.text = ''; } catch (error) { /* not an utterance */ }
        return speak.call(this, utterance);
      },
    });
  }

  Object.defineProperty(window, '__SILENT_BROWSER__', {
    configurable: false, enumerable: false,
    value: Object.freeze({
      version: 1,
      native: element => ({muted: nativeMuted.get.call(element), volume: nativeVolume.get.call(element)}),
      captured: element => captured.has(element),
      // A description of the sink, never the node: page code must not be able to raise the zero gain.
      sinkInfo: context => {
        if (!isRealtime(context)) return null;
        const sink = sinkFor(context);
        return {gain: sink.gain.value, isGain: sink instanceof GainNode, owner: sink.context === context};
      },
    }),
  });
})();
