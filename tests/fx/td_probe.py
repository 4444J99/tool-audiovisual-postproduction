"""Runs the generated TouchDesigner OSC module against reports and edits supplied by a Node test.

stdin : {"module": path to td_osc.py, "contract": {...}, "reports": [{"address", "args"}], "edits": [{"comp", "par", "value"}]}
stdout: {"params": {"comp|par": value}, "sent": [{"address", "args"}], "status": str}
"""
import importlib.util
import json
import sys

request = json.load(sys.stdin)
spec = importlib.util.spec_from_file_location('td_osc', request['module'])
td_osc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(td_osc)


class Backend:
    def __init__(self):
        self.params, self.sent = {}, []

    def get(self, comp, par):
        return self.params.get('%s|%s' % (comp, par))

    def set(self, comp, par, value):
        self.params['%s|%s' % (comp, par)] = value

    def send(self, address, args):
        self.sent.append({'address': address, 'args': list(args)})


backend = Backend()
link = td_osc.RackLink(request['contract'], backend)
understood = [link.receive(item['address'], item['args']) for item in request['reports']]
backend.sent.clear()
edits = []
for edit in request['edits']:
    sent = link.par_changed(edit['comp'], edit['par'], edit['value'])
    edits.append(None if sent is None else {'address': sent[0], 'args': sent[1]})
json.dump({'params': backend.params, 'understood': understood, 'edits': edits, 'sent': backend.sent,
           'status': backend.params.get('|' + request['contract']['rack']['status']['par'])}, sys.stdout)
