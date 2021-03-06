import os
import sys
import argparse
import logging

import service
import toolbox
import riscv.constants
import riscv.decode

def remaining_buffer_availability(): return state.get('config').get('buffer_capacity') - len(state.get('buffer'))
def hazard(p, c):
    return 'rd' in p.keys() and (('rs1' in c.keys() and p.get('rd') == c.get('rs1')) or ('rs2' in c.keys() and p.get('rd') == c.get('rs2')))
def do_tick(service, state, results, events):
    for _reg in map(lambda y: y.get('register'), filter(lambda x: x.get('register'), results)):
        if '%pc' != _reg.get('name'): continue
        _pc = _reg.get('data')
        state.get('buffer').clear()
        state.update({'drop_until': _pc})
        service.tx({'info': '_pc                   : {}'.format(_pc)})
        service.tx({'info': 'state.get(pc)         : {}'.format(state.get('%pc'))})
        service.tx({'info': 'state.get(drop_until) : {}'.format(state.get('drop_until'))})
        state.update({'reset_buffer_available': True})
    for _flush, _retire in map(lambda y: (y.get('flush'), y.get('retire')), filter(lambda x: x.get('flush') or x.get('retire'), results)):
        if _flush: service.tx({'info': '_flush : {}'.format(_flush)})
        if _retire: service.tx({'info': '_retire : {}'.format(_retire)})
        _commit = (_flush if _flush else _retire)
        assert state.get('issued')[0].get('iid') == _commit.get('iid')
        state.get('issued').pop(0)
    for _dec in map(lambda y: y.get('decode'), filter(lambda x: x.get('decode'), events)):
        state.update({'bytes_fetched' : state.get('bytes_fetched') + _dec.get('size')})
        service.tx({'info': '_dec                  : {}'.format(_dec)})
        if state.get('drop_until'):
            if _dec.get('addr') != state.get('drop_until'): continue
            state.update({'drop_until': None})
            state.update({'%pc': _dec.get('addr')})
        state.get('buffer').extend(_dec.get('data'))
        service.tx({'info': 'state.get(drop_until) : {}'.format(state.get('drop_until'))})
    if state.get('bytes_fetched') == state.get('config').get('buffer_capacity') and len(state.get('buffer')) <= (state.get('config').get('buffer_capacity') >> 1):
        state.update({'reset_buffer_available': True})
    service.tx({'info': 'state.bytes_fetched : {}'.format(state.get('bytes_fetched'))})
    service.tx({'info': 'state.issued        : {}'.format(state.get('issued'))})
    service.tx({'info': 'state.buffer        : {}'.format(state.get('buffer'))})
    for _insn in riscv.decode.do_decode(state.get('buffer'), state.get('max_instructions_to_decode')):
        toolbox.report_stats(service, state, 'histo', 'decoded.insn', _insn.get('cmd'))
        if any(map(lambda x: hazard(x, _insn), state.get('issued'))): break
        if _insn.get('rs1'): service.tx({'event': {
            'arrival': 1 + state.get('cycle'),
            'register': {
                'cmd': 'get',
                'name': _insn.get('rs1'),
            }
        }})
        if _insn.get('rs2'): service.tx({'event': {
            'arrival': 1 + state.get('cycle'),
            'register': {
                'cmd': 'get',
                'name': _insn.get('rs2'),
            }
        }})
        _insn = {
            **_insn,
            **{'iid': state.get('iid')},
            **{'%pc': state.get('%pc')},
        }
        state.update({'iid': 1 + state.get('iid')})
        state.update({'%pc': riscv.constants.integer_to_list_of_bytes(_insn.get('size') + int.from_bytes(state.get('%pc'), 'little'), 64, 'little')})
        service.tx({'event': {
            'arrival': 2 + state.get('cycle'),
            'alu': {
                'insn': _insn,
            },
        }})
        state.get('issued').append(_insn)
        for _ in range(_insn.get('size')): state.get('buffer').pop(0)
        toolbox.report_stats(service, state, 'histo', 'issued.insn', _insn.get('cmd'))
    if state.get('reset_buffer_available'):
        service.tx({'result': {
            'arrival': 1 + state.get('cycle'),
            'decode.buffer_status': {
                'available': remaining_buffer_availability(),
                'cycle': state.get('cycle'),
            },
        }})
        state.update({'bytes_fetched': max(0, state.get('bytes_fetched') - remaining_buffer_availability())})
        state.update({'reset_buffer_available': False})

if '__main__' == __name__:
    parser = argparse.ArgumentParser(description='??Service-SIMulator: Instruction Decode')
    parser.add_argument('--debug', '-D', dest='debug', action='store_true', help='output debug messages')
    parser.add_argument('--quiet', '-Q', dest='quiet', action='store_true', help='suppress status messages')
    parser.add_argument('--log', type=str, dest='log', default='/tmp', help='logging output directory (absolute path!)')
    parser.add_argument('launcher', help='host:port of ??Service-SIMulator launcher')
    args = parser.parse_args()
    logging.basicConfig(
        filename=os.path.join(args.log, '{}.log'.format(os.path.basename(__file__))),
        format='%(message)s',
        level=(logging.DEBUG if args.debug else logging.INFO),
    )
    logging.debug('args : {}'.format(args))
    if not args.quiet: print('Starting {}...'.format(sys.argv[0]))
    _launcher = {x:y for x, y in zip(['host', 'port'], args.launcher.split(':'))}
    _launcher['port'] = int(_launcher['port'])
    logging.debug('_launcher : {}'.format(_launcher))
    state = {
        'service': 'decode',
        'cycle': 0,
        'active': True,
        'running': False,
        '%pc': None,
        'ack': True,
        'buffer': [],
        'bytes_fetched': 0,
        'reset_buffer_available': False,
        'drop_until': None,
        'issued': [],
        'iid': 0,
        'max_instructions_to_decode': 1, # HACK: hard-coded max-instructions-to-decode of 1
        'config': {
            'buffer_capacity': 16,
        },
    }
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
                _service.tx({'result': {
                    'arrival': 2 + state.get('cycle'), # current-cycle + 2 b/c when this executes cycle is 0; +1 would double-count cycle 1
                    'decode.buffer_status': {
                        'available': state.get('config').get('buffer_capacity'),
                        'cycle': state.get('cycle'),
                    },
                }})
            elif 'config' == k:
                logging.debug('config : {}'.format(v))
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
                state.update({'%pc': _pc})
        if state.get('ack') and state.get('running'): _service.tx({'ack': {'cycle': state.get('cycle')}})
    if not args.quiet: print('Shutting down {}...'.format(sys.argv[0]))