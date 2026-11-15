"""Parameter resolution for the FX rack network: automation, events, macros, sidechain modulation, clip scope, Solo.

This module is the text of a Text DAT (`td_resolve`) inside the generated rack component, and it also runs under
plain CPython: it never imports `td`. It is a port of the workbench's `evaluatedNode` (web/fx/core.mjs), and
`tests/test_td_network.py` checks it against that function over every fixture patch at sampled times, so the two
cannot drift apart silently.

What each processor's effect reads is not its raw custom parameters but the resolved values this module writes
into the processor's `resolved` Table DAT, once per frame. A custom parameter is the base value (what a person or
the rack's OSC report set); the resolved value is what the workbench would render at the rack's playhead:

    base -> automation lane -> event -> clamp -> sidechain modulation          (Intensity, Wet)
    macro mapping from the resolved Intensity                                  (processor parameters)
    base -> automation lane -> event -> clamp -> sidechain modulation          (processor parameters; wins over the macro)
    Solo, Bypass events; Solo is rack-scoped; a clip scope silences a processor outside its range.

The resolver itself is pure: `resolve_rack(contract, bases, time, controls)`. The TouchDesigner glue is `tick(rack)`,
called from an Execute DAT at the start of every frame.
"""
import json
import math


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _clamp(value, low, high):
    return max(low, min(high, value))


def _round(value):
    """JavaScript Math.round: halves go toward positive infinity."""
    return math.floor(value + 0.5)


def evaluate_lane(lane, time, base):
    """One automation lane at a time (core.mjs evaluateAutomation)."""
    frames = lane.get('keyframes') if isinstance(lane, dict) else None
    if not isinstance(frames, list) or not frames:
        return base
    if time <= frames[0]['time']:
        return frames[0]['value']
    if time >= frames[-1]['time']:
        return frames[-1]['value']
    for index in range(1, len(frames)):
        following, previous = frames[index], frames[index - 1]
        if time <= following['time']:
            if time == following['time']:
                return following['value']
            if lane.get('interpolation') == 'step':
                return previous['value']
            ratio = (time - previous['time']) / (following['time'] - previous['time'])
            return previous['value'] + (following['value'] - previous['value']) * ratio
    return base


def event_value(events, parameter, time, base):
    """The latest event at or before `time` for one parameter; a later list entry wins a tie."""
    value, applied = base, -math.inf
    for event in events:
        if event['parameter'] == parameter and event['time'] <= time and event['time'] >= applied:
            value, applied = event['value'], event['time']
    return value


def curve_at(curve, value):
    if curve == 'ease-in':
        return value * value
    if curve == 'ease-out':
        return 1 - (1 - value) ** 2
    if curve == 'smoothstep':
        return value * value * (3 - 2 * value)
    return value


def _bounded(spec, path, value):
    """Clamp a resolved value onto its range; a parameter with step 1 is rounded to an integer."""
    if path.startswith('params.'):
        item = spec['params'].get(path[7:])
        if item is not None:
            bounded = _clamp(value, item['min'], item['max'])
            return _round(bounded) if item['step'] == 1 else bounded
        return value
    if path in ('intensity', 'wet'):
        return _clamp(value, 0, 1)
    return value


def _lanes(spec, parameter):
    return [lane for lane in spec['automation'] if lane['parameter'] == parameter]


def resolve_node(spec, base, time, control=0.0, others_soloed=None):
    """Resolve one processor. `spec` is the node's `resolve` block of the contract; `base` holds its base values:
    {'intensity', 'wet', 'params': {key: value}, 'bypass', 'solo'}. Returns the resolved values and, in
    'solo' / 'bypass', the event-resolved switches (before the rack-wide Solo is applied)."""
    if not _finite(time):
        raise ValueError('resolve time must be finite')
    events = spec['events']
    result = {'intensity': base['intensity'], 'wet': base['wet'], 'params': dict(base['params'])}
    envelope = _clamp(control, 0, 1) if _finite(control) else 0.0
    modulation = spec.get('modulation')

    def modulate(parameter):
        if not modulation or modulation['parameter'] != parameter:
            return
        current = result[parameter] if not parameter.startswith('params.') else result['params'][parameter[7:]]
        if modulation['mode'] == 'multiply':
            value = current * (1 + modulation['amount'] * envelope)
        else:
            value = current + modulation['amount'] * envelope
        value = _bounded(spec, parameter, value)
        if parameter.startswith('params.'):
            result['params'][parameter[7:]] = value
        else:
            result[parameter] = value

    for parameter in ('intensity', 'wet'):
        value = result[parameter]
        for lane in _lanes(spec, parameter):
            value = evaluate_lane(lane, time, value)
        result[parameter] = _bounded(spec, parameter, event_value(events, parameter, time, value))
        modulate(parameter)
    for mapping in spec['macro']:
        key = mapping['parameter']
        if key in spec['params']:
            ratio = curve_at(mapping['curve'], _clamp(result['intensity'], 0, 1))
            result['params'][key] = _bounded(spec, 'params.' + key, mapping['min'] + (mapping['max'] - mapping['min']) * ratio)
    for key in spec['order']:
        path = 'params.' + key
        value = result['params'][key]
        for lane in _lanes(spec, path):
            value = evaluate_lane(lane, time, value)
        result['params'][key] = _bounded(spec, path, event_value(events, path, time, value))
        modulate(path)
    # a look-ahead buffer of finite depth can only serve a parameter up to its latency (latency.mjs, LIMITED)
    for key, limit in (spec.get('limits') or {}).items():
        if key in result['params']:
            result['params'][key] = min(result['params'][key], limit)
    result['solo'] = bool(event_value(events, 'solo', time, base['solo']))
    result['bypass'] = bool(event_value(events, 'bypass', time, base['bypass']))
    return result


def _js_round_int(value):
    return int(_round(value))


def reverse_source_time(t, span, pivot, speed, mode, duration):
    """audio-engine.mjs reverseSourceTime for the picture (no per-sample offset): the source time the interval playing backward
    reads at output time `t`; route mode (1) reads the running time."""
    if _js_round_int(mode) == 1:
        return t
    span, speed = max(1e-5, span), max(1e-5, speed)
    start = pivot + math.floor((t - pivot) / span) * span
    left, right = max(0.0, start), min(duration, start + span)
    if not right > left:
        return t
    return right - ((t - left) * speed) % (right - left) - 1e-7


def stutter_read(t, capture, window, rate, gap, repeats):
    """audio-engine.mjs stutterSourceTime, with the gap named: ('live', t) outside the stutter, ('gap', None) between repeats,
    ('repeat', source time) while a repeat plays."""
    window, rate, gap = max(1e-5, window), max(1e-5, rate), max(0.0, gap)
    repeats = max(1, _js_round_int(repeats))
    period = window / rate + gap
    elapsed = t - capture
    if elapsed < 0 or elapsed >= repeats * period:
        return ('live', t)
    phase = elapsed % period
    return ('gap', None) if phase >= window / rate else ('repeat', capture + phase * rate)


def derived_rows(node_type, block, params, clock):
    """The extra `resolved` rows of a processor behind a look-ahead buffer, from its resolved parameters at its own clock.
    `depth` is how many seconds behind the live edge of its input (which runs `lookahead` seconds ahead of the clock) the
    frame to show was cached; it is never negative (a read the buffer cannot cover shows the newest frame).
    Reverse: depth, shift (route mode: the picture's slide, a fraction of the width). Stutter: pick (0 running picture, 1 repeat,
    2 gap), depth, record (1 while the repeat cache takes frames)."""
    lookahead = block.get('lookahead', 0.0)
    live_edge = clock + lookahead
    if node_type == 'reverse':
        if _js_round_int(params['mode']) == 1:
            span = params['span']
            fraction = ((clock * params['speed']) % max(0.15, span)) / span
            return [['depth', lookahead], ['shift', (1 - 2 * fraction) * 0.75]]
        source = reverse_source_time(clock, params['span'], params['pivot'], params['speed'], params['mode'], block['duration'])
        return [['depth', max(0.0, live_edge - source)], ['shift', 0.0]]
    if node_type == 'stutter':
        kind, source = stutter_read(clock, params['capture'], params['window'], params['rate'], params['gap'], params['repeats'])
        window = max(1e-5, params['window'])
        period = window / max(1e-5, params['rate']) + max(0.0, params['gap'])
        run = max(1, _js_round_int(params['repeats'])) * period
        # the cache stops once the captured window has arrived and stays still while any repeat can still read it
        frozen = params['capture'] + window <= live_edge < params['capture'] + run + lookahead
        newest = params['capture'] + window if frozen else live_edge   # the newest source time in the repeat cache
        pick = {'live': 0, 'repeat': 1, 'gap': 2}[kind]
        depth = lookahead if kind == 'live' else (0.0 if source is None else max(0.0, newest - source))
        return [['pick', pick], ['depth', depth], ['record', 0 if frozen else 1]]
    return []


def resolve_rack(contract, bases, time, controls=None):
    """Resolve every processor of the rack and apply the rack-scoped Solo: when any processor is soloed, the others
    are inactive and therefore bypassed. `bases` and `controls` are keyed by processor id."""
    controls = controls or {}
    if not _finite(time):
        raise ValueError('resolve time must be finite')
    # a processor behind a look-ahead buffer evaluates at its own clock: the rack time minus the lag of what it emits
    clocks = {node['id']: max(0.0, time - node['resolve'].get('lag', 0.0)) for node in contract['nodes']}
    partial = {node['id']: resolve_node(node['resolve'], bases[node['id']], clocks[node['id']], controls.get(node['id'], 0.0)) for node in contract['nodes']}
    out = {}
    for node in contract['nodes']:
        item = partial[node['id']]
        scope = node['resolve'].get('scope') or {}
        clock = clocks[node['id']]
        # Solo is rack-scoped at the instant being rendered: any processor soloed at this processor's own clock (behind a look-ahead buffer
        # the clocks differ), as the workbench's evaluatedNode asks
        solo_exists = any(bool(event_value(other['resolve']['events'], 'solo', clock, bases[other['id']]['solo'])) for other in contract['nodes'])
        outside = scope.get('type') == 'clip' and (clock < scope['start'] or clock >= scope['end'])
        active = not outside and (not solo_exists or item['solo'])
        out[node['id']] = {**item, 'active': active, 'bypass': item['bypass'] or not active, 'clock': clock}
        if node['resolve'].get('lag', 0.0) > 0:
            out[node['id']]['derived'] = [['clock', clock]] + (derived_rows(node['type'], node['resolve'], item['params'], clock) if node['resolve'].get('lookahead', 0.0) > 0 else [])
    return out


def has_epoch(spec):
    """A processor with a Reset time parameter (Feedback) also resolves an `epoch` row."""
    return 'reset' in spec['order']


def resolved_rows(spec, resolved, epoch=0.0):
    """The rows written into a processor's `resolved` table, in a fixed order: name, value. A processor with a Reset time
    parameter ends with an `epoch` row (EpochLatch)."""
    rows = [['intensity', resolved['intensity']], ['wet', resolved['wet']], ['bypass', 1 if resolved['bypass'] else 0],
            ['solo', 1 if resolved['solo'] else 0], ['active', 1 if resolved['active'] else 0]]
    rows += [['param.' + key, resolved['params'][key]] for key in spec['order']]
    if has_epoch(spec):
        rows.append(['epoch', epoch])
    rows += [list(row) for row in resolved.get('derived', [])]
    return rows


EPOCH_FPS = 12.0   # the workbench's picture bank rate (docs/FX-WORKBENCH.md); the contract carries the generated network's own


def picture_fps(contract):
    """The picture frame rate the feedback epoch is latched on: the contract's `pictureFps`, else the workbench's 12."""
    value = contract.get('pictureFps', EPOCH_FPS)
    return float(value) if _finite(value) and value > 0 else EPOCH_FPS


class EpochLatch:
    """Feedback's reset epoch, as the workbench's VisualEngine.feedbackEpoch keeps it: the picture clock walks frame by frame, and
    each time the resolved Reset time becomes reached (`time >= reset`) after not being reached, the epoch moves to that frame's
    time; it stays there until the next crossing. It is 0 before the first. The epoch cannot be derived from the current Reset
    time alone (an automated Reset that tracks the playhead never crosses), so the latch remembers; a backward seek replays from
    the nearest saved state (every CHECKPOINT frames), so the answer never depends on the order the playhead moved in.

    `at(time, control)` returns the epoch at `time`. The sidechain level each frame was judged on is recorded the first time that
    frame is walked and never changed afterwards: a frame that is the one asked about takes the level passed in, a frame walked on the
    way there takes the level of the frame before it (the level held until the next observation), and a replay after a backward seek
    uses exactly the recorded levels, so a sidechain-modulated Reset time gives the same epoch however the playhead moved, as the
    workbench's per-frame history does. The first frame walked takes the level passed in."""
    CHECKPOINT = 64    # frames between saved states; a backward seek restarts from the nearest one at or before its frame

    def __init__(self, spec, base, fps=EPOCH_FPS):
        self.spec, self.base, self.fps = spec, base, fps
        self.levels = {}       # frame -> the sidechain level that frame was judged on
        self.checkpoints = {}  # frame (a multiple of CHECKPOINT, or -1) -> (active, epoch) after that frame was walked
        self._restart()

    def _restart(self, frame=-1):
        """Return to the state after `frame` (-1: before the first frame). Only a frame whose state was saved can be returned to."""
        self.frame = frame
        self.active, self.epoch = self.checkpoints.get(frame, (False, 0.0))

    def _step(self, frame, control):
        time = frame / self.fps
        reset = resolve_node(self.spec, self.base, time, control)['params']['reset']
        active = time + 1e-9 >= reset
        if active and not self.active:
            self.epoch = time
        self.active = active

    def at(self, time, control=0.0):
        if not _finite(time):
            raise ValueError('epoch time must be finite')
        frame = int(math.floor(max(0.0, time) * self.fps + 1e-6))
        if frame < self.frame:
            # a state is saved every CHECKPOINT frames as the walk passes, so the replay starts there, not at frame 0: scrubbing
            # backward costs at most CHECKPOINT steps per seek instead of the whole history
            self._restart(max((c for c in self.checkpoints if c <= frame), default=-1))
        while self.frame < frame:
            self.frame += 1
            if self.frame not in self.levels:
                self.levels[self.frame] = control if self.frame == frame or self.frame - 1 not in self.levels else self.levels[self.frame - 1]
            self._step(self.frame, self.levels[self.frame])
            if self.frame % self.CHECKPOINT == 0:
                self.checkpoints[self.frame] = (self.active, self.epoch)
        return self.epoch


def base_values(node, read):
    """Base values of one processor from its custom parameters. `read(par)` returns a parameter's current value."""
    base = {'intensity': 0.0, 'wet': 0.0, 'params': {}, 'bypass': False, 'solo': False}
    for control in node['controls']:
        value = read(control['par'])
        path = control['path']
        if path in ('bypass', 'solo'):
            base[path] = bool(value)
        elif path in ('intensity', 'wet'):
            base[path] = float(value)
        else:
            base['params'][path[7:]] = float(value)
    return base


def release_rules(spec, path):
    """Drop the rules that govern one control, as the workbench does when that control is edited by hand: its
    events, its automation lane and (for a processor parameter) its macro mapping. Returns True when anything went."""
    before = (len(spec['events']), len(spec['automation']), len(spec['macro']))
    spec['events'] = [e for e in spec['events'] if e['parameter'] != path]
    spec['automation'] = [lane for lane in spec['automation'] if lane['parameter'] != path]
    if path.startswith('params.'):
        spec['macro'] = [m for m in spec['macro'] if m['parameter'] != path[7:]]
    return before != (len(spec['events']), len(spec['automation']), len(spec['macro']))


EVENT_TIME_DECIMALS = 4
LANE_INTERPOLATIONS = ('linear', 'step')
MACRO_CURVES = ('linear', 'ease-in', 'ease-out', 'smoothstep')


def apply_rule(spec, kind, path, payload):
    """Mirror one rule change the workbench reported (web/fx/osc-control.mjs, OscFeedback) onto a node's `resolve` block.

    kind 'released': drop the control's events, lane and macro (payload ignored).
    kind 'event':    payload (time, value): insert the event, or replace the value of the event at that time; a later
                     entry wins a tie, as in the workbench.
    kind 'lane':     payload {interpolation, keyframes}: replace the control's lane.
    kind 'macro':    payload {min, max, curve}: replace the processor parameter's mapping.
    Returns True when the block changed; a malformed payload changes nothing and returns False."""
    if kind == 'released':
        return release_rules(spec, path)
    if kind == 'event':
        if not isinstance(payload, (list, tuple)) or len(payload) != 2 or not all(_finite(v) for v in payload):
            return False
        time, value = round(float(payload[0]), EVENT_TIME_DECIMALS), payload[1]
        events = spec['events']
        for event in events:
            if event['parameter'] == path and round(event['time'], EVENT_TIME_DECIMALS) == time:
                event['value'] = value
                return True
        index = len(events)
        while index > 0 and events[index - 1]['time'] > time:
            index -= 1
        events.insert(index, {'time': time, 'parameter': path, 'value': value})
        return True
    if kind == 'lane':
        if not isinstance(payload, dict):
            return False
        frames = payload.get('keyframes')
        if payload.get('interpolation') not in LANE_INTERPOLATIONS or not isinstance(frames, list) or not frames:
            return False
        if not all(isinstance(f, dict) and _finite(f.get('time')) and _finite(f.get('value')) for f in frames):
            return False
        spec['automation'] = [lane for lane in spec['automation'] if lane['parameter'] != path]
        spec['automation'].append({'parameter': path, 'interpolation': payload['interpolation'],
                                   'keyframes': [{'time': f['time'], 'value': f['value']} for f in frames]})
        return True
    if kind == 'macro':
        if not path.startswith('params.') or not isinstance(payload, dict) or payload.get('curve') not in MACRO_CURVES:
            return False
        if not _finite(payload.get('min')) or not _finite(payload.get('max')):
            return False
        key = path[7:]
        spec['macro'] = [m for m in spec['macro'] if m['parameter'] != key]
        spec['macro'].append({'parameter': key, 'min': payload['min'], 'max': payload['max'], 'curve': payload['curve']})
        return True
    return False


# --- TouchDesigner glue ----------------------------------------------------------------------------
_LAST = {}
_RULES = {}   # rack path -> {processor id: the node's resolve block, edited live}
_LATCH = {}   # (rack path, comp) -> (base values, EpochLatch)


def _contract_rules(rack):
    contract = json.loads(rack.op('td_contract').text)
    rules = _RULES.setdefault(rack.path, {})
    for node in contract['nodes']:
        if node['id'] not in rules:
            rules[node['id']] = json.loads(json.dumps(node['resolve']))
        node['resolve'] = rules[node['id']]
    return contract


def apply(rack, comp, kind, path, payload):
    """The workbench reported a rule change for one control of one processor (OSC, `td_osc`): mirror it. Returns True
    when the processor's rules changed."""
    contract = _contract_rules(rack)
    for node in contract['nodes']:
        if node['comp'] == comp:
            changed = apply_rule(node['resolve'], kind, path, payload)
            if changed:
                _LAST.pop((rack.path, comp), None)
                _LATCH.pop((rack.path, comp), None)   # the workbench rebuilds its feedback history when the patch changes
            return changed
    return False


def tick(rack):
    """Resolve the rack at its playhead and refresh every processor's `resolved` table that changed."""
    contract = _contract_rules(rack)
    time = float(rack.par.Time.eval())
    bases, controls = {}, {}
    for node in contract['nodes']:
        comp = rack.op(node['comp'])
        bases[node['id']] = base_values(node, lambda par, comp=comp: getattr(comp.par, par).eval())
        channel = comp.op('in_control')
        controls[node['id']] = float(channel[0].eval()) if channel is not None and channel.numChans else 0.0
    resolved = resolve_rack(contract, bases, time, controls)
    for node in contract['nodes']:
        key = (rack.path, node['comp'])
        epoch = 0.0
        if has_epoch(node['resolve']):
            base = bases[node['id']]
            held = _LATCH.get(key)
            if held is None or held[0] != base:
                held = _LATCH[key] = (json.loads(json.dumps(base)), EpochLatch(node['resolve'], base, picture_fps(contract)))
            epoch = held[1].at(resolved[node['id']]['clock'], controls[node['id']])
        rows = resolved_rows(node['resolve'], resolved[node['id']], epoch)
        if _LAST.get(key) == rows:
            continue
        _LAST[key] = rows
        table = rack.op(node['comp']).op('resolved')
        table.clear()
        for row in rows:
            table.appendRow(row)
    return resolved


def reset():
    _LAST.clear()
    _RULES.clear()
    _LATCH.clear()
