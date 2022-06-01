import sys
import argparse

import service
import components.simplecache
import riscv.constants

def fetch_block(service, state, jp):
    _blockaddr = state.get('l1ic').blockaddr(jp)
    _blocksize = state.get('l1ic').nbytesperblock
    state.get('pending_fetch').append(_blockaddr)
    service.tx({'event': {
        'arrival': 1 + state.get('cycle'),
        'mem': {
            'cmd': 'peek',
            'addr': _blockaddr,
            'size': _blocksize,
        },
    }})
def do_l1ic(service, state):
    _jp = int.from_bytes(state.get('%jp'), 'little')
    service.tx({'info': '_jp : {}'.format(_jp)})
    if state.get('l1ic').fits(_jp, state.get('fetch_size')):
        _data = state.get('l1ic').peek(_jp, state.get('fetch_size'))
        service.tx({'info': '_data : {}'.format(_data)})
        if not _data:
            if len(state.get('pending_fetch')): return # only 1 pending fetch at a time is primitive, but good enough for now
            fetch_block(service, state, _jp)
            return
    else:
        _size = state.get('fetch_size') >> 1 # Why div-by-2? because RISC-V instructions are always 4B or 2B
        _ante = state.get('l1ic').peek(_jp, _size)
        if not _ante:
            if len(state.get('pending_fetch')): return # only 1 pending fetch at a time is primitive, but good enough for now
            fetch_block(service, state, _jp)
            return
        _post = state.get('l1ic').peek(_jp + _size, _size)
        if not _post:
            if len(state.get('pending_fetch')): return # only 1 pending fetch at a time is primitive, but good enough for now
            fetch_block(service, state, _jp + _size)
            return
        # NOTE: In an L1IC with only a single block, an incoming _post would
        #       always displace _ante, and an incoming _ante would always displace
        #       _post... but an L1IC with only a single block would not be very
        #       useful in practice, so no effort will be made to handle that scenario.
        #       Like: No effort AT ALL.
        _data = _ante + _post
        assert len(_data) == state.get('fetch_size')
    service.tx({'event': {
        'arrival': 1 + state.get('cycle'),
        'decode': {
            'addr': state.get('%jp'),
            'size': state.get('fetch_size'),
            'data': _data,
        },
    }})
    state.update({'%jp': riscv.constants.integer_to_list_of_bytes(4 + _jp, 64, 'little')})
def do_tick(service, state, results, events):
    for _reg in map(lambda y: y.get('register'), filter(lambda x: x.get('register'), results)):
        if '%pc' != _reg.get('name'): continue
        _pc = _reg.get('data')
        if 0 == int.from_bytes(_pc, 'little'):
            service.tx({'info': 'Jump to @0x00000000... graceful shutdown'})
            service.tx({'shutdown': None})
        state.update({'%jp': _pc})
    for _decode_buffer_available in map(lambda y: y.get('decode.buffer_available'), filter(lambda x: x.get('decode.buffer_available'), results)):
        state.update({'decode.buffer_available': _decode_buffer_available})
    for _mem in map(lambda y: y.get('mem'), filter(lambda x: x.get('mem'), results)):
        _addr = _mem.get('addr')
        if _addr not in state.get('pending_fetch'): continue
        service.tx({'info': '_mem : {}'.format(_mem)})
        state.get('pending_fetch').remove(_addr)
        state.get('l1ic').poke(_addr, _mem.get('data'))
    service.tx({'info': 'decode.buffer_available : {}'.format(state.get('decode.buffer_available'))})
    service.tx({'info': 'fetch_size              : {}'.format(state.get('fetch_size'))})
    if state.get('decode.buffer_available') <= state.get('fetch_size'): return
    if state.get('stall_until') > state.get('cycle'): return
    if not state.get('%jp'): return
    do_l1ic(service, state)
    

if '__main__' == __name__:
    parser = argparse.ArgumentParser(description='μService-SIMulator: Simple Core')
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
        'service': 'fetch',
        'cycle': 0,
        'stall_until': 0,
        'l1ic': None,
        'pending_fetch': [],
        'active': True,
        'running': False,
        'decode.buffer_available': 4,
        'fetch_size': 4, # HACK: hard-coded number of bytes to fetch
        '%jp': None, # This is the fetch pointer. Why %jp? Who knows?
        'ack': True,
        'config': {
            'l1ic.nsets': 2**4,
            'l1ic.nways': 2**1,
            'l1ic.nbytesperblock': 2**4,
        },
    }
    state.update({'l1ic': components.simplecache.SimpleCache(
        state.get('config').get('l1ic.nsets'),
        state.get('config').get('l1ic.nways'),
        state.get('config').get('l1ic.nbytesperblock'),
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
            elif 'config' == k:
                print('config : {}'.format(v))
                if state.get('service') != v.get('service'): continue
                _field = v.get('field')
                _val = v.get('val')
                assert _field in state.get('config').keys(), 'No such config field, {}, in service {}!'.format(_field, state.get('service'))
                state.get('config').update({_field: _val})
            elif 'tick' == k:
                state.update({'cycle': v.get('cycle')})
                _results = v.get('results')
                _events = v.get('events')
                do_tick(_service, state, _results, _events)
            elif 'register' == k:
                if not '%pc' == v.get('name'): continue
                if not 'set' == v.get('cmd'): continue
                _pc = v.get('data')
                state.update({'%jp': _pc})
        if state.get('ack') and state.get('running'): _service.tx({'ack': {'cycle': state.get('cycle')}})
    if not args.quiet: print('Shutting down {}...'.format(sys.argv[0]))