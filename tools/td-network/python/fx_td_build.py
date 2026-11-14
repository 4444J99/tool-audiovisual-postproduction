"""Builds the FX rack network from a generated spec (amps.td-network.v1).

Run inside TouchDesigner (Textport or a Text DAT), or under CPython against any object tree that offers the
same few calls (the repository tests use a fake). The spec decides everything; nothing here knows a processor.

TouchDesigner calls used, all documented Python API:
    COMP.create(typeName, name)        COMP.appendCustomPage(name) and Page.appendFloat/Int/Toggle/Str/File/Pulse
    op.par.<name>.val / .expr / .min / .max / .clampMin / .clampMax / .default / .readOnly
    op.inputConnectors[i].connect(other)     DAT.text = ...       COMP.save(path)

The spec is checked against its own vocabulary (operator types and parameter names) before anything is created,
so a spec this build cannot honour fails cleanly instead of leaving a half-built network.
"""
import json

SCHEMA = 'amps.td-network.v1'
_APPEND = {'Float': 'appendFloat', 'Int': 'appendInt', 'Toggle': 'appendToggle', 'Str': 'appendStr',
           'File': 'appendFile', 'Pulse': 'appendPulse'}


class BuildError(Exception):
    pass


def check_spec(spec):
    """Raise BuildError for anything the builder cannot create. Pure; touches no operators."""
    if spec.get('schema') != SCHEMA:
        raise BuildError('unsupported spec schema %r' % (spec.get('schema'),))
    vocabulary = spec['vocabulary']
    paths = {''}
    for operator in spec['operators']:
        kind = vocabulary.get(operator['type'])
        if kind is None:
            raise BuildError('unknown operator type %s (%s)' % (operator['type'], operator['path']))
        if operator['parent'] not in paths:
            raise BuildError('%s is listed before its parent %r' % (operator['path'], operator['parent']))
        if operator['path'] in paths:
            raise BuildError('duplicate operator path %s' % operator['path'])
        paths.add(operator['path'])
        for name in operator['pars']:
            if name not in kind['pars']:
                raise BuildError('%s: %s has no parameter %s in the vocabulary' % (operator['path'], operator['type'], name))
        siblings = [o['name'] for o in spec['operators'] if o['parent'] == operator['parent'] and o['path'] != operator['path']]
        for name in operator['inputs']:
            if name not in siblings:
                raise BuildError('%s: input %s is not a sibling' % (operator['path'], name))
    for operator in spec['operators']:
        for name, value in operator['pars'].items():
            if isinstance(value, dict) and 'ref' in value and value['ref'] not in paths:
                raise BuildError('%s.%s refers to missing %s' % (operator['path'], name, value['ref']))


def _page(target, page_name, cache):
    if page_name not in cache:
        cache[page_name] = target.appendCustomPage(page_name)
    return cache[page_name]


def _custom_pars(target, definitions):
    pages = {}
    for item in definitions:
        page = _page(target, item['page'], pages)
        method = getattr(page, _APPEND[item['style']])
        created = method(item['name'], label=item['label'])
        par = created[0] if isinstance(created, (tuple, list)) else created
        for key in ('min', 'max', 'clampMin', 'clampMax', 'normMin', 'normMax', 'default'):
            if key in item:
                setattr(par, key, item[key])
        if 'value' in item:
            par.val = item['value']
        if item.get('readOnly'):
            par.readOnly = True


def _set_pars(target, path, pars, root_path):
    for name, value in pars.items():
        par = getattr(target.par, name, None)
        if par is None:
            raise BuildError('%s has no parameter %s in this TouchDesigner build' % (path, name))
        if isinstance(value, dict):
            if 'expr' in value:
                par.expr = value['expr']
            elif 'ref' in value:
                par.val = root_path + '/' + value['ref']
            else:
                raise BuildError('%s.%s: unrecognised value %r' % (path, name, value))
        else:
            par.val = value


def build(parent, spec, save_to=None):
    """Create the rack under `parent` and return it. An existing component of that name is replaced."""
    check_spec(spec)
    root_spec = spec['root']
    existing = parent.op(root_spec['name'])
    if existing is not None:
        existing.destroy()
    rack = parent.create(root_spec['type'], root_spec['name'])
    _custom_pars(rack, root_spec['customPars'])
    made = {'': rack}
    for operator in spec['operators']:
        container = made[operator['parent']]
        node = container.create(operator['type'], operator['name'])
        made[operator['path']] = node
        if operator['customPars']:
            _custom_pars(node, operator['customPars'])
        if 'text' in operator:
            node.text = operator['text']
        if 'table' in operator:
            node.clear()
            for row in operator['table']:
                node.appendRow(row)
    # Parameters and wires are set after every operator exists, so references and sibling order never matter.
    for operator in spec['operators']:
        node = made[operator['path']]
        _set_pars(node, operator['path'], operator['pars'], rack.path)
        container = made[operator['parent']]
        for index, name in enumerate(operator['inputs']):
            node.inputConnectors[index].connect(container.op(name))
    if save_to:
        rack.save(save_to)
    return rack


def run_in_touchdesigner(spec, parent_path='/project1', save_to=None):
    import td  # only available inside TouchDesigner
    rack = build(td.op(parent_path), spec, save_to)
    td.op(rack.path + '/td_osc').module.reset(rack)
    return rack
