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
    result['solo'] = bool(event_value(events, 'solo', time, base['solo']))
    result['bypass'] = bool(event_value(events, 'bypass', time, base['bypass']))
    return result


def resolve_rack(contract, bases, time, controls=None):
    """Resolve every processor of the rack and apply the rack-scoped Solo: when any processor is soloed, the others
    are inactive and therefore bypassed. `bases` and `controls` are keyed by processor id."""
    controls = controls or {}
    partial = {node['id']: resolve_node(node['resolve'], bases[node['id']], time, controls.get(node['id'], 0.0)) for node in contract['nodes']}
    solo_exists = any(item['solo'] for item in partial.values())
    out = {}
    for node in contract['nodes']:
        item = partial[node['id']]
        scope = node['resolve'].get('scope') or {}
        outside = scope.get('type') == 'clip' and (time < scope['start'] or time >= scope['end'])
        active = not outside and (not solo_exists or item['solo'])
        out[node['id']] = {**item, 'active': active, 'bypass': item['bypass'] or not active}
    return out


def resolved_rows(spec, resolved):
    """The rows written into a processor's `resolved` table, in a fixed order: name, value."""
    rows = [['intensity', resolved['intensity']], ['wet', resolved['wet']], ['bypass', 1 if resolved['bypass'] else 0],
            ['solo', 1 if resolved['solo'] else 0], ['active', 1 if resolved['active'] else 0]]
    rows += [['param.' + key, resolved['params'][key]] for key in spec['order']]
    return rows


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


# --- TouchDesigner glue ----------------------------------------------------------------------------
_LAST = {}
_RULES = {}   # rack path -> {processor id: the node's resolve block, edited live}


def _contract_rules(rack):
    contract = json.loads(rack.op('td_contract').text)
    rules = _RULES.setdefault(rack.path, {})
    for node in contract['nodes']:
        if node['id'] not in rules:
            rules[node['id']] = json.loads(json.dumps(node['resolve']))
        node['resolve'] = rules[node['id']]
    return contract


def release(rack, comp, path):
    """A person edited a control by hand in this network: the workbench drops that control's automation, events and
    macro mapping (web/fx/app.mjs setParameter), so the resolver drops them too. Returns True when a rule went."""
    contract = _contract_rules(rack)
    for node in contract['nodes']:
        if node['comp'] == comp:
            changed = release_rules(node['resolve'], path)
            if changed:
                _LAST.pop((rack.path, comp), None)
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
        rows = resolved_rows(node['resolve'], resolved[node['id']])
        key = (rack.path, node['comp'])
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
