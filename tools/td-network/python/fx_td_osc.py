"""TouchDesigner side of the FX rack OSC contract (docs/FX-TOUCHDESIGNER.md).

This module is the text of a Text DAT inside the generated rack component, and it also runs under plain
CPython: it never imports `td`. Everything it needs from TouchDesigner goes through a backend with three
methods, so the same code is exercised by the repository tests against a fake network.

    backend.get(comp, par)            current value of a custom parameter ('' = the rack component)
    backend.set(comp, par, value)     write a custom parameter
    backend.send(address, args)       send one OSC message to the bridge

The contract (`td_contract` DAT) is generated from the patch and lists every address with its own range,
so this module does no address parsing: an address either is in the contract or it is ignored.

Direction. The rack reports its state to TouchDesigner on exactly the addresses TouchDesigner uses to
control the rack, so one table serves both ways. `receive()` applies a rack report to the parameters.
`par_changed()` turns an edit made in TouchDesigner into the control message. A value that only repeats
what was last exchanged on that address (within the contract's tolerance) is an echo and is not sent.
"""
import json
import math

NORM_DECIMALS = 4


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _clamp(value, low, high):
    return min(high, max(low, value))


def _round_half_up(value, places):
    scale = 10 ** places
    return math.floor(value * scale + 0.5) / scale


def value_to_norm(control, value):
    span = control['max'] - control['min']
    return _clamp((value - control['min']) / span, 0.0, 1.0) if span > 0 else 0.0


def norm_to_value(control, norm):
    """Scale a 0..1 position onto the control's range and step (same arithmetic as the workbench)."""
    position = _clamp(norm if _number(norm) else 0.0, 0.0, 1.0)
    value = control['min'] + position * (control['max'] - control['min'])
    step = control['step']
    if step > 0:
        text = repr(float(step)) if not float(step).is_integer() else str(int(step))
        decimals = len(text.split('.')[1]) if '.' in text else 0
        value = control['min'] + math.floor((value - control['min']) / step + 0.5) * step
        value = round(value, min(10, decimals + 2))
    value = _clamp(value, control['min'], control['max'])
    return int(round(value)) if control.get('integer') else value


class RackLink:
    def __init__(self, contract, backend):
        if contract.get('schema') != 'amps.td-osc-contract.v1':
            raise ValueError('unsupported contract schema %r' % (contract.get('schema'),))
        self.contract = contract
        self.backend = backend
        self.tolerance = contract['echoTolerance']
        self.last = {}          # address -> last value exchanged (normalised float, 0/1, or string)
        self.mismatches = []
        self.count = None
        self.by_address = {}    # control address -> (node, control)
        self.by_par = {}        # (comp, par) -> (node, control)
        self.types = {}         # type address -> node
        for node in contract['nodes']:
            self.types[node['typeAddress']] = node
            for control in node['controls']:
                self.by_address[control['address']] = (node, control)
                self.by_par[(node['comp'], control['par'])] = (node, control)
        rack = contract['rack']
        self.transport_by_address = {item['address']: item for item in rack['transport']}
        self.transport_by_par = {item['par']: item for item in rack['transport']}
        self.pulses = {item['par']: item for item in rack['pulses']}

    # rack -> TouchDesigner -------------------------------------------------------------------------
    def receive(self, address, args):
        """Apply one OSC message from the rack. Returns True when it was understood."""
        if not isinstance(address, str) or not isinstance(args, (list, tuple)):
            return False
        rack = self.contract['rack']
        entry = self.by_address.get(address)
        if entry:
            node, control = entry
            if not args:
                return False
            if control['kind'] == 'toggle':
                if not _number(args[0]) and not isinstance(args[0], bool):
                    return False
                on = bool(args[0]) if isinstance(args[0], bool) else args[0] >= 0.5
                self.last[address] = 1 if on else 0
                self.backend.set(node['comp'], control['par'], 1 if on else 0)
            else:
                if not _number(args[0]):
                    return False
                norm = _clamp(args[0], 0.0, 1.0)
                self.last[address] = norm
                self.backend.set(node['comp'], control['par'], norm_to_value(control, norm))
            return True
        node = self.types.get(address)
        if node:
            if args and args[0] != node['type']:
                self.mismatches.append('%s is %s in the rack, %s in this network' % (node['id'], args[0], node['type']))
                self._status()
            return True
        if address == rack['count']['address']:
            if not args or not _number(args[0]):
                return False
            self.count = int(args[0])
            self.backend.set('', rack['count']['par'], self.count)
            self._status()
            return True
        if address == rack['selected']['address']:
            if not args or not isinstance(args[0], str):
                return False
            self.last[address] = args[0]
            self.backend.set('', rack['selected']['par'], args[0])
            return True
        if address == rack['selectedType']['address']:
            return bool(args) and isinstance(args[0], str)
        if address == rack['time']['address']:
            if not args or not _number(args[0]):
                return False
            self.backend.set('', rack['time']['par'], float(args[0]))
            return True
        item = self.transport_by_address.get(address)
        if item:
            if not args or not _number(args[0]):
                return False
            on = 1 if args[0] >= 0.5 else 0
            self.last[address] = on
            self.backend.set('', item['par'], on)
            return True
        return False

    def _status(self):
        expected = len(self.contract['nodes'])
        if self.mismatches:
            text = 'mismatch: ' + '; '.join(self.mismatches[:3])
        elif self.count is None:
            text = 'waiting for the rack'
        elif self.count != expected:
            text = 'mismatch: rack has %d processors, this network has %d' % (self.count, expected)
        else:
            text = 'linked: %d processors' % self.count
        self.backend.set('', self.contract['rack']['status']['par'], text)

    # TouchDesigner -> rack -------------------------------------------------------------------------
    def par_changed(self, comp, par, value):
        """A parameter was edited in TouchDesigner. Returns the message sent, or None."""
        entry = self.by_par.get((comp, par))
        if entry:
            node, control = entry
            if control['kind'] == 'toggle':
                args = [1 if value else 0]
                if self.last.get(control['address']) == args[0]:
                    return None
            else:
                if not _number(value):
                    return None
                norm = _round_half_up(value_to_norm(control, value), NORM_DECIMALS)
                previous = self.last.get(control['address'])
                if previous is not None and abs(previous - norm) <= self.tolerance:
                    return None
                args = [norm]
            self.last[control['address']] = args[0]
            self.backend.send(control['address'], args)
            return (control['address'], args)
        if comp != '':
            return None
        rack = self.contract['rack']
        if par == rack['selected']['par']:
            if not isinstance(value, str) or not value or self.last.get(rack['selected']['address']) == value:
                return None
            self.last[rack['selected']['address']] = value
            self.backend.send(rack['selected']['address'], [value])
            return (rack['selected']['address'], [value])
        item = self.transport_by_par.get(par)
        if item:
            args = [1 if value else 0]
            if self.last.get(item['address']) == args[0]:
                return None
            self.last[item['address']] = args[0]
            self.backend.send(item['address'], args)
            return (item['address'], args)
        return None

    def pulse(self, comp, par):
        item = self.pulses.get(par) if comp == '' else None
        if not item:
            return None
        if item['address'] == self.contract['rack']['sync']['address']:
            self.last.clear()   # the rack is about to resend everything; none of it is an echo
        self.backend.send(item['address'], [])
        return (item['address'], [])


# --- TouchDesigner glue (used inside the generated component) -------------------------------------
class TDBackend:
    def __init__(self, rack):
        self.rack = rack

    def _par(self, comp, par):
        owner = self.rack.op(comp) if comp else self.rack
        return getattr(owner.par, par)

    def get(self, comp, par):
        return self._par(comp, par).eval()

    def set(self, comp, par, value):
        target = self._par(comp, par)
        if target.eval() != value:
            target.val = value

    def send(self, address, args):
        self.rack.op('osc_out').sendOSC(address, list(args))


_LINKS = {}


def link(rack):
    """The RackLink for a rack component, created on first use from its `td_contract` DAT."""
    key = rack.path
    if key not in _LINKS:
        _LINKS[key] = RackLink(json.loads(rack.op('td_contract').text), TDBackend(rack))
    return _LINKS[key]


def reset(rack=None):
    """Forget what was exchanged (and, with no argument, every rack): call after editing `td_contract`."""
    if rack is None:
        _LINKS.clear()
    else:
        _LINKS.pop(rack.path, None)


def owner_name(rack, par):
    return '' if par.owner.path == rack.path else par.owner.name
