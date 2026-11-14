from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from dm_bot import Bot, Scheduler, Telegram, TelegramError, parse_login, parse_password_login, lock_instance
from dm_login import encrypt_password
from dm_bot_store import Store
from dm_bot_worker import Worker, JobCancelled
from dm_grab import BusinessError

OWNER = 100000001
UA = {'device': 'OnePlus PHK110', 'system': 'android', 'version': '5.0.0', 'jPushId': 'push'}
PROFILE = {'uid': '42', 'token': 'secret-token', 'version': '5.0.0', 'jPushId': 'push'}


class API:
    def __init__(self):
        self.sent, self.deleted = [], []

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))

    def delete(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


def message(text, sender=OWNER, chat_type='private'):
    return {'update_id': 11, 'message': {'message_id': 123, 'text': text,
                'from': {'id': sender}, 'chat': {'id': sender if chat_type == 'private' else -123, 'type': chat_type}}}


@pytest.fixture
def state(tmp_path):
    store = Store(tmp_path / 'bot.db')
    store.put_account('42', PROFILE, json.dumps(UA))
    return store


def test_login_json_spaces_are_preserved_and_header_values_checked():
    profile, ua = parse_login('42 secret-token ' + json.dumps(UA, indent=2))
    assert profile == PROFILE
    assert json.loads(ua)['device'] == 'OnePlus PHK110'
    with pytest.raises(ValueError, match='JSON 后'):
        parse_login('42 secret-token ' + json.dumps(UA) + ' different 5.0.0')


@pytest.mark.parametrize('field,value', [
    ('jPushId', False), ('jPushId', 123),
    ('jPushId', 'bad push'), ('jPushId', 'x' * 257),
    ('version', None), ('version', ''), ('version', False), ('version', 500),
    ('version', '5.0'),
])
def test_login_requires_push_and_version_in_ua(field, value):
    ua = dict(UA)
    if value is None:
        del ua[field]
    else:
        ua[field] = value
    with pytest.raises(ValueError, match=field):
        parse_login('42 secret-token ' + json.dumps(ua))


@pytest.mark.parametrize('push', [None, ''])
def test_session_import_allows_unregistered_push_id(push):
    ua = dict(UA)
    if push is None:
        ua.pop('jPushId')
    else:
        ua['jPushId'] = push
    profile, header = parse_login('42 secret-token ' + json.dumps(ua))
    assert profile == {'uid':'42','token':'secret-token','version':'5.0.0'}
    assert json.loads(header) == ua


@pytest.mark.parametrize('password', ['password123', r'p\a$$word', ' Pass word! '])
def test_password_login_parser_preserves_special_characters(password):
    argument = json.dumps(password) if ' ' in password else password
    assert parse_password_login('example-account ' + argument) == ('example-account', password)


@pytest.mark.parametrize('arguments', ['', 'account', 'account ""', 'account "unterminated', 'account two words'])
def test_password_login_parser_rejects_incomplete_input(arguments):
    with pytest.raises(ValueError):
        parse_password_login(arguments)


def test_permissions_one_persists_login_access_without_other_uid_control(state):
    api = API()
    bot = Bot(state, api, OWNER, clients=lambda *_: pytest.fail('No App request required'))
    bot.handle(message('/permissions 1 77', sender=77))
    assert not state.can_login(77, OWNER)
    bot.handle(message('/permissions 1 77'))
    restarted = Store(state.path)
    assert restarted.can_login(77, OWNER) and restarted.can_login(OWNER, OWNER)
    assert not restarted.allowed('42', 77, OWNER)
    assert not restarted.allowed('99', 77, OWNER)
    with pytest.raises(ValueError):
        restarted.account('1')


@pytest.mark.parametrize('command', ['/login account password123', '/login 42 token {}'])
def test_uid_permission_alone_does_not_allow_either_login_mode(state, command):
    state.grant('42', 77)
    api = API()
    denied = lambda *_: pytest.fail('Login authorization must precede any request')
    Bot(state, api, OWNER, clients=denied, password_clients=denied).handle(message(command, sender=77))
    assert not state.can_login(77, OWNER)
    assert api.deleted == [(77, 123)] and '/permissions 1' in api.sent[0][1]


def test_login_grantee_can_import_and_only_gains_verified_uid(state):
    state.grant_login(77)
    api = API()

    @contextmanager
    def clients(profile, ua):
        yield SimpleNamespace(call=lambda *_: {'userId':'42'})

    bot = Bot(state, api, OWNER, clients=clients,
              password_clients=lambda *_: pytest.fail('Token import must not use password login'))
    bot.handle(message('/login 42 fresh-token ' + json.dumps(UA), sender=77))
    assert state.account('42')[0]['token'] == 'fresh-token'
    assert state.allowed('42', 77, OWNER) and not state.allowed('99', 77, OWNER)
    bot.handle(message('/permissions 99 88', sender=77))
    assert '仅主人' in api.sent[-1][1]


@pytest.mark.parametrize('push', ['real-push', '', None])
def test_token_import_preserves_entire_supplied_device_configuration(state, monkeypatch, push):
    state.grant_login(77)
    supplied = dict(UA, device='Existing Phone', hardware='existing-hardware',
                    uuid='existing-device-uuid', sysVersion='Android 33 13', version='5.1.0',
                    screenResolution='1080-2300', startTime=1700000000000,
                    createTime=1690000000000, jPushId=push, extraField='keep-me')
    monkeypatch.setattr(state,'login_device',lambda *_: pytest.fail('Token import must preserve its UA'))

    @contextmanager
    def clients(profile, ua):
        assert json.loads(ua) == supplied
        yield SimpleNamespace(call=lambda *_: {'userId':'42'})

    api = API()
    Bot(state,api,OWNER,clients=clients,
        password_clients=lambda *_: pytest.fail('Token import cannot use password login')).handle(
            message('/login 42 fresh-token '+json.dumps(supplied),sender=77))
    assert json.loads(state.account('42')[1]) == supplied
    assert state.allowed('42',77,OWNER)
    with state.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM login_devices').fetchone()[0] == 0


@pytest.mark.parametrize('uid_matches', [True, False])
def test_password_login_redacts_errors_and_never_saves_failed_session(state, uid_matches):
    state.grant_login(77)
    api = API()
    password = 'password123'
    wrapped = encrypt_password(password)

    @contextmanager
    def passwords(ua):
        if not uid_matches:
            yield SimpleNamespace(login=lambda *_: dict(PROFILE, token='fresh-token'))
        else:
            def rejected(*_):
                raise BusinessError('400', password + ' ' + wrapped)
            yield SimpleNamespace(login=rejected)

    @contextmanager
    def clients(*_):
        yield SimpleNamespace(call=lambda *_: {'userId':'99'})

    Bot(state, api, OWNER, clients=clients, password_clients=passwords).handle(
        message('/login account ' + password, sender=77))
    assert state.account('42')[0] == PROFILE
    assert not state.allowed('42', 77, OWNER)
    assert all(password not in text and wrapped not in text and 'fresh-token' not in text for _,text in api.sent)
    assert password.encode() not in state.path.read_bytes()


def test_password_login_group_is_rejected_before_device_or_network_access(state):
    state.grant_login(77)
    api = API()
    denied = lambda *_: pytest.fail('Group login must not request App')
    Bot(state,api,OWNER,clients=denied,password_clients=denied).handle(
        message('/login account password123',sender=77,chat_type='group'))
    assert api.deleted == [(-123,123)]
    with state.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM login_devices').fetchone()[0] == 0


def test_nonowner_cannot_import_or_grant_and_secret_message_is_deleted(state):
    api = API()
    bot = Bot(state, api, OWNER, clients=lambda *_: pytest.fail('No API calls allowed'))
    bot.handle(message('/login 99 stolen-token {}', sender=77))
    bot.handle(message('/permissions 42 77', sender=77))
    assert not state.allowed('42', 77, OWNER)
    assert api.deleted == [(77, 123)]
    assert all('主人' in text and 'stolen-token' not in text for _, text in api.sent)


def test_delegated_uid_permissions_apply_before_any_api_request(state):
    api = API()
    bot = Bot(state, api, OWNER, clients=lambda *_: pytest.fail('Unauthorized API call'))
    for command in ['/me 42', '/list 42', '/add 42 123', '/cancel 42 123']:
        bot.handle(message(command, sender=77))
    assert len(api.sent) == 4 and all('没有' in text for _, text in api.sent)
    bot.handle(message('/permissions 42 77'))
    assert state.allowed('42', 77, OWNER)
    assert not state.allowed('99', 77, OWNER)


def test_import_verifies_identity_before_overwriting_and_never_echoes_token(state):
    api = API()
    response = {'userId': '99'}

    @contextmanager
    def clients(profile, ua):
        class Client:
            def call(self, *_args):
                return response
        yield Client()

    bot = Bot(state, api, OWNER, clients=clients)
    command = '/login 42 new-secret ' + json.dumps(UA)
    bot.handle(message(command))
    assert state.account('42')[0]['token'] == 'secret-token'
    response['userId'] = '42'
    bot.handle(message(command))
    assert state.account('42')[0]['token'] == 'new-secret'
    assert all('new-secret' not in text for _, text in api.sent)


def test_group_cannot_import_even_when_owner_and_credential_is_deleted(state):
    api = API()
    Bot(state, api, OWNER, clients=lambda *_: pytest.fail('Group credentials cannot be used')).handle(
        message('/login 42 new-secret {}', chat_type='group'))
    assert api.deleted == [(-123, 123)]
    assert state.account('42')[0] == PROFILE


def test_delegated_me_returns_personal_info_but_never_session_secrets(state):
    state.grant('42', 77)
    state.add_job('42', '123', OWNER, OWNER, 'Activity', time.time() + 600)
    api = API()

    @contextmanager
    def clients(profile, ua):
        class Client:
            def call(self, endpoint, fields):
                assert endpoint == '/v3/user/mine_homepage'
                return {'userId': '42', 'name': '测试姓名', 'nickname': 'nick', 'token': 'response-secret'}
        yield Client()

    Bot(state, api, OWNER, clients=clients).handle(message('/me 42', sender=77))
    assert '测试姓名' in api.sent[0][1]
    assert 'secret' not in api.sent[0][1]
    assert '待抢任务' not in api.sent[0][1] and '123' not in api.sent[0][1]


@pytest.mark.parametrize('sender', [OWNER, 77])
def test_list_returns_only_current_uid_active_jobs_without_app_request(state, sender):
    state.grant('42', 77)
    for target in ('111', '222', '333', '444'):
        job, _ = state.add_job('42', target, OWNER, OWNER, 'Activity', time.time() + 600)
        if target in ('222', '333'):
            assert state.claim(job['id'])
        if target == '333':
            state.cancel('42', target)
        if target == '444':
            state.finish(job['id'], 'success', {})
    state.put_account('99', dict(PROFILE, uid='99'), json.dumps(UA))
    state.add_job('99', '555', OWNER, OWNER, 'Other account', time.time() + 600)
    api = API()
    Bot(state, api, OWNER, clients=lambda *_: pytest.fail('List must not call the App')).handle(
        message('/list 42', sender=sender))
    assert api.sent == [(sender, 'UID：42\n待抢任务：111、222、333')]


def test_list_empty_and_invalid_arguments_do_not_call_app(state):
    api = API()
    bot = Bot(state, api, OWNER, clients=lambda *_: pytest.fail('List must not call the App'))
    bot.handle(message('/list 42'))
    assert api.sent == [(OWNER, 'UID：42\n待抢任务：无')]
    for command in ('/list', '/list 42 123', '/list invalid'):
        bot.handle(message(command))
    assert all('参数数量' in text for _, text in api.sent[1:3])
    assert 'UID 必须为数字' in api.sent[3][1]


def test_expired_token_is_reported_without_automatic_password_login(state):
    api = API()

    @contextmanager
    def clients(profile, ua):
        class Client:
            def call(self, *_args):
                raise BusinessError('10007', '')
        yield Client()

    Bot(state, api, OWNER, clients=clients).handle(message('/me 42'))
    assert '会话已失效' in api.sent[0][1]
    assert state.account('42')[0]['token'] == 'secret-token'


def test_duplicate_tasks_persist_and_cancel_does_not_withdraw_registration(state):
    first, created = state.add_job('42', '123', OWNER, OWNER, 'Activity', time.time() + 100)
    duplicate, created_again = state.add_job('42', '123', OWNER, OWNER, 'Activity', time.time() + 100)
    assert created and not created_again and first['id'] == duplicate['id']
    restarted = Store(state.path)
    assert len(restarted.jobs(('waiting',))) == 1
    api = API()
    Bot(restarted, api, OWNER, clients=lambda *_: pytest.fail('Cancel never calls signup APIs')).handle(message('/cancel 42 123'))
    assert restarted.job(first['id'])['status'] == 'cancelled'
    assert not restarted.begin_submit(first['id'])


def test_cancel_and_submission_gate_are_atomic(state):
    job, _ = state.add_job('42', '123', OWNER, OWNER, 'Activity', time.time())
    assert state.claim(job['id'])
    state.cancel('42', '123')
    assert not state.begin_submit(job['id'])
    assert state.job(job['id'])['submit_attempts'] == 0


def test_restart_recovers_only_tasks_that_never_submitted(state):
    waiting, _ = state.add_job('42', '123', OWNER, OWNER, 'Waiting', time.time() + 100)
    sent, _ = state.add_job('42', '456', OWNER, OWNER, 'Sent', time.time() + 100)
    state.claim(waiting['id'])
    state.claim(sent['id'])
    assert state.begin_submit(sent['id'])
    Store(state.path).recover()
    assert state.job(waiting['id'])['status'] == 'waiting'
    assert state.job(sent['id'])['status'] == 'uncertain'
    assert not state.claim(sent['id'])


def test_worker_cancellation_checks_and_inflight_reconciliation(state, tmp_path):
    job, _ = state.add_job('42', '123', OWNER, OWNER, 'Activity', time.time())
    state.claim(job['id'])
    worker = Worker(state, job['id'], tmp_path / 'logs')
    state.cancel('42', '123')
    with pytest.raises(JobCancelled):
        worker.check()
    worker.submitted = True
    worker.check()  # In-flight status queries must finish.
    with pytest.raises(JobCancelled):
        worker.check(before_submit=True)


def test_cancelling_completed_job_is_not_a_withdrawal(state):
    job, _ = state.add_job('42', '123', OWNER, OWNER, 'Activity', time.time())
    state.finish(job['id'], 'success', {'signUpId': '456'})
    assert state.cancel('42', '123') is None
    assert state.job(job['id'])['status'] == 'success'


def test_terminal_notifications_are_durable_and_only_sent_once(state, tmp_path):
    job, _ = state.add_job('42', '123', 77, 77, 'Activity', time.time())
    state.finish(job['id'], 'success', {'signUpId': '456'})
    api = API()
    scheduler = Scheduler(state, api, OWNER, tmp_path)
    scheduler.notify()
    scheduler.notify()
    assert [chat for chat, _ in api.sent] == [77, OWNER]
    assert all('456' in text for _, text in api.sent)


def test_real_telegram_http_exchange_and_error_redaction():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            params = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append((self.path, params))
            body = json.dumps({'ok': True, 'result': {'message_id': 1}}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        telegram = Telegram('test-secret', base_url=f'http://127.0.0.1:{server.server_port}')
        telegram.send(77, 'hello')
        telegram.delete(77, 1)
        assert received[0][0] == '/bottest-secret/sendMessage'
        assert received[0][1]['protect_content'] is True
        assert received[1][1] == {'chat_id': 77, 'message_id': 1}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    with pytest.raises(TelegramError) as error:
        telegram.send(77, 'hello')
    assert 'test-secret' not in str(error.value)


def test_second_instance_cannot_recover_live_jobs(tmp_path):
    path = tmp_path / 'bot.lock'
    first = lock_instance(path)
    try:
        with pytest.raises(RuntimeError, match='已经运行'):
            lock_instance(path)
    finally:
        first.close()
    lock_instance(path).close()


def test_add_routes_to_persistent_idempotent_task_and_never_submits(state):
    api = API()

    @contextmanager
    def clients(profile, ua):
        class Client:
            def call(self, endpoint, fields):
                assert endpoint == '/v2/activity/detail'
                assert fields == {'activityId': '123'}
                return {'activityName': '测试活动', 'ableJoinFlag': 1, 'buttonType': 2,
                        'joinstartdate': (time.time() + 600) * 1000, 'joinmaxnum': 85, 'joinNum': 0}
        yield Client()

    bot = Bot(state, api, OWNER, clients=clients)
    bot.handle(message('/add 42 123'))
    bot.handle(message('/add 42 123'))
    assert len(state.jobs(('waiting',))) == 1
    assert '已添加' in api.sent[0][1] and '已存在' in api.sent[1][1]


def test_worker_prewarms_connection_before_the_early_captcha_window(state, tmp_path, monkeypatch):
    import dm_bot_worker as module
    job, _ = state.add_job('42', '123', OWNER, OWNER, 'Activity', 110)
    state.claim(job['id'])
    worker = Worker(state, job['id'], tmp_path / 'logs')
    worker.job = state.job(job['id'])
    clock = SimpleNamespace(now=85.0)
    monkeypatch.setattr(module.time, 'time', lambda: clock.now)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: setattr(clock, 'now', clock.now + seconds))
    observed = []

    class Client:
        timeout = 10

        def call(self, endpoint, fields):
            observed.append((clock.now, self.timeout))
            clock.now += min(self.timeout, 6)
            return {}

    worker.client = Client()
    worker.sleep(25)
    assert len(observed) == 2
    assert observed[0][0] <= 87
    assert 9 <= observed[0][1] <= 10
    assert 102 <= observed[1][0] < 103
    assert 6 <= observed[1][1] <= 7
    assert clock.now == pytest.approx(110)
    assert worker.client.timeout == 10


def test_worker_final_connection_warmup_cannot_oversleep_opening(state, tmp_path, monkeypatch):
    import dm_bot_worker as module
    job, _ = state.add_job('42', '123', OWNER, OWNER, 'Activity', 150)
    state.claim(job['id'])
    worker = Worker(state, job['id'], tmp_path / 'logs')
    worker.job = state.job(job['id'])
    clock = SimpleNamespace(now=144.0)
    monkeypatch.setattr(module.time, 'time', lambda: clock.now)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: setattr(clock, 'now', clock.now + seconds))
    observed = []

    class Client:
        timeout = 10

        def call(self, endpoint, fields):
            observed.append((clock.now, self.timeout))
            clock.now += self.timeout
            raise module.EnrollmentError('bounded connection timeout')

    worker.client = Client()
    worker.sleep(6)
    assert observed == [(144, 5)]
    assert clock.now == pytest.approx(150)
    assert worker.client.timeout == 10


@pytest.mark.parametrize('mode', ['success', 'session_changed', 'boundary_session_changed', 'cancelled'])
def test_worker_early_captcha_uses_current_session_and_honors_cancellation(state, tmp_path, monkeypatch, capsys, mode):
    import dm_bot_worker as module
    from dm_grab import DETAIL, INFO, SUBMIT
    job, _ = state.add_job('42', '123', OWNER, OWNER, 'Activity', 150)
    state.claim(job['id'])
    worker = Worker(state, job['id'], tmp_path / 'logs')
    original_emit = worker.emit

    def emit(kind, **fields):
        if mode == 'boundary_session_changed' and kind == 'captcha_prefetch_reused':
            state.put_account('42', dict(PROFILE, token='updated-session'), json.dumps(UA))
        original_emit(kind, **fields)

    worker.emit = emit
    clock = SimpleNamespace(now=110.0)
    monkeypatch.setattr(module.time, 'time', lambda: clock.now)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: setattr(clock, 'now', clock.now + seconds))
    real_runner = module.EnrollmentRunner
    monkeypatch.setattr(module, 'EnrollmentRunner', lambda *args, **kw: real_runner(*args, clock=lambda: clock.now, **kw))
    submissions, solves = [], []

    def call(client, endpoint, fields):
        if endpoint == DETAIL:
            return {'activityId': 123, 'joinstartdate': 150000, 'joinenddate': 300000,
                    'ableJoinFlag': 1, 'buttonType': 3, 'signUpId': 0}
        if endpoint == INFO:
            if clock.now < 150:
                raise BusinessError('closed', '报名未开放')
            return {'info': [], 'openCaptcha': True, 'captchaId': module.CAPTCHA_HINT}
        assert endpoint == SUBMIT
        submissions.append((clock.now, dict(fields), dict(client.profile)))
        return {'signUpId': '456'}

    class Browser:
        last_timings = {'totalSeconds': 6}
        _context = SimpleNamespace(tracing=SimpleNamespace(start=lambda **_kw: None, stop=lambda **_kw: None))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def prepare(self, _cid):
            pass

        def solve(self, captcha_id, *, timeout=60):
            solves.append((clock.now, timeout, captcha_id))
            clock.now += 6
            if len(solves) == 1:
                if mode == 'session_changed':
                    state.put_account('42', dict(PROFILE, token='updated-session'), json.dumps(UA))
                elif mode == 'cancelled':
                    state.cancel('42', '123')
            return {'validate': 'early-private-result' if len(solves) == 1 else 'fresh-private-result'}

    monkeypatch.setattr(module.DmClient, 'call', call)
    monkeypatch.setattr(module, 'CaptchaBrowser', lambda **_kw: Browser())
    assert worker.run() == 0
    assert solves[0] == (138, 10, module.CAPTCHA_HINT)
    if mode == 'cancelled':
        assert submissions == []
        assert state.job(job['id'])['status'] == 'cancelled'
        assert state.job(job['id'])['submit_attempts'] == 0
    else:
        assert len(submissions) == 1
        when, fields, profile = submissions[0]
        assert when == (150 if mode == 'success' else 156)
        assert fields['validate'] == ('early-private-result' if mode == 'success' else 'fresh-private-result')
        assert profile['token'] == ('secret-token' if mode == 'success' else 'updated-session')
        assert state.job(job['id'])['status'] == 'success'
        assert state.job(job['id'])['submit_attempts'] == 1
    logs = (worker.logs / 'events.jsonl').read_text(encoding='utf-8') + capsys.readouterr().out
    for private in ['early-private-result', 'fresh-private-result', 'secret-token', 'updated-session']:
        assert private not in logs


def test_worker_diagnostics_do_not_persist_browser_validation_results(state, tmp_path, monkeypatch, capsys):
    """Exercise real browser instrumentation against a local page, without CAPTCHA/signup APIs."""
    import zipfile
    import dm_bot_worker as module
    from dm_grab import DETAIL, INFO, SUBMIT
    marker = 'synthetic-captcha-for-trace-test'
    job, _ = state.add_job('42', '123', OWNER, OWNER, 'Activity', 0)
    state.claim(job['id'])
    worker = Worker(state, job['id'], tmp_path / 'logs')

    def call(_client, endpoint, _fields):
        if endpoint == DETAIL:
            return {'activityId': 123, 'joinstartdate': 0, 'ableJoinFlag': 1, 'signUpId': 0}
        if endpoint == INFO:
            return {'info': [], 'openCaptcha': True, 'captchaId': module.CAPTCHA_HINT}
        assert endpoint == SUBMIT
        return {'signUpId': '456'}

    def solve(browser, _cid, *, timeout=60):
        page = browser._context.new_page()
        try:
            page.evaluate('value => { window.__dmCaptchaResult = {validate: value}; }', marker)
            return page.evaluate('window.__dmCaptchaResult')
        finally:
            page.close()

    monkeypatch.setattr(module.DmClient, 'call', call)
    monkeypatch.setattr(module.CaptchaBrowser, 'prepare', lambda *_args, **_kw: None)
    monkeypatch.setattr(module.CaptchaBrowser, 'solve', solve)
    assert worker.run() == 0
    for path in worker.logs.rglob('*'):
        if not path.is_file():
            continue
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                content = b''.join(archive.read(name) for name in archive.namelist())
        else:
            content = path.read_bytes()
        assert marker.encode() not in content, f'validation persisted in {path.name}'
    assert marker not in capsys.readouterr().out
