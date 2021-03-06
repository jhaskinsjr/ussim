import os
import time
import socket
import json
import argparse
import threading
import subprocess
import itertools
import logging

import elftools.elf.elffile

import service
import riscv.constants

def tx(conns, msg):
    _message = {
        str: lambda : json.dumps({'text': msg}),
        dict: lambda : json.dumps(msg),
    }.get(type(msg), lambda : json.dumps({'error': 'Undeliverable object'}))().encode('ascii')
    assert service.Service.MESSAGE_SIZE >= len(_message), 'Message too big! ({} bytes) -> {}'.format(len(_message), _message)
    _message += (' ' * (service.Service.MESSAGE_SIZE - len(_message))).encode('ascii')
    for c in conns: c.send(_message)
def handler(conn, addr):
    global state
    state.get('lock').acquire()
    state.get('connections').append(conn)
    state.get('lock').release()
    logging.info('handler(): {}:{}'.format(*addr))
    tx([conn], {'ack': 'launcher'})
    while True:
        try:
            msg = conn.recv(service.Service.MESSAGE_SIZE)
            if not len(msg):
                time.sleep(0.01)
                continue
            msg = json.loads(msg.decode('ascii'))
            logging.debug('{}: {}'.format(threading.current_thread().name, msg))
            k, v = (next(iter(msg.items())) if isinstance(msg, dict) else (None, None))
            if {k: v} == {'text': 'bye'}:
                conn.close()
                state.get('lock').acquire()
                state.get('connections').remove(conn)
                state.get('lock').release()
                break
            elif 'shutdown' == k:
                state.get('lock').acquire()
                state.update({'running': False})
                state.get('lock').release()
            elif 'undefined' == k:
#                assert False
                state.get('lock').acquire()
                state.update({'undefined': v})
                state.get('lock').release()
            elif 'committed' == k:
                state.get('lock').acquire()
                state.update({'instructions_committed': 1 + state.get('instructions_committed')})
                state.get('lock').release()
            elif 'name' == k:
                threading.current_thread().name = v
            elif 'info' == k:
                state.get('lock').acquire()
                state.get('info').append('{}.handler(): info : {}'.format(threading.current_thread().name, v))
                state.get('lock').release()
            elif 'ack' == k:
                state.get('lock').acquire()
                state.get('ack').append((threading.current_thread().name, msg))
                logging.debug('{}.handler(): ack: {} ({})'.format(threading.current_thread().name, state.get('ack'), len(state.get('ack'))))
                state.get('lock').release()
            elif 'result' == k:
                _arr = v.pop('arrival')
                assert _arr > state.get('cycle'), 'Attempting to schedule arrival in the past ({} vs. {})!'.format(_arr, state.get('cycle'))
                _res = v
                state.get('lock').acquire()
                _res_evt = state.get('futures').get(_arr, {'results': [], 'events': []})
                _res_evt.get('results').append(_res)
                state.get('futures').update({_arr: _res_evt})
                state.get('lock').release()
            elif 'event' == k:
                _arr = v.pop('arrival')
                assert _arr > state.get('cycle'), 'Attempting to schedule arrival in the past ({} vs. {})!'.format(_arr, state.get('cycle'))
                _evt = v
                state.get('lock').acquire()
                _res_evt = state.get('futures').get(_arr, {'results': [], 'events': []})
                _res_evt.get('events').append(_evt)
                state.get('futures').update({_arr: _res_evt})
                state.get('lock').release()
            else:
                state.get('lock').acquire()
                _running = state.get('running')
                state.get('lock').release()
                logging.debug('{}.handler(): _running : {} ({})'.format(threading.current_thread().name, _running, msg))
                if _running:
                    state.get('lock').acquire()
                    state.get('events').append(msg)
                    state.get('lock').release()
                else:
                    state.get('lock').acquire()
                    tx(filter(lambda c: c != conn, state.get('connections')), msg)
                    state.get('lock').release()
        except Exception as ex:
            logging.fatal('Oopsie! {} ({} ({}:{}))'.format(ex, str(msg), type(msg), len(msg)))
            conn.close()
def acceptor():
    while True:
        _conn, _addr = _s.accept()
        th = threading.Thread(target=handler, args=(_conn, _addr))
        th.start()
def integer(val):
    return {
        '0x': lambda x: int(x, 16),
        '0o': lambda x: int(x, 8),
        '0b': lambda x: int(x, 2),
    }.get(val[:2], lambda x: int(x))(val)
def register(connections, cmd, name, data=None):
    _name = name
    try:
        _name = int(_name)
    except:
        pass
    tx(connections, {'register': {**{
            'cmd': cmd,
            'name': _name,
        },
        **({'data': (riscv.constants.integer_to_list_of_bytes(integer(data), 64, 'little') if isinstance(data, str) else riscv.constants.integer_to_list_of_bytes(data, 64, 'little'))} if data else {}),
    }})
def config(connections, service, field, val):
    _val = val
    try:
        _val = integer(val)
    except:
        pass
    tx(connections, {'config': {
        'service': service,
        'field': field,
        'val': _val,
    }})
def loadbin(connections, mainmem_filename, mainmem_capacity, sp, pc, start_symbol, binary, *args):
    global state
    fd = os.open(mainmem_filename, os.O_RDWR | os.O_CREAT)
    os.ftruncate(fd, 0)
    os.ftruncate(fd, mainmem_capacity) # HACK: hard-wired memory size is dumb, but I don't want to focus on that right now
    _start_pc = pc
    with open(binary, 'rb') as fp:
        elffile = elftools.elf.elffile.ELFFile(fp)
        _addr = pc
        for section in map(lambda n: elffile.get_section_by_name(n), ['.text', '.data', '.rodata', '.bss']):
            if not section: continue
            logging.info('{} : 0x{:08x} ({})'.format(section.name, _addr, section.data_size))
            os.lseek(fd, _addr, os.SEEK_SET)
            os.write(fd, section.data())
            _addr += section.data_size
            _addr += 0x10000
            _addr |= 0xffff
            _addr ^= 0xffff
        _symbol_tables = [s for s in elffile.iter_sections() if isinstance(s, elftools.elf.elffile.SymbolTableSection)]
        _start = sum([list(filter(lambda s: start_symbol == s.name, tab.iter_symbols())) for tab in _symbol_tables], [])
        assert 0 < len(_start), 'No {} symbol!'.format(start_symbol)
        assert 2 > len(_start), 'More than one {} symbol?!?!?!?'.format(start_symbol)
        _start = next(iter(_start))
        _start_pc += _start.entry.st_value - elffile.get_section_by_name('.text').header.sh_addr
    # The value of the argc argument is the number of command line
    # arguments. The argv argument is a vector of C strings; its elements
    # are the individual command line argument strings. The file name of
    # the program being run is also included in the vector as the first
    # element; the value of argc counts this element. A null pointer
    # always follows the last element: argv[argc] is this null pointer.
    #
    # For the command ???cat foo bar???, argc is 3 and argv has three
    # elements, "cat", "foo" and "bar". 
    #
    # https://www.gnu.org/software/libc/manual/html_node/Program-Arguments.html
    #
    # see also: https://refspecs.linuxbase.org/LSB_3.1.1/LSB-Core-generic/LSB-Core-generic/baselib---libc-start-main-.html
    _argc = len(args)      # binary name is argv[0]
    _args = list(map(lambda a: '{}\0'.format(a), args))
    _fp  = sp
    _fp += 8               # 8 bytes for argc
    _fp += 8 * (1 + _argc) # 8 bytes for each argv * plus 1 NULL pointer
    _addr = list(itertools.accumulate([8 + _fp] + list(map(lambda a: len(a), _args))))
    logging.info('loadbin(): argc : {}'.format(_argc))
    logging.info('loadbin(): len(_args) : {}'.format(sum(map(lambda a: len(a), _args))))
    for x, y in zip(_addr, _args):
        logging.info('loadbin(): @{:08x} : {} ({})'.format(x, y, len(y)))
    os.lseek(fd, sp, os.SEEK_SET)
    os.write(fd, _argc.to_bytes(8, 'little'))       # argc
    for a in _addr:                                 # argv pointers
        os.write(fd, a.to_bytes(8, 'little'))
    os.lseek(fd, 8, os.SEEK_CUR)                    # NULL pointer
    os.write(fd, bytes(''.join(_args), 'ascii'))    # argv data
    os.close(fd)
    register(connections, 'set', 2, hex(sp))
    register(connections, 'set', 10, hex(_argc))
    register(connections, 'set', 11, hex(8 + sp))
    register(connections, 'set', '%pc', hex(_start_pc))
def snapshot(mainmem_filename, snapshot_filename, cycle):
    global state
    subprocess.run('cp {} {}'.format(mainmem_filename, snapshot_filename).split())
    fd = os.open(snapshot_filename, os.O_RDWR)
    os.lseek(fd, 0x10000000, os.SEEK_SET) # HACK: hard-coding snapshot metadata to 0x10000000 is dumb, but I don't want to be bothered with that now
    os.write(fd, (1 + cycle).to_bytes(8, 'little'))
    os.lseek(fd, 8, os.SEEK_CUR)
    os.fsync(fd)
    os.close(fd)
    _res_evt = state.get('futures').get(1 + cycle, {'results': [], 'events': []})
    _res_evt.get('events').append({
        'register': {
            'cmd': 'snapshot',
            'name': '{}'.format(snapshot_filename),
            'data': 0x20000000, # HACK: hard-coding snapshot metadata to 0x20000000 is dumb, but I don't want to be bothere with that now
        }
    })
    state.get('futures').update({1 + cycle: _res_evt})
def restore(mainmem_filename, snapshot_filename):
    global state
    subprocess.run('cp {} {}'.format(snapshot_filename, mainmem_filename).split())
    fd = os.open(mainmem_filename, os.O_RDWR)
    os.lseek(fd, 0x10000000, os.SEEK_SET)
    cycle = int.from_bytes(os.read(fd, 8), 'little')
#    os.write(fd, (0).to_bytes(4096, 'little'))
    os.lseek(fd, 0x20000000, os.SEEK_SET)
    pc = int.from_bytes(os.read(fd, 8), 'little')
#    os.write(fd, (0).to_bytes(4096, 'little'))
    os.close(fd)
    _res_evt = state.get('futures').get(cycle, {'results': [], 'events': []})
    _res_evt.get('events').append({
        'register': {
            'cmd': 'restore',
            'name': '{}'.format(snapshot_filename),
            'data': 0x20000000, # HACK: hard-coding snapshot metadata to 0x20000000 is dumb, but I don't want to be bothere with that now
        }
    })
    state.get('futures').update({cycle: _res_evt})
    register(state.get('connections'), 'set', '%pc', hex(pc))
    return cycle - 1
def run(cycle, max_cycles, max_instructions, break_on_undefined, snapshot_frequency):
    global state
    # {
    #   'tick': {
    #       'cycle': XXX,
    #       'results': [],
    #       'events': [],
    #   }
    # }
    state.get('lock').acquire()
    tx(state.get('connections'), 'run')
    state.get('lock').release()
    snapshot_at = cycle + snapshot_frequency
    while (cycle < max_cycles if max_cycles else True) and \
          (state.get('instructions_committed') < max_instructions if max_instructions else True) and \
          (state.get('running')) and \
          (None == state.get('undefined') if break_on_undefined else True):
        state.get('lock').acquire()
        if snapshot_at and cycle >= snapshot_at:
            snapshot(
                state.get('config').get('mainmem_filename'),
                '{}.snapshot'.format(state.get('config').get('mainmem_filename')),
                cycle
            )
            snapshot_at += snapshot_frequency
        logging.info('run(): @{:8}'.format(cycle))
        logging.info('\tinfo :\n\t\t{}'.format('\n\t\t'.join(state.get('info'))))
        state.get('info').clear()
        logging.info('\tfutures :\n\t\t{}'.format(
            '\t\t'.join(map(lambda a: '{:8}: {}\n'.format(
                a,
                state.get('futures').get(a)
            ), sorted(state.get('futures').keys())))
        ))
        cycle = (min(state.get('futures').keys()) if len(state.get('futures').keys()) else 1 + cycle)
        tx(state.get('connections'), {'tick': {
            **{'cycle': cycle},
            **dict(state.get('futures').get(cycle, {'results': [], 'events': []})),
        }})
        if cycle in state.get('futures'): state.get('futures').pop(cycle)
        state.get('ack').clear()
        state.get('lock').release()
        _ack = False
        while not _ack:
            time.sleep(0.01)
            state.get('lock').acquire()
            _ack = len(state.get('ack')) == len(state.get('connections'))
            state.get('lock').release()
    return cycle
def add_service(services, arguments, s):
    c, h = s.split(':')
    services.append(
        threading.Thread(
            target=subprocess.run,
            args=([
                'ssh',
                h,
                'python3 {} {} {} {}'.format(
                    os.path.join(os.getcwd(), c),
                    ('-D' if arguments.debug else ''),
                    '{}:{}'.format(socket.gethostbyaddr(socket.gethostname())[0], arguments.port),
                    ('--log {}'.format(arguments.log) if arguments.log else ''),
                )
            ],),
            daemon=True,
        )
    )
def spawn(services, args):
    if args.services:
        for s in args.services: add_service(services, args, s)
    threading.Thread(target=acceptor, daemon=True).start()
    [th.start() for th in services]
    while len(services) > len(state.get('connections')): time.sleep(1)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='??Service-SIMulator')
    parser.add_argument('--debug', '-D', dest='debug', action='store_true', help='output debug messages')
    parser.add_argument('--break_on_undefined', '-B', dest='break_on_undefined', action='store_true', help='cease execution on undefined instruction')
    parser.add_argument('--services', dest='services', nargs='+', help='code:host')
    parser.add_argument('--config', dest='config', nargs='+', help='service:field:val')
    parser.add_argument('--mainmem', dest='mainmem', default='/tmp/mainmem.raw:{}'.format(2**32), help='filename:capacity')
    parser.add_argument('--log', type=str, dest='log', default='/tmp', help='logging output directory')
    parser.add_argument('--max_cycles', type=int, dest='max_cycles', default=None, help='maximum number of cycles to run for')
    parser.add_argument('--max_instructions', type=int, dest='max_instructions', default=None, help='maximum number of instructions to execute')
    parser.add_argument('--snapshots', type=int, dest='snapshots', default=0, help='number of cycles per snapshot')
    parser.add_argument('port', type=int, help='port for accepting connections')
    parser.add_argument('script', type=str, help='script to be executed by ??Service-SIMulator')
    parser.add_argument('cmdline', nargs='*', help='binary to be executed and parameters')
    args = parser.parse_args()
    logging.basicConfig(
        filename=os.path.join(args.log, '{}.log'.format(os.path.basename(__file__))),
        format='%(message)s',
        level=(logging.DEBUG if args.debug else logging.INFO),
    )
    assert os.path.exists(args.script), 'Cannot open script file, {}!'.format(args.script)
    logging.debug('args : {}'.format(args))
    state = {
        'lock': threading.Lock(),
        'connections': [],
        'ack': [],
        'futures': {},
        'info': [],
        'running': False,
        'cycle': 0,
        'instructions_committed': 0,
        'undefined': None,
        'config': {
            'mainmem_filename': None,
            'mainmem_capacity': None,
        },
    }
    _mainmem_filename, _mainmem_capacity = args.mainmem.split(':')
    state.get('config').update({'mainmem_filename': _mainmem_filename})
    state.get('config').update({'mainmem_capacity': integer(_mainmem_capacity)})
    _s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    _s.bind(('0.0.0.0', args.port))
    _s.listen(5)
    _services = []
    with open(args.script) as fp:
        for raw in map(lambda x: x.strip(), fp.readlines()):
            raw = (raw[:raw.index('#')] if '#' in raw else raw)
            if '' == raw: continue
            cmd, params = ((raw.split()[0], raw.split()[1:]) if 1 < len(raw.split()) else (raw, []))
            if 'shutdown' == cmd:
                break
            elif 'tick' == cmd:
                state.update({'cycle': state.get('cycle') + sum(map(lambda x: integer(x), params))})
                tx(state.get('connections'), {
                    'tick': {
                        'cycle': state.get('cycle'),
                        'results': [],
                        'events': [],
                    }
                })
            elif 'run' == cmd:
                if args.config:
                    for c in args.config: config(state.get('connections'), *c.split(':'))
                state.update({'running': True})
                state.update({'cycle': run(state.get('cycle'), args.max_cycles, args.max_instructions, args.break_on_undefined, args.snapshots)})
                state.update({'running': False})
            elif 'loadbin' == cmd:
                _sp = integer(params[0])
                _pc = integer(params[1])
                _start_symbol = params[2]
                _binary = args.cmdline[0]
                _args = tuple(args.cmdline[1:])
                loadbin(
                    state.get('connections'),
                    state.get('config').get('mainmem_filename'),
                    state.get('config').get('mainmem_capacity'),
                    _sp, _pc, _start_symbol,
                    _binary,
                    *((_binary,) + _args)
                ),
            elif 'spawn' == cmd:
                spawn(_services, args)
                # since main memory is configured from launcher.py, the filename and capacity
                # need to be reported to the mainmem.py service... this kinda FORCES pipeline
                # implementations to have a mainmem.py service. That's not super-elegant, but
                # will get the job done for now.
                config(state.get('connections'), 'mainmem', 'main_memory_filename', state.get('config').get('mainmem_filename'))
                config(state.get('connections'), 'mainmem', 'main_memory_capacity', state.get('config').get('mainmem_capacity'))
            else:
                {
#                    'spawn': lambda: spawn(_services, args),
                    'service': lambda x: add_service(_services, args, x),
                    'register': lambda x, y, z=None: register(state.get('connections'), x, y, z),
                    'restore': lambda x, y: state.update({'cycle': restore(x, y)}),
                    'cycle': lambda: logging.info(state.get('cycle')),
                    'state': lambda: logging.info(state),
                    'config': lambda x, y, z: config(state.get('connections'), x, y, z),
                    'connections': lambda: logging.info(state.get('connections')),
                }.get(cmd, lambda : logging.fatal('Unknown command!'))(*params)
    tx(state.get('connections'), 'bye')
    [th.join() for th in _services]