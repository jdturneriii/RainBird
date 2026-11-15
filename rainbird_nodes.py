"""Rain Bird plugin: device nodes plus a small bridge to the async pyrainbird library.

pyrainbird is asyncio based while udi_interface is thread/callback based, so every
library call runs on one private event-loop thread and is serialised with a lock
(the Rain Bird controller only talks to one client at a time).
"""

import asyncio
import threading
import time

import udi_interface
from udi_interface import Node

LOGGER = udi_interface.LOGGER

UNIT_ADDR = 'rbunit'
NOTICE_KEY = 'rainbird'
OWN_NOTICE_KEYS = ('config', NOTICE_KEY)
CONTROLLER_NAME = 'Rain Bird Plugin'
MAX_RAIN_DELAY_DAYS = 14
MAX_RUN_MINUTES = 240
BUSY_ATTEMPTS = 3
BUSY_WAIT_SECONDS = 2
FAILURES_BEFORE_PAUSE = 3
PASSWORD_PAUSE_SECONDS = 16 * 60
NETWORK_PAUSE_SECONDS = 2 * 60

_S = {
    'loop': None,
    'session': None,
    'ctrl': None,
    'host': '',
    'password': '',
    'blocked_until': 0.0,
    'pending_notice': None,
    'fail_pw': 0,
    'fail_net': 0,
    'lock': threading.RLock(),
    'boot_lock': threading.Lock(),
    'started': False,
    'reset': False,
    'error_shown': False,
    'zones': [],
    'names': {},
    'nodes': {},
    'max_programs': 4,
}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def zone_address(zone):
    return 'zone%d' % int(zone)


def zone_name(zone):
    return _S['names'].get(int(zone)) or ('Zone %d' % int(zone))


def _clamp(value, low, high):
    return max(low, min(high, value))


def _cmd_int(command):
    """Pull the single numeric parameter out of an IoX command dict."""
    if not command:
        return None
    value = command.get('value')
    if value is None:
        query = command.get('query') or {}
        if '' in query:
            value = query['']
        elif query:
            value = next(iter(query.values()))
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _blocked():
    return time.time() < _S['blocked_until']


def _notice(poly, key, text=None):
    try:
        if text is None:
            poly.Notices.delete(key)
        else:
            poly.Notices[key] = text
    except Exception as ex:
        LOGGER.debug('Notice %s update ignored: %s', key, ex)


def cleanup_foreign(poly, controller):
    """Remove leftovers of a previous plugin that used this slot.

    Deletes every banner this plugin did not create and gives the controller
    node this plugin's own name.
    """
    try:
        for key in list(poly.Notices.keys()):
            if key not in OWN_NOTICE_KEYS:
                LOGGER.info('Removing stale notice %r left by a previous plugin', key)
                poly.Notices.delete(key)
    except Exception as ex:
        LOGGER.debug('Stale notice cleanup ignored: %s', ex)
    try:
        if controller.name != CONTROLLER_NAME:
            LOGGER.info('Renaming controller node %r to %r', controller.name, CONTROLLER_NAME)
            try:
                controller.rename(CONTROLLER_NAME)
            except AttributeError:
                poly.renameNode(controller.address, CONTROLLER_NAME)
            controller.name = CONTROLLER_NAME
    except Exception as ex:
        LOGGER.error('Could not rename controller node: %s', ex)


# --------------------------------------------------------------------------
# asyncio bridge to pyrainbird
# --------------------------------------------------------------------------

def _loop():
    if _S['loop'] is None:
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, name='rainbird-loop', daemon=True)
        thread.start()
        _S['loop'] = loop
    return _S['loop']


def _run(coro, timeout=60):
    future = asyncio.run_coroutine_threadsafe(coro, _loop())
    try:
        return future.result(timeout)
    except Exception:
        future.cancel()
        raise


async def _aconnect():
    import aiohttp
    from pyrainbird import async_client

    if _S['session'] is None or _S['session'].closed:
        _S['session'] = aiohttp.ClientSession()
    factory = getattr(async_client, 'create_controller', None)  # newer releases only
    if factory is not None:
        controller = await factory(_S['session'], _S['host'], _S['password'])
    else:
        controller = async_client.CreateController(_S['session'], _S['host'], _S['password'])
    # Probe right away so a wrong IP/password shows up immediately (result is cached by the library).
    await controller.get_model_and_version()
    return controller


async def _aclose():
    session = _S['session']
    _S['session'] = None
    if session is not None and not session.closed:
        await session.close()


def configure(poly, host, password):
    """Store connection settings. Returns True when they changed."""
    changed = (host != _S['host']) or (password != _S['password'])
    _S['host'] = host
    _S['password'] = password
    if changed:
        _S['reset'] = True
        _S['blocked_until'] = 0.0
        _S['pending_notice'] = None
        _S['fail_pw'] = 0
        _S['fail_net'] = 0
        _S['zones'] = []
        clear_error(poly)
    if host and password:
        _notice(poly, 'config')
    else:
        _notice(poly, 'config', 'Enter the Rain Bird controller IP address and password in the plugin configuration.')
    return changed


def shutdown():
    try:
        with _S['lock']:
            _S['ctrl'] = None
            _run(_aclose(), 10)
    except Exception as ex:
        LOGGER.debug('Rain Bird shutdown: %s', ex)


def _classify(err):
    """Sort a failure into: auth, password, busy, bug or network."""
    names = {c.__name__ for c in type(err).__mro__}
    if names & {'RainbirdAuthException', 'RainbirdAuthLockoutException'}:
        return 'auth'
    if 'RainbirdCodingException' in names:
        return 'password'  # reply could not be decoded: usually a wrong password
    if 'RainbirdDeviceBusyException' in names:
        return 'busy'
    if isinstance(err, (AttributeError, TypeError, ValueError, KeyError)):
        return 'bug'
    return 'network'  # generic API error, timeout, connection problem


def _block(until, text):
    _S['blocked_until'] = until
    _S['pending_notice'] = text
    _S['ctrl'] = None


def _record_failure(kind):
    now = time.time()
    if kind == 'auth':
        _block(float('inf'),
               'The Rain Bird controller rejected the password. Correct it in the plugin configuration '
               'and save. Polling is paused until then so the controller is not locked out.')
    elif kind == 'password':
        _S['fail_pw'] += 1
        if _S['fail_pw'] >= FAILURES_BEFORE_PAUSE:
            _S['fail_pw'] = FAILURES_BEFORE_PAUSE - 1  # one more failure after the pause re-pauses
            _block(now + PASSWORD_PAUSE_SECONDS,
                   'The Rain Bird controller keeps sending replies the plugin cannot read - this usually '
                   'means the password is wrong. Polling is paused for 16 minutes to avoid a lockout. '
                   'Check the password in the plugin configuration and save.')
    elif kind == 'network':
        _S['fail_net'] += 1
        if _S['fail_net'] >= FAILURES_BEFORE_PAUSE:
            _S['fail_net'] = FAILURES_BEFORE_PAUSE - 1
            _block(now + NETWORK_PAUSE_SECONDS,
                   'Cannot reach the Rain Bird controller at %s. Check the IP address and that the WiFi '
                   'module is online. The plugin will retry in 2 minutes.' % _S['host'])


def call(method, *args):
    """Run one pyrainbird controller coroutine, one at a time. Raises on failure."""
    with _S['lock']:
        if not _S['host'] or not _S['password']:
            raise RuntimeError('Rain Bird host/password not configured yet')
        if _blocked():
            raise RuntimeError('Rain Bird access is paused')
        if _S['reset']:
            _S['reset'] = False
            _S['ctrl'] = None
            try:
                _run(_aclose(), 10)
            except Exception as ex:
                LOGGER.debug('Closing old Rain Bird session: %s', ex)
        attempts = 0
        while True:
            attempts += 1
            try:
                if _S['ctrl'] is None:
                    _S['ctrl'] = _run(_aconnect(), 60)
                result = _run(getattr(_S['ctrl'], method)(*args), 60)
                _S['fail_pw'] = 0
                _S['fail_net'] = 0
                return result
            except Exception as err:
                kind = _classify(err)
                if kind == 'busy' and attempts < BUSY_ATTEMPTS:
                    LOGGER.info('Rain Bird controller busy, retrying (%d/%d)', attempts, BUSY_ATTEMPTS)
                    time.sleep(BUSY_WAIT_SECONDS)
                    continue
                _record_failure(kind)
                raise


def handle_error(poly, err):
    if _S['pending_notice']:
        _notice(poly, NOTICE_KEY, _S['pending_notice'])
        LOGGER.error('Rain Bird paused polling: %s', _S['pending_notice'])
        _S['pending_notice'] = None
        _S['error_shown'] = True
        return
    if isinstance(err, RuntimeError):
        LOGGER.warning('%s', err)
        return
    kind = _classify(err)
    if kind == 'busy':
        LOGGER.warning('Rain Bird controller is busy (another app talking to it?); will retry next poll')
    elif kind == 'bug':
        LOGGER.error('Rain Bird plugin or pyrainbird mismatch (%s): %s', type(err).__name__, err)
    elif kind == 'password':
        LOGGER.warning('Rain Bird reply could not be decoded (wrong password?) %d/%d',
                       _S['fail_pw'], FAILURES_BEFORE_PAUSE)
    else:
        LOGGER.warning('Rain Bird communication problem (%s): %s', type(err).__name__, err)


def clear_error(poly):
    if _S['error_shown']:
        _notice(poly, NOTICE_KEY)
        _S['error_shown'] = False


def _safe(poly, fn):
    try:
        fn(poly)
        clear_error(poly)
        return True
    except Exception as ex:
        handle_error(poly, ex)
        return False


def run_action(poly, label, method, *args):
    try:
        call(method, *args)
        LOGGER.info('Rain Bird: %s ok', label)
        return True
    except Exception as ex:
        LOGGER.error('Rain Bird: %s failed', label)
        handle_error(poly, ex)
        return False


# --------------------------------------------------------------------------
# discovery, naming and status updates
# --------------------------------------------------------------------------

def discover_nodes(poly, primary):
    info = call('get_model_and_version')
    model_name = getattr(info, 'model_name', None) or 'Rain Bird'
    try:
        programs = int(info.model_info.max_programs)
        if programs > 0:
            _S['max_programs'] = programs
    except Exception:
        pass
    LOGGER.info('Rain Bird model: %s', model_name)

    stations = call('get_available_stations')
    zones = sorted(int(z) for z in stations.active_set)
    _S['zones'] = zones
    LOGGER.info('Rain Bird zones available: %s', zones)

    unit = RainbirdUnit(poly, primary, UNIT_ADDR, poly.getValidName('Rain Bird %s' % model_name))
    poly.addNode(unit)
    _S['nodes'][UNIT_ADDR] = unit
    for zone in zones:
        address = zone_address(zone)
        node = RainbirdZone(poly, primary, address, poly.getValidName(zone_name(zone)), zone)
        poly.addNode(node)
        _S['nodes'][address] = node


def set_zone_names(poly, data):
    """Apply the customer's typed zone-name list and rename existing zone nodes."""
    names = {}
    try:
        entries = (data or {}).get('zone_names') or []
        for entry in entries:
            zone = int(float(entry.get('zone')))
            name = str(entry.get('name') or '').strip()
            if name:
                names[zone] = name
    except Exception as ex:
        LOGGER.error('Could not read zone names: %s', ex)
    _S['names'] = names
    for zone in list(_S['zones']):
        node = _S['nodes'].get(zone_address(zone))
        if node is None:
            continue
        wanted = poly.getValidName(zone_name(zone))
        if node.name == wanted:
            continue
        try:
            try:
                node.rename(wanted)
            except AttributeError:
                poly.renameNode(node.address, wanted)
            node.name = wanted
        except Exception as ex:
            LOGGER.error('Could not rename %s to %s: %s', node.address, wanted, ex)


def update_fast(poly):
    states = call('get_zone_states')
    active = set(int(z) for z in states.active_set)
    for zone in _S['zones']:
        node = _S['nodes'].get(zone_address(zone))
        if node is not None:
            node.setDriver('ST', 1 if zone in active else 0)
    unit = _S['nodes'].get(UNIT_ADDR)
    if unit is not None:
        unit.setDriver('ST', 1 if active else 0)


def update_slow(poly):
    unit = _S['nodes'].get(UNIT_ADDR)
    if unit is None:
        return
    first_error = None
    try:
        unit.setDriver('DELAY', int(call('get_rain_delay')))
    except Exception as ex:
        first_error = first_error or ex
    try:
        unit.setDriver('RAIN_SENSOR', 1 if call('get_rain_sensor_state') else 0)
    except Exception as ex:
        first_error = first_error or ex
    try:
        budget = call('water_budget', 0)
        unit.setDriver('WATER_BUDGET', int(budget.adjust))
    except Exception as ex:
        first_error = first_error or ex
    if first_error is not None:
        raise first_error


def bootstrap(poly, primary):
    """Connect, (re)create nodes and read everything once."""
    if not _S['boot_lock'].acquire(False):
        return
    try:
        if not _S['host'] or not _S['password']:
            return
        discover_nodes(poly, primary)
        update_fast(poly)
        try:
            update_slow(poly)
        except Exception as ex:
            handle_error(poly, ex)
        clear_error(poly)
    except Exception as ex:
        handle_error(poly, ex)
        if not _S['error_shown'] and not isinstance(ex, RuntimeError):
            _notice(poly, NOTICE_KEY,
                    'Could not connect to the Rain Bird controller at %s. Check the IP address and '
                    'password in the plugin configuration (see the plugin log for details).' % _S['host'])
            _S['error_shown'] = True
    finally:
        _S['boot_lock'].release()


def poll_fast(poly, primary):
    if not _S['started'] or not _S['host'] or _blocked():
        return
    if not _S['zones']:
        bootstrap(poly, primary)
        return
    _safe(poly, update_fast)


def poll_slow(poly, primary):
    if not _S['started'] or not _S['host'] or _blocked():
        return
    if not _S['zones']:
        bootstrap(poly, primary)
        return
    _safe(poly, update_slow)


# --------------------------------------------------------------------------
# nodes
# --------------------------------------------------------------------------

class RainbirdUnit(Node):
    """The Rain Bird controller itself: rain delay, rain sensor, water budget, programs."""

    id = 'rbunit'
    drivers = [
        {'driver': 'ST', 'value': 0, 'uom': 25, 'name': 'Watering'},
        {'driver': 'DELAY', 'value': 0, 'uom': 10, 'name': 'Rain Delay'},
        {'driver': 'RAIN_SENSOR', 'value': 0, 'uom': 25, 'name': 'Rain Sensor'},
        {'driver': 'WATER_BUDGET', 'value': 100, 'uom': 51, 'name': 'Water Budget'},
    ]

    def stop_all(self, command=None):
        if run_action(self.poly, 'stop all watering', 'stop_irrigation'):
            _safe(self.poly, update_fast)

    def set_delay(self, command=None):
        days = _cmd_int(command)
        if days is None:
            return
        days = _clamp(days, 0, MAX_RAIN_DELAY_DAYS)
        if run_action(self.poly, 'set rain delay %d days' % days, 'set_rain_delay', days):
            self.setDriver('DELAY', days)

    def run_program(self, command=None):
        program = _cmd_int(command)
        if program is None:
            return
        program = _clamp(program, 0, max(0, _S['max_programs'] - 1))
        if run_action(self.poly, 'run program %d' % program, 'set_program', program):
            _safe(self.poly, update_fast)

    commands = {
        'STOP_ALL': stop_all,
        'SET_DELAY': set_delay,
        'RUN_PROGRAM': run_program,
    }


class RainbirdZone(Node):
    """One sprinkler zone (station)."""

    id = 'rbzone'
    drivers = [
        {'driver': 'ST', 'value': 0, 'uom': 25, 'name': 'Zone State'},
        {'driver': 'DUR', 'value': 10, 'uom': 45, 'name': 'Run Time'},
    ]

    def __init__(self, polyglot, primary, address, name, zone):
        super().__init__(polyglot, primary, address, name)
        self.zone = int(zone)

    def don(self, command=None):
        minutes = _cmd_int(command)
        if minutes is None:
            try:
                minutes = int(float(self.getDriver('DUR')))
            except (TypeError, ValueError):
                minutes = 10
        minutes = _clamp(minutes, 1, MAX_RUN_MINUTES)
        self.setDriver('DUR', minutes)
        if run_action(self.poly, 'run zone %d for %d min' % (self.zone, minutes),
                      'irrigate_zone', self.zone, minutes):
            _safe(self.poly, update_fast)

    def dof(self, command=None):
        # The controller can only stop everything, so only do so if this zone is running.
        try:
            states = call('get_zone_states')
            if self.zone in set(int(z) for z in states.active_set):
                call('stop_irrigation')
                LOGGER.info('Rain Bird: stopped watering (zone %d was running)', self.zone)
        except Exception as ex:
            handle_error(self.poly, ex)
            return
        _safe(self.poly, update_fast)

    def set_dur(self, command=None):
        minutes = _cmd_int(command)
        if minutes is None:
            return
        self.setDriver('DUR', _clamp(minutes, 1, MAX_RUN_MINUTES))

    commands = {
        'DON': don,
        'DOF': dof,
        'SET_DUR': set_dur,
    }
