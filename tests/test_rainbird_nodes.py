import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rainbird_nodes as rb  # noqa: E402


class FakePoly:
    def getValidName(self, name):
        return name


def _reset():
    rb._S.update(blocked_until=0.0, pending_notice=None, fail_pw=0, fail_net=0,
                 ctrl=None, reset=False, host='', password='', zones=[], names={})


def test_zone_address():
    assert rb.zone_address(3) == 'zone3'
    assert len(rb.zone_address(40)) <= 14


def test_cmd_int_value():
    assert rb._cmd_int({'value': '12'}) == 12
    assert rb._cmd_int({'value': 7.0}) == 7


def test_cmd_int_query_fallback():
    assert rb._cmd_int({'query': {'': '5'}}) == 5
    assert rb._cmd_int({'query': {'duration': '9'}}) == 9


def test_cmd_int_missing_or_bad():
    assert rb._cmd_int(None) is None
    assert rb._cmd_int({}) is None
    assert rb._cmd_int({'value': 'abc'}) is None


def test_clamp():
    assert rb._clamp(500, 10, 200) == 200
    assert rb._clamp(-1, 0, 14) == 0
    assert rb._clamp(30, 1, 240) == 30


def test_zone_names_and_defaults():
    _reset()
    rb.set_zone_names(FakePoly(), {'zone_names': [
        {'zone': 1.0, 'name': '  Front Lawn '},
        {'zone': 2, 'name': ''},
    ]})
    assert rb.zone_name(1) == 'Front Lawn'
    assert rb.zone_name(2) == 'Zone 2'
    assert rb.zone_name(3) == 'Zone 3'


def test_zone_names_bad_input_is_ignored():
    _reset()
    rb.set_zone_names(FakePoly(), None)
    assert rb.zone_name(1) == 'Zone 1'
    rb.set_zone_names(FakePoly(), {'zone_names': [{'zone': 'x', 'name': 'Oops'}]})
    assert rb.zone_name(1) == 'Zone 1'


def test_classify():
    class RainbirdApiException(Exception):
        pass

    class RainbirdAuthException(RainbirdApiException):
        pass

    class RainbirdCodingException(Exception):
        pass

    class RainbirdDeviceBusyException(RainbirdApiException):
        pass

    assert rb._classify(RainbirdAuthException()) == 'auth'
    assert rb._classify(RainbirdCodingException()) == 'password'
    assert rb._classify(RainbirdDeviceBusyException()) == 'busy'
    assert rb._classify(RainbirdApiException()) == 'network'
    assert rb._classify(TimeoutError()) == 'network'
    assert rb._classify(AttributeError('x')) == 'bug'


def test_three_password_failures_pause_for_sixteen_minutes():
    _reset()
    rb._record_failure('password')
    rb._record_failure('password')
    assert not rb._blocked()
    rb._record_failure('password')
    assert rb._blocked()
    assert 15 * 60 < rb._S['blocked_until'] - time.time() <= 16 * 60
    assert rb._S['pending_notice']
    # after the pause a single further failure pauses again
    assert rb._S['fail_pw'] == rb.FAILURES_BEFORE_PAUSE - 1


def test_explicit_auth_rejection_pauses_until_settings_saved():
    _reset()
    rb._record_failure('auth')
    assert rb._blocked()
    assert rb._S['blocked_until'] == float('inf')
    rb.configure(FakePoly_with_notices(), 'h', 'p')
    assert not rb._blocked()


def test_three_network_failures_pause_for_two_minutes():
    _reset()
    for _ in range(3):
        rb._record_failure('network')
    assert rb._blocked()
    assert 100 < rb._S['blocked_until'] - time.time() <= 120


def test_busy_and_bug_do_not_count():
    _reset()
    for _ in range(5):
        rb._record_failure('busy')
        rb._record_failure('bug')
    assert not rb._blocked()
    assert rb._S['fail_pw'] == 0 and rb._S['fail_net'] == 0


class FakeNotices(dict):
    def delete(self, key):
        self.pop(key, None)


class FakePoly_with_notices(FakePoly):
    def __init__(self):
        self.Notices = FakeNotices()


class FakeController:
    address = 'ctl'

    def __init__(self, name):
        self.name = name
        self.renamed_to = None

    def rename(self, new_name):
        self.renamed_to = new_name


def test_cleanup_foreign_removes_only_other_plugins_notices_and_renames():
    poly = FakePoly_with_notices()
    poly.Notices['midea_conn'] = 'old NetHome Plus banner'
    poly.Notices['config'] = 'mine'
    poly.Notices['rainbird'] = 'mine too'
    ctl = FakeController('NetHome Plus Controller')
    rb.cleanup_foreign(poly, ctl)
    assert set(poly.Notices) == {'config', 'rainbird'}
    assert ctl.renamed_to == 'Rain Bird Plugin'
    assert ctl.name == 'Rain Bird Plugin'


def test_cleanup_foreign_leaves_correct_name_alone():
    poly = FakePoly_with_notices()
    ctl = FakeController('Rain Bird Plugin')
    rb.cleanup_foreign(poly, ctl)
    assert ctl.renamed_to is None


def test_call_retries_busy_then_succeeds(monkeypatch):
    _reset()

    class RainbirdDeviceBusyException(Exception):
        pass

    class Fake:
        calls = 0

        async def get_rain_delay(self):
            Fake.calls += 1
            if Fake.calls < 3:
                raise RainbirdDeviceBusyException()
            return 4

    rb._S.update(host='h', password='p', ctrl=Fake())
    monkeypatch.setattr(rb.time, 'sleep', lambda seconds: None)
    assert rb.call('get_rain_delay') == 4
    assert Fake.calls == 3


def test_call_refuses_when_not_configured():
    _reset()
    try:
        rb.call('get_rain_delay')
        assert False, 'expected RuntimeError'
    except RuntimeError:
        pass
