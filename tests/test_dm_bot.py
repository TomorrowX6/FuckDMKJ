from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from dm_bot import Bot, Scheduler, Telegram, TelegramError, parse_login, lock_instance
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
    ('jPushId', None), ('jPushId', ''), ('jPushId', False), ('jPushId', 123),
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


def test_nonowner_cannot_import_or_grant_and_secret_message_is_deleted(state):
    api = API()
    bot = Bot(state, api, OWNER, clients=lambda *_: pytest.fail('No API calls allowed'))
    bot.handle(message('/login 99 stolen-token {}', sender=77))
    bot.handle(message('/Permissions 42 77', sender=77))
    assert not state.allowed('42', 77, OWNER)
    assert api.deleted == [(77, 123)]
    assert all('主人' in text and 'stolen-token' not in text for _, text in api.sent)


def test_delegated_uid_permissions_apply_before_any_api_request(state):
    api = API()
    bot = Bot(state, api, OWNER, clients=lambda *_: pytest.fail('Unauthorized API call'))
    for command in ['/me 42', '/add 42 123', '/cancel 42 123']:
        bot.handle(message(command, sender=77))
    assert len(api.sent) == 3 and all('没有' in text for _, text in api.sent)
    bot.handle(message('/Permissions 42 77'))
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


def test_worker_prewarms_slow_server_connection_before_final_seconds(state, tmp_path, monkeypatch):
    import dm_bot_worker as module
    job, _ = state.add_job('42', '123', OWNER, OWNER, 'Activity', 110)
    state.claim(job['id'])
    worker = Worker(state, job['id'], tmp_path / 'logs')
    worker.job = state.job(job['id'])
    clock = SimpleNamespace(now=97.0)
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
    worker.sleep(13)
    assert len(observed) == 1
    assert observed[0][0] <= 99
    assert 8 <= observed[0][1] <= 9
    assert clock.now == pytest.approx(110)
    assert worker.client.timeout == 10
