/**
 * Touch and MIDI control surface for the FX rack: knobs, faders and switches that
 * drive the selected processor, with hardware MIDI learn. Touch controls and MIDI
 * messages meet at one host.apply() so the rack records, undoes and renders them
 * exactly like a mouse edit.
 */
import {
  ControllerMap, parseMidi, describeControls, findControl, normToValue, valueToNorm, formatValue,
  defaultTemplate, describeSource, GLOBAL_ACTIONS, SELECTED, MAX_BINDINGS,
} from './controller-map.mjs';
import {OscSession, DEFAULT_BRIDGE_URL} from './osc-session.mjs';

const escapeHTML = text => String(text).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const GLOBAL_LABELS = {play: 'Play', record: 'Record', loop: 'Loop'};

/**
 * Listen to every MIDI input through the Web MIDI API (never with system-exclusive access).
 * `request` is navigator.requestMIDIAccess, injected so the wiring can be tested.
 */
export async function connectMidi({request, onMessage, onStatus = () => {}}) {
  if (typeof request !== 'function') throw Error('This browser does not offer Web MIDI. Touch controls still work.');
  const access = await request({sysex: false});
  const listening = new Set();
  const attach = () => {
    for (const input of access.inputs.values()) {
      if (listening.has(input.id)) continue;
      listening.add(input.id);
      input.onmidimessage = event => onMessage(event.data, input);
    }
    onStatus({inputs: [...access.inputs.values()].filter(i => i.state !== 'disconnected').map(i => i.name || i.id)});
  };
  access.onstatechange = () => {
    // A re-plugged device reuses its id; forget stale entries and listen again.
    const present = new Set([...access.inputs.values()].filter(i => i.state !== 'disconnected').map(i => i.id));
    for (const id of [...listening]) if (!present.has(id)) listening.delete(id);
    attach();
  };
  attach();
  return {
    inputs: () => [...access.inputs.values()].map(i => i.name || i.id),
    close() {
      access.onstatechange = null;
      for (const input of access.inputs.values()) input.onmidimessage = null;
      listening.clear();
    },
  };
}

const ARC = 'M 14.5 41.5 A 22 22 0 1 1 41.5 41.5';
function knobHTML(control) {
  return `<svg class="cs-knob-dial" viewBox="0 0 56 56" aria-hidden="true"><path class="cs-arc-track" d="${ARC}" pathLength="100" stroke-dasharray="75 100"/><path class="cs-arc-value" d="${ARC}" pathLength="100" stroke-dasharray="0 100"/><line class="cs-pointer" x1="28" y1="28" x2="28" y2="12"/></svg>`;
}

/**
 * host: {
 *   controller: ControllerMap, getView(): view, apply(action), gesture(key), save(), toast(text), select(delta),
 *   selectNode(id), storage?: {get(), set(value)}
 * }
 * The TouchDesigner link (OSC through tools/fx-td-bridge.mjs) shares host.apply with touch and MIDI.
 */
export function mountSurface(root, host, {request = globalThis.navigator?.requestMIDIAccess?.bind(globalThis.navigator), WebSocketImpl = globalThis.WebSocket} = {}) {
  const controller = host.controller;
  let midi = null, learnMode = false, layoutKey = '', lastBindings = '', osc = null;
  const dragging = new Set();
  const waiting = new Map();

  root.innerHTML = `
    <div class="cs-head"><button type="button" class="cs-step" data-step="-1" aria-label="Previous processor">‹</button><div class="cs-title"><span class="cs-code"></span><h2 class="cs-name"></h2><span class="cs-hint"></span></div><button type="button" class="cs-step" data-step="1" aria-label="Next processor">›</button></div>
    <div class="cs-strip" role="group" aria-label="Selected processor controls"></div>
    <div class="cs-transport" role="group" aria-label="Transport"></div>
    <div class="cs-midi">
      <div class="cs-midi-row"><button type="button" class="toggle" data-act="connect">Connect MIDI</button><button type="button" class="toggle" data-act="learn" aria-pressed="false">MIDI learn</button><button type="button" data-act="template">Load template</button></div>
      <p class="cs-status" role="status"></p>
      <details class="cs-osc"><summary>TouchDesigner (OSC)</summary>
        <div class="cs-midi-row"><input class="cs-osc-url" type="text" inputmode="url" spellcheck="false" aria-label="Bridge address" value="${DEFAULT_BRIDGE_URL}"><button type="button" class="toggle" data-act="osc-connect" aria-pressed="false">Connect bridge</button><label class="cs-osc-feedback"><input type="checkbox" data-act="osc-feedback" checked> Send rack state back</label></div>
        <p class="cs-osc-status" role="status"></p>
      </details>
      <details><summary>Mappings</summary><div class="cs-midi-row"><button type="button" data-act="clear">Clear mappings</button><button type="button" data-act="export">Export map</button><button type="button" data-act="import">Import map</button><input class="cs-import" type="file" accept="application/json,.json" hidden></div><ul class="cs-bindings"></ul></details>
    </div>`;
  const $ = selector => root.querySelector(selector);
  const strip = $('.cs-strip'), transport = $('.cs-transport'), statusLine = $('.cs-status');
  const setStatus = text => {statusLine.textContent = text;};

  const selectedNode = view => view.nodes.find(node => node.id === view.selectedId);
  const controlFor = (view, element) => {
    const node = selectedNode(view);
    return node && findControl(node, {path: element.dataset.path}, view);
  };
  const currentOf = (view, control) => view.current(view.selectedId, control.path);

  function change(control, value, drag = null) {
    const view = host.getView();
    if (value === currentOf(view, control)) return;
    const key = `surface:${view.selectedId}:${control.path}`;
    // A drag is one gesture for as long as the pointer is down, however long it pauses; other edits use the idle gap.
    if (!drag) host.gesture(key);
    else if (!drag.snapped) {drag.snapped = true; host.endGesture?.(key); host.gesture(key);}
    host.apply({nodeId: view.selectedId, path: control.path, value, binding: 'surface'});
    update();
  }

  function build() {
    const view = host.getView(), node = selectedNode(view);
    layoutKey = node ? `${node.id}|${node.type}|${view.duration}` : '';
    if (!node) {strip.innerHTML = '<p class="cs-empty">Select a processor to control it.</p>'; transport.innerHTML = ''; return;}
    const {ranges, switches} = describeControls(node, view);
    const faders = ranges.slice(0, 2), knobs = ranges.slice(2);
    const ctl = (control, kind) => `<div class="cs-ctl cs-kind-${kind}" data-path="${escapeHTML(control.path)}" data-kind="${kind}">
        ${kind === 'fader' ? '<div class="cs-fader-track" tabindex="0" role="slider"><div class="cs-fader-fill"></div><div class="cs-fader-cap"></div></div>'
          : kind === 'knob' ? `<div class="cs-knob-face" tabindex="0" role="slider">${knobHTML(control)}</div>`
          : '<button type="button" class="cs-switch toggle" aria-pressed="false"></button>'}
        ${kind === 'switch' ? '' : `<span class="cs-label">${escapeHTML(control.label)}</span><output class="cs-value"></output>`}<span class="cs-badge"></span></div>`;
    strip.innerHTML = `<div class="cs-faders">${faders.map(c => ctl(c, 'fader')).join('')}</div><div class="cs-knobs">${knobs.map(c => ctl(c, 'knob')).join('')}</div><div class="cs-switches">${switches.map(c => ctl(c, 'switch')).join('')}</div>`;
    transport.innerHTML = GLOBAL_ACTIONS.map(name => `<div class="cs-ctl cs-global" data-global="${name}"><button type="button" class="cs-switch toggle" aria-pressed="false">${GLOBAL_LABELS[name]}</button><span class="cs-badge"></span></div>`).join('');
  }

  function badgeFor(target) {
    const binding = controller.bindingForTarget(target);
    if (controller.learning && JSON.stringify(controller.learning) === JSON.stringify(target)) return 'Move a control…';
    return binding ? `${describeSource(binding.source)}${binding.target.nodeId === SELECTED ? '' : ' · fixed'}` : '';
  }
  const slotTarget = (view, control) => {
    const node = selectedNode(view), {ranges, switches} = describeControls(node, view);
    const index = ranges.indexOf(ranges.find(c => c.path === control.path));
    return index >= 0 ? {nodeId: SELECTED, slot: `knob:${index}`} : {nodeId: SELECTED, slot: `switch:${switches.findIndex(c => c.path === control.path)}`};
  };

  function update() {
    const view = host.getView(), node = selectedNode(view);
    const key = node ? `${node.id}|${node.type}|${view.duration}` : '';
    if (key !== layoutKey) {build(); controller.resetPickup(); waiting.clear();}
    if (!node) return;
    const definition = node.name || node.type;
    $('.cs-code').textContent = node.type;
    $('.cs-name').textContent = definition;
    $('.cs-hint').textContent = learnMode ? 'MIDI learn: tap a control, then move a knob, fader or key on your controller.' : 'Drag a knob or fader, or tap a switch.';
    for (const element of strip.querySelectorAll('.cs-ctl')) {
      const control = controlFor(view, element);
      if (!control) continue;
      const value = currentOf(view, control);
      const isDragging = dragging.has(element);
      if (control.kind === 'toggle') {
        const button = element.querySelector('button');
        button.setAttribute('aria-pressed', String(Boolean(value)));
        button.textContent = control.label;
      } else if (!isDragging) {
        const n = valueToNorm(control, value), face = element.querySelector('[role=slider]');
        element.style.setProperty('--n', n);
        face.setAttribute('aria-valuemin', control.min); face.setAttribute('aria-valuemax', control.max); face.setAttribute('aria-valuenow', value);
        face.setAttribute('aria-valuetext', formatValue(control, value)); face.setAttribute('aria-label', control.label);
        element.querySelector('.cs-value').textContent = formatValue(control, value);
        const arc = element.querySelector('.cs-arc-value');
        if (arc) {arc.setAttribute('stroke-dasharray', `${(75 * n).toFixed(2)} 100`); element.querySelector('.cs-pointer').setAttribute('transform', `rotate(${(-135 + 270 * n).toFixed(1)} 28 28)`);}
        const fill = element.querySelector('.cs-fader-fill');
        if (fill) fill.style.height = `${(n * 100).toFixed(1)}%`;
        const cap = element.querySelector('.cs-fader-cap');
        if (cap) cap.style.bottom = `${(n * 100).toFixed(1)}%`;
      }
      const target = slotTarget(view, control);
      const wait = waiting.get(control.path);
      element.classList.toggle('waiting', Boolean(wait));
      element.querySelector('.cs-badge').textContent = wait ? `Move hardware to ${Math.round(wait.software * 100)}% to take over` : badgeFor(target);
      element.classList.toggle('armed', Boolean(controller.learning) && JSON.stringify(controller.learning) === JSON.stringify(target));
    }
    for (const element of transport.querySelectorAll('.cs-global')) {
      const name = element.dataset.global, on = Boolean(view.global(name));
      element.querySelector('button').setAttribute('aria-pressed', String(on));
      const target = {global: name};
      element.querySelector('.cs-badge').textContent = badgeFor(target);
      element.classList.toggle('armed', Boolean(controller.learning) && controller.learning.global === name);
    }
    const list = $('.cs-bindings');
    const markup = controller.bindings.map(b => `<li>${escapeHTML(describeSource(b.source))} → ${escapeHTML(b.target.global ? GLOBAL_LABELS[b.target.global] : `${b.target.nodeId === SELECTED ? 'selected' : b.target.nodeId} · ${b.target.slot ?? b.target.path}`)} <small>${escapeHTML(b.mode)}${b.pickup ? ' · pickup' : ''}</small></li>`).join('') || '<li class="cs-empty">No hardware mapped yet. Connect a controller and choose Load template, or use MIDI learn.</li>';
    if (markup !== lastBindings) {lastBindings = markup; list.innerHTML = markup;}
    $('[data-act=learn]').setAttribute('aria-pressed', String(learnMode));
    $('[data-act=connect]').setAttribute('aria-pressed', String(Boolean(midi)));
    $('[data-act=connect]').textContent = midi ? 'MIDI connected' : 'Connect MIDI';
    const linked = Boolean(osc) && osc.wanted;
    $('[data-act=osc-connect]').setAttribute('aria-pressed', String(linked));
    $('[data-act=osc-connect]').textContent = linked ? 'Disconnect bridge' : 'Connect bridge';
  }

  const oscStatus = ({state, detail, received, sent, bridge}) => {
    const text = {off: 'Not connected.', connecting: `Connecting to ${detail}…`, open: `Connected to ${detail}${bridge ? ` · bridge v${bridge.version}, TouchDesigner at ${bridge.touchdesigner?.host}:${bridge.touchdesigner?.port}` : ''}. Received ${received}, sent ${sent}.`,
      retrying: detail, unsupported: detail}[state] || '';
    $('.cs-osc-status').textContent = text;
    update();
  };
  function toggleOsc() {
    const input = $('.cs-osc-url');
    if (osc?.wanted) {osc.disconnect(); host.storage?.set({...host.storage.get(), osc: {url: input.value, connect: false, feedback: osc.feedbackOn}}); return;}
    osc ??= new OscSession(host, {WebSocketImpl, onStatus: oscStatus});
    try {
      osc.setFeedback($('[data-act=osc-feedback]').checked);
      osc.connect(input.value);
      host.storage?.set({...host.storage.get(), osc: {url: input.value, connect: true, feedback: osc.feedbackOn}});
    } catch (error) {$('.cs-osc-status').textContent = error.message;}
    update();
  }

  // Touch and pointer interaction ---------------------------------------------------
  function arm(target) {
    controller.armLearn(target);
    setStatus('Waiting for a MIDI message…');
    update();
  }

  strip.addEventListener('pointerdown', event => {
    const element = event.target.closest('.cs-ctl');
    if (!element || event.button > 0) return;
    const view = host.getView(), control = controlFor(view, element);
    if (!control) return;
    if (learnMode) {event.preventDefault(); arm(slotTarget(view, control)); return;}
    if (control.kind === 'toggle') return;
    const face = element.querySelector('[role=slider]');
    event.preventDefault(); face.focus({preventScroll: true});
    const rect = element.querySelector('.cs-fader-track')?.getBoundingClientRect();
    dragging.add(element);
    const drag = {snapped: false};
    const start = {y: event.clientY, norm: valueToNorm(control, currentOf(view, control))};
    const move = ev => {
      if (ev.pointerId !== event.pointerId) return;
      const fine = ev.shiftKey ? 4 : 1;
      const norm = rect ? 1 - (ev.clientY - rect.top) / Math.max(1, rect.height) : start.norm + (start.y - ev.clientY) / (170 * fine);
      const value = normToValue(control, norm);
      element.querySelector('.cs-value').textContent = formatValue(control, value);
      element.style.setProperty('--n', valueToNorm(control, value));
      change(control, value, drag);
      // Keep the dragged control showing its own position while the rack renders.
      const n = valueToNorm(control, value), arc = element.querySelector('.cs-arc-value');
      if (arc) {arc.setAttribute('stroke-dasharray', `${(75 * n).toFixed(2)} 100`); element.querySelector('.cs-pointer').setAttribute('transform', `rotate(${(-135 + 270 * n).toFixed(1)} 28 28)`);}
      const fill = element.querySelector('.cs-fader-fill'), cap = element.querySelector('.cs-fader-cap');
      if (fill) fill.style.height = `${(n * 100).toFixed(1)}%`;
      if (cap) cap.style.bottom = `${(n * 100).toFixed(1)}%`;
    };
    const finish = ev => {
      if (ev.pointerId !== event.pointerId) return;
      removeEventListener('pointermove', move); removeEventListener('pointerup', finish); removeEventListener('pointercancel', finish);
      dragging.delete(element); host.endGesture?.(`surface:${host.getView().selectedId}:${control.path}`);
      controller.resetPickup(); update();
    };
    addEventListener('pointermove', move); addEventListener('pointerup', finish); addEventListener('pointercancel', finish);
    if (rect) move(event);
  });

  strip.addEventListener('dblclick', event => {
    const element = event.target.closest('.cs-ctl'), view = host.getView(), control = element && controlFor(view, element);
    if (!control || control.kind === 'toggle' || learnMode) return;
    change(control, control.default);
    controller.resetPickup();
  });

  strip.addEventListener('keydown', event => {
    const element = event.target.closest('.cs-ctl');
    if (!element) return;
    const view = host.getView(), control = controlFor(view, element);
    if (!control) return;
    if (learnMode && (event.key === 'Enter' || event.key === ' ') && control.kind !== 'toggle') {event.preventDefault(); arm(slotTarget(view, control)); return;}
    if (control.kind === 'toggle') return;
    const stepsPerKey = {ArrowUp: 1, ArrowRight: 1, ArrowDown: -1, ArrowLeft: -1, PageUp: 10, PageDown: -10}[event.key];
    const current = currentOf(view, control);
    let next;
    if (stepsPerKey) next = normToValue({...control, step: 0}, valueToNorm(control, current + stepsPerKey * control.step));
    else if (event.key === 'Home') next = control.min;
    else if (event.key === 'End') next = control.max;
    else return;
    event.preventDefault();
    change(control, normToValue(control, valueToNorm(control, next)));
    controller.resetPickup();
  });

  root.addEventListener('click', event => {
    const view = host.getView();
    const step = event.target.closest('[data-step]');
    if (step) {host.select(Number(step.dataset.step)); return;}
    const element = event.target.closest('.cs-ctl');
    if (element?.dataset.global) {
      if (learnMode) {arm({global: element.dataset.global}); return;}
      host.gesture(`global:${element.dataset.global}`);
      host.apply({global: element.dataset.global, value: !view.global(element.dataset.global), binding: 'surface'});
      update();
      return;
    }
    const button = element && element.querySelector('.cs-switch');
    if (button && event.target.closest('.cs-switch')) {
      const control = controlFor(view, element);
      if (!control) return;
      if (learnMode) arm(slotTarget(view, control)); else change(control, !currentOf(view, control));
      return;
    }
    const act = event.target.closest('[data-act]')?.dataset.act;
    if (act) action(act);
  });

  async function connect() {
    if (midi) return;
    try {
      midi = await connectMidi({request, onMessage: handle, onStatus: ({inputs}) => {setStatus(inputs.length ? `Listening to ${inputs.join(', ')}.` : 'MIDI is on. Plug in a controller; it is picked up automatically.'); update();}});
      host.storage?.set({...host.storage.get(), connect: true});
    } catch (error) {
      midi = null; setStatus(error.message || 'MIDI access was not granted.');
    }
    update();
  }

  function action(name) {
    if (name === 'connect') {if (!midi) connect(); else setStatus('MIDI is connected.');}
    else if (name === 'osc-connect') toggleOsc();
    else if (name === 'learn') {learnMode = !learnMode; controller.cancelLearn(); setStatus(learnMode ? 'Tap a control on screen, then move the hardware control to bind it.' : ''); update();}
    else if (name === 'template') {
      for (const binding of defaultTemplate()) controller.bind(binding);
      host.save(); setStatus('Template loaded: CC 20–27 on any channel drive the eight controls of the selected processor, notes 36–40 switch Bypass, Solo, Play, Record and Loop.'); update();
    } else if (name === 'clear') {controller.clear(); host.save(); setStatus('All mappings cleared.'); update();}
    else if (name === 'import') root.querySelector('.cs-import').click();
    else if (name === 'export') {
      const blob = new Blob([JSON.stringify(controller.toJSON(), null, 2)], {type: 'application/json'});
      const url = URL.createObjectURL(blob), a = document.createElement('a');
      a.href = url; a.download = 'god-here-controller-map.json'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  }

  root.querySelector('[data-act=osc-feedback]').addEventListener('change', event => {osc?.setFeedback(event.target.checked);});
  root.querySelector('.cs-import').addEventListener('change', async event => {
    const file = event.target.files[0]; event.target.value = '';
    if (!file) return;
    try {
      if (file.size > 200_000) throw Error('Controller map exceeds 200 KB.');
      const next = ControllerMap.fromJSON(JSON.parse(await file.text()));
      controller.clear();
      for (const binding of next.bindings) controller.bind(binding);
      host.save(); setStatus(`Imported ${next.bindings.length} mappings (limit ${MAX_BINDINGS}).`); update();
    } catch (error) {setStatus(`Import rejected: ${error.message}`);}
  });

  function handle(data) {
    const message = parseMidi(data);
    if (!message) return;
    const view = host.getView();
    const result = controller.process(message, view);
    if (result.learned) {
      learnMode = false; host.save();
      setStatus(`Bound ${describeSource(result.learned.source)}.`); update(); return;
    }
    waiting.clear();
    for (const wait of result.waiting) waiting.set(wait.path, wait);
    for (const action of result.actions) {
      host.gesture(action.global ? `global:${action.global}` : `midi:${action.binding}:${action.path}`);
      host.apply(action);
    }
    if (result.actions.length || result.waiting.length) update();
  }

  update();
  try {if (host.storage?.get().connect && request) connect();} catch {/* storage is optional */}
  try {
    const saved = host.storage?.get().osc;
    if (saved && typeof saved.url === 'string') {
      $('.cs-osc-url').value = saved.url; $('[data-act=osc-feedback]').checked = saved.feedback !== false;
      if (saved.connect && WebSocketImpl) toggleOsc();
    }
  } catch {/* storage is optional */}
  return {update, handle, learning: () => learnMode, osc: () => osc, destroy() {midi?.close(); osc?.disconnect();}};
}
