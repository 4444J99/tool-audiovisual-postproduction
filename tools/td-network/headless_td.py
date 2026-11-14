"""A TouchDesigner stand-in that runs the generated OSC module (python/fx_td_osc.py) for real.

    python3 tools/td-network/headless_td.py network.json --td-port 7000 --osc-port 7001

It listens where TouchDesigner's OSC In DAT would, hands every message to the same `RackLink` the generated
network uses, keeps the custom parameters in a dictionary, and sends what the module sends to the bridge's
OSC port. Control is by JSON lines on stdin; each command is answered by one JSON line on stdout:

    {"cmd": "edit",  "comp": "fx_inbox", "par": "Intensity", "value": 0.5}     -> {"sent": [address, args] | null}
    {"cmd": "pulse", "comp": "", "par": "Next"}                                -> {"sent": ...}
    {"cmd": "params"}                                                          -> {"params": {"comp|par": value}}
    {"cmd": "quit"}

Only the OSC wire format is implemented here (int, float, string, true, false, nil; bundles are unpacked),
independently of the Node codec the bridge uses, so a test that runs both checks the two against each other.
"""
import argparse
import importlib.util
import json
import os
import select
import socket
import struct
from pathlib import Path


def load_module():
    path = Path(__file__).parent / 'python' / 'fx_td_osc.py'
    spec = importlib.util.spec_from_file_location('fx_td_osc', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pad(raw):
    return raw + b'\0' * (4 - len(raw) % 4)


def encode(address, args):
    tags, body = ',', b''
    for value in args:
        if isinstance(value, bool):
            tags += 'T' if value else 'F'
        elif isinstance(value, int):
            tags += 'i'; body += struct.pack('>i', value)
        elif isinstance(value, float):
            tags += 'f'; body += struct.pack('>f', value)
        elif isinstance(value, str):
            tags += 's'; body += _pad(value.encode('utf-8'))
        else:
            raise ValueError('unsupported OSC argument %r' % (value,))
    return _pad(address.encode('utf-8')) + _pad(tags.encode('ascii')) + body


def _string(data, at):
    end = data.index(b'\0', at)
    return data[at:end].decode('utf-8'), (end + 4) & ~3


def decode(data):
    """Yield (address, args) for a message or every message inside a bundle."""
    if data.startswith(b'#bundle\0'):
        at = 16
        while at + 4 <= len(data):
            size = struct.unpack('>i', data[at:at + 4])[0]
            yield from decode(data[at + 4:at + 4 + size])
            at += 4 + size
        return
    address, at = _string(data, 0)
    tags, at = _string(data, at)
    args = []
    for tag in tags[1:]:
        if tag == 'i':
            args.append(struct.unpack('>i', data[at:at + 4])[0]); at += 4
        elif tag == 'f':
            args.append(struct.unpack('>f', data[at:at + 4])[0]); at += 4
        elif tag == 's':
            text, at = _string(data, at); args.append(text)
        elif tag in 'TF':
            args.append(tag == 'T')
        elif tag == 'N':
            args.append(None)
        else:
            return
    yield address, args


class Backend:
    def __init__(self, sock, target):
        self.params, self.sock, self.target = {}, sock, target

    def get(self, comp, par):
        return self.params.get('%s|%s' % (comp, par))

    def set(self, comp, par, value):
        self.params['%s|%s' % (comp, par)] = value

    def send(self, address, args):
        self.sock.sendto(encode(address, args), self.target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('spec')
    parser.add_argument('--td-port', type=int, default=7000)
    parser.add_argument('--osc-port', type=int, default=7001)
    options = parser.parse_args()
    contract = json.loads(Path(options.spec).read_text(encoding='utf-8'))['contract']
    module = load_module()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1', options.td_port))
    backend = Backend(sock, ('127.0.0.1', options.osc_port))
    link = module.RackLink(contract, backend)
    print(json.dumps({'ready': True, 'port': sock.getsockname()[1]}), flush=True)
    pending = b''
    while True:
        ready, _, _ = select.select([sock, 0], [], [])
        if sock in ready:
            data, _ = sock.recvfrom(65536)
            try:
                for address, args in decode(data):
                    link.receive(address, args)
            except (ValueError, struct.error, IndexError):
                pass
        if 0 in ready:
            # Unbuffered reads: a buffered readline() can swallow several commands that select() then never reports.
            chunk = os.read(0, 65536)
            if not chunk:
                return
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                command = json.loads(line)
                if command['cmd'] == 'quit':
                    return
                if command['cmd'] == 'edit':
                    reply = {'sent': link.par_changed(command['comp'], command['par'], command['value'])}
                elif command['cmd'] == 'pulse':
                    reply = {'sent': link.pulse(command['comp'], command['par'])}
                elif command['cmd'] == 'params':
                    reply = {'params': backend.params, 'status': backend.params.get('|' + contract['rack']['status']['par'])}
                else:
                    reply = {'error': 'unknown command'}
                print(json.dumps(reply), flush=True)


if __name__ == '__main__':
    main()
