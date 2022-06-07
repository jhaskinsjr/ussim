import sys
import argparse
import functools
import struct

import service
import toolbox
import components.simplecache
import riscv.execute
import riscv.syscall.linux

def fetch_block(service, state, addr):
    _blockaddr = state.get('l2').blockaddr(addr)
    _blocksize = state.get('l2').nbytesperblock
    state.get('pending_fetch').append(_blockaddr)
    service.tx({'event': {
        'arrival': 1 + state.get('cycle'),
        'mem': {
            'cmd': 'peek',
            'addr': _blockaddr,
            'size': _blocksize,
        },
    }})
    toolbox.report_stats(service, state, 'flat', 'l2.misses')
def do_l2(service, state, addr, size, data=None):
    service.tx({'info': 'addr : {}'.format(addr)})
    if state.get('l2').fits(addr, size):
        _data = state.get('l2').peek(addr, size)
#        service.tx({'info': '_data : {}'.format(_data)})
        if not _data:
            if len(state.get('pending_fetch')): return # only 1 pending fetch at a time is primitive, but good enough for now
            fetch_block(service, state, addr)
            return
    else:
        _blockaddr = state.get('l2').blockaddr(addr)
        _blocksize = state.get('l2').nbytesperblock
        _size = _blockaddr + _blocksize - addr
        _ante = state.get('l2').peek(addr, _size)
        if not _ante:
            if len(state.get('pending_fetch')): return # only 1 pending fetch at a time is primitive, but good enough for now
            fetch_block(service, state, addr)
            return
        _post = state.get('l2').peek(addr + _size, size - _size)
        if not _post:
            if len(state.get('pending_fetch')): return # only 1 pending fetch at a time is primitive, but good enough for now
            fetch_block(service, state, addr + _size)
            return
        # NOTE: In an L1DC with only a single block, an incoming _post would
        #       always displace _ante, and an incoming _ante would always displace
        #       _post... but an L1DC with only a single block would not be very
        #       useful in practice, so no effort will be made to handle that scenario.
        #       Like: No effort AT ALL.
        _data = _ante + _post
        assert len(_data) == size
    if data:
        # POKE
        service.tx({'result': {
            'arrival': state.get('config').get('l2.hitlatency') + state.get('cycle'),
            'mem': {
                'addr': addr,
                'size': size,
            },
        }})
        # TODO: Should _ante and _post be poke()'d into L1DC separately?
        state.get('l2').poke(addr, data)
        # writethrough
        service.tx({'event': {
            'arrival': 1 + state.get('cycle'),
            'mem': {
                'cmd': 'poke',
                'addr': addr,
                'size': len(data),
                'data': data
            }
        }})
    else:
        # PEEK
        service.tx({'result': {
            'arrival': state.get('config').get('l2.hitlatency') + state.get('cycle'), # must not arrive in commit the same cycle as the LOAD instruction
            'mem': {
                'addr': addr,
                'size': size,
                'data': _data,
            },
        }})
    state.get('executing').pop(0)
    if len(state.get('pending_fetch')): state.get('pending_fetch').pop(0)
    toolbox.report_stats(service, state, 'flat', 'l2.accesses')

def do_tick(service, state, results, events):
    for _mem in filter(lambda x: x, map(lambda y: y.get('mem'), results)):
        _addr = _mem.get('addr')
        if _addr == state.get('operands').get('mem'):
            state.get('operands').update({'mem': _mem.get('data')})
        elif _addr in state.get('pending_fetch'):
            service.tx({'info': '_mem : {}'.format(_mem)})
            state.get('l2').poke(_addr, _mem.get('data'))
    for ev in map(lambda y: y.get('l2'), filter(lambda x: x.get('l2'), events)):
        state.get('executing').append(ev)
    if len(state.get('executing')):
        _op = state.get('executing')[0] # forcing single outstanding operation for now
#        _cmd = _op.get('cmd') # NOTE: assumed to be a poke if message contains a payload (i.e., _data != None)
        _addr = _op.get('addr')
        _size = _op.get('size')
        _data = _op.get('data')
        do_l2(service, state, _addr, _size, _data)

if '__main__' == __name__:
    parser = argparse.ArgumentParser(description='μService-SIMulator: Load-Store Unit')
    parser.add_argument('--debug', '-D', dest='debug', action='store_true', help='print debug messages')
    parser.add_argument('--quiet', '-Q', dest='quiet', action='store_true', help='suppress status messages')
    parser.add_argument('launcher', help='host:port of μService-SIMulator launcher')
    args = parser.parse_args()
    if args.debug: print('args : {}'.format(args))
    if not args.quiet: print('Starting {}...'.format(sys.argv[0]))
    _launcher = {x:y for x, y in zip(['host', 'port'], args.launcher.split(':'))}
    _launcher['port'] = int(_launcher['port'])
    if args.debug: print('_launcher : {}'.format(_launcher))
    state = {
        'service': 'lsu',
        'cycle': 0,
        'l2': None,
        'pending_fetch': [],
        'active': True,
        'running': False,
        'ack': True,
        'pending_execute': [],
        'executing': [],
        'operands': {},
        'config': {
            'l2.nsets': 2**5,
            'l2.nways': 2**4,
            'l2.nbytesperblock': 2**4,
            'l2.hitlatency': 5,
        },
    }
    state.update({'l2': components.simplecache.SimpleCache(
        state.get('config').get('l2.nsets'),
        state.get('config').get('l2.nways'),
        state.get('config').get('l2.nbytesperblock'),
    )})
    _service = service.Service(state.get('service'), _launcher.get('host'), _launcher.get('port'))
    while state.get('active'):
        state.update({'ack': True})
        msg = _service.rx()
#        _service.tx({'info': {'msg': msg, 'msg.size()': len(msg)}})
#        print('msg : {}'.format(msg))
        for k, v in msg.items():
            if {'text': 'bye'} == {k: v}:
                state.update({'active': False})
                state.update({'running': False})
            elif {'text': 'run'} == {k: v}:
                state.update({'running': True})
                state.update({'ack': False})
            elif 'tick' == k:
                state.update({'cycle': v.get('cycle')})
                _results = v.get('results')
                _events = v.get('events')
                do_tick(_service, state, _results, _events)
        if state.get('ack') and state.get('running'): _service.tx({'ack': {'cycle': state.get('cycle')}})
    if not args.quiet: print('Shutting down {}...'.format(sys.argv[0]))