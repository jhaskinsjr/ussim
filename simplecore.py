import sys
import argparse

import service

def do_tick(service, state, cycle, results, events):
    [rs for rs in results]
    [ev for ev in events]
    for pc in filter(lambda x: x, map(lambda y: y.get('%pc'), results)):
        service.tx({
            'mem': {
                'cmd': 'peek',
                'addr': pc,
                'size': 4,
            },
        })
        service.tx({
            'register': {
                'cmd': 'set',
                'name': '%pc',
                'data': 4 + pc,
            }
        })
        if pc not in state.get('requested_pc'): state.get('requested_pc').append(pc)
        service.tx({'info': state.get('requested_pc')})
    for mem in filter(lambda x: x and x.get('addr') in state.get('requested_pc'), map(lambda y: y.get('mem'), results)):
        state.get('requested_pc').remove(mem.get('addr'))
        service.tx({'info': state.get('requested_pc')})
        service.tx({'info': mem})
    service.tx({
        'register': {
            'cmd': 'get',
            'name': '%pc',
        },
    })
    return cycle

if '__main__' == __name__:
    parser = argparse.ArgumentParser(description='μService-SIMulator: Watchdog Timer')
    parser.add_argument('--debug', '-D', dest='debug', action='store_true', help='print debug messages')
    parser.add_argument('--quiet', '-Q', dest='quiet', action='store_true', help='suppress status messages')
    parser.add_argument('launcher', help='host:port of μService-SIMulator launcher')
    args = parser.parse_args()
    if args.debug: print('args : {}'.format(args))
    if not args.quiet: print('Starting {}...'.format(sys.argv[0]))
    _launcher = {x:y for x, y in zip(['host', 'port'], args.launcher.split(':'))}
    _launcher['port'] = int(_launcher['port'])
    if args.debug: print('_launcher : {}'.format(_launcher))
    _service = service.Service('simplecore', _launcher.get('host'), _launcher.get('port'))
    state = {
        'cycle': 0,
        'active': True,
        'running': False,
        'requested_pc': [], # to allow more than one outstanding PC req at a time
        'ack': True,
    }
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
                _cycle = v.get('cycle')
                _results = v.get('results')
                _events = v.get('events')
                state.update({'cycle': do_tick(_service, state, _cycle, _results, _events)})
        if state.get('ack') and state.get('running'): _service.tx({'ack': {'cycle': state.get('cycle')}})
    if not args.quiet: print('Shutting down {}...'.format(sys.argv[0]))