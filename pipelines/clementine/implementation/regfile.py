import os
import sys
import argparse
import logging

import service
import toolbox
import riscv.constants

def do_tick(service, state, results, events):
    for ev in filter(lambda x: x, map(lambda y: y.get('register'), events)):
        _cmd = ev.get('cmd')
        _name = ev.get('name')
        _data = ev.get('data')
        if 'set' == _cmd:
            assert _name in state.get('registers').keys()
            assert isinstance(_data, list)
            if 0 != _name:
                state.update({'registers': setregister(state.get('registers'), _name, _data)})
            toolbox.report_stats(service, state, 'histo', 'set.register', _name)
        elif 'get' == _cmd:
            assert _name in state.get('registers').keys()
            service.tx({'result': {
                'arrival': 1 + state.get('cycle'),
                'register': {
                    'name': _name,
                    'data': getregister(state.get('registers'), _name),
                }
            }})
            toolbox.report_stats(service, state, 'histo', 'get.register', _name)
        elif 'snapshot' == _cmd:
            fd = os.open(_name, os.O_RDWR)
            os.lseek(fd, _data, os.SEEK_SET)
            for k in ['%pc'] + sorted(filter(lambda x: not '%pc' == x, state.get('registers').keys()), key=str):
                v = getregister(state.get('registers'), k)
                os.write(fd, bytes(v))
                os.lseek(fd, 8, os.SEEK_CUR)
                service.tx({'info': 'snapshot: {} : {}'.format(k, v)})
            os.fsync(fd)
            os.close(fd)
            toolbox.report_stats(service, state, 'flat', 'snapshot')
        elif 'restore' == _cmd:
            fd = os.open(_name, os.O_RDWR)
            os.lseek(fd, _data, os.SEEK_SET)
            for k in ['%pc'] + sorted(filter(lambda x: not '%pc' == x, state.get('registers').keys()), key=str):
                v = list(os.read(fd, 8))
                os.lseek(fd, 8, os.SEEK_CUR)
                state.update({'registers': setregister(state.get('registers'), k, v)})
                service.tx({'info': 'restore: {} : {}'.format(k, v)})
            os.close(fd)
            toolbox.report_stats(service, state, 'flat', 'restore')
        else:
            logging.fatal('ev   : {}'.format(ev))
            logging.fatal('_cmd : {}'.format(_cmd))
            assert False

def setregister(registers, reg, val):
    return {x: y for x, y in tuple(registers.items()) + ((reg, val),)}
def getregister(registers, reg):
    return registers.get(reg, None)

if '__main__' == __name__:
    parser = argparse.ArgumentParser(description='??Service-SIMulator: Register File')
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
        'service': 'regfile',
        'cycle': 0,
        'active': True,
        'running': False,
        'ack': True,
        'registers': {
            **{'%pc': riscv.constants.integer_to_list_of_bytes(0, 64, 'little')},
            **{x: riscv.constants.integer_to_list_of_bytes(0, 64, 'little') for x in range(32)},
        }
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
            elif 'tick' == k:
                state.update({'cycle': v.get('cycle')})
                _results = v.get('results')
                _events = v.get('events')
                do_tick(_service, state, _results, _events)
            elif 'register' == k:
                _cmd = v.get('cmd')
                _name = v.get('name')
                if 'set' == _cmd:
                    state.update({'registers': setregister(state.get('registers'), _name, v.get('data'))})
                elif 'get' == _cmd:
                    _ret = getregister(state.get('registers'), _name)
                    _service.tx({'result': {'register': _ret}})
        if state.get('ack') and state.get('running'): _service.tx({'ack': {'cycle': state.get('cycle')}})
    if not args.quiet: print('Shutting down {}...'.format(sys.argv[0]))
    for k, v in state.get('registers').items():
        logging.info('register {:2} : {}'.format(k, v))