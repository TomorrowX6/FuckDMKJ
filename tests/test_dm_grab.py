import copy
import gzip
import importlib.util
import json
import http.client
from pathlib import Path
from types import SimpleNamespace
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA
import pytest


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / 'dm_grab.py'
if PATH.exists():
    spec = importlib.util.spec_from_file_location('dm_grab', PATH)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
else:
    tool = SimpleNamespace()


def detail(**changes):
    value = dict(activityId=123, activityName='测试活动', signUpId=0,
                 joinstartdate=100000, joinenddate=200000, buttonType=3,
                 ableJoinFlag=1, joinNum=0, joinmaxnum='10')
    value.update(changes)
    return value


class Clock:
    def __init__(self, now=110):
        self.now = now
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert seconds > 0
        self.sleeps.append(seconds)
        self.now += seconds


class Client:
    def __init__(self, replies):
        self.replies = {key: list(values) for key, values in replies.items()}
        self.calls = []

    def call(self, endpoint, fields):
        self.calls.append((endpoint, copy.deepcopy(fields)))
        value = self.replies[endpoint].pop(0)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


def runner(client, clock=None, **kwargs):
    clock = clock or Clock()
    return tool.EnrollmentRunner(client, clock=clock, sleep=clock.sleep,
                                 emit=lambda *_args, **_kw: None, **kwargs)


def replies(submit, activity=None, form=None, result=None):
    return {'/v2/activity/detail': [activity or detail()],
            '/v2/signup/info': [form or {'info': []}],
            '/v2/signup/submit': submit,
            '/v2/signup/result': result or []}


def test_future_activity_waits_even_when_button_has_not_opened():
    value = detail(joinstartdate=120000, buttonType=2, ableJoinFlag=0,
                   unableJoinReason='报名尚未开始')
    assert tool.assess_detail(value, 110000) == 'waiting'


def test_future_activity_does_not_ignore_college_restriction():
    value = detail(joinstartdate=120000, buttonType=2, ableJoinFlag=0,
                   unableJoinReason='不在活动规定的院系内')
    with pytest.raises(tool.EnrollmentError, match='院系'):
        tool.assess_detail(value, 110000)


@pytest.mark.parametrize('changes,reason', [
    ({'joinenddate': 105000}, '结束'),
    ({'isCancel': True}, '取消'),
    ({'joinNum': 10}, '满'),
    ({'buttonType': 9, 'ableJoinFlag': 0}, '报名'),
])
def test_terminal_activity_states_never_submit(changes, reason):
    client = Client({'/v2/activity/detail': [detail(**changes)]})
    with pytest.raises(tool.EnrollmentError, match=reason):
        runner(client).run('123')
    assert [path for path, _ in client.calls] == ['/v2/activity/detail']


def test_existing_registration_exits_without_new_submission():
    client = Client({'/v2/activity/detail': [detail(signUpId='456')]})
    result = runner(client).run('123')
    assert result['status'] == 'success'
    assert result['signUpId'] == '456'
    assert len(client.calls) == 1


def test_required_fields_use_conent_and_preserve_server_metadata():
    form = {'info': [{'fullid': 'a', 'title': '姓名', 'notNull': True,
                      'key': 1, 'content': '说明'},
                     {'fullid': 'b', 'title': '选填', 'notNull': False}]}
    original = copy.deepcopy(form)
    fields = tool.prepare_fields(form, {'a': '张三'}, prompt=None)
    assert fields[0]['conent'] == '张三'
    assert fields[0]['content'] == '说明'
    assert fields[1]['conent'] == ''
    assert form == original


def test_missing_required_answer_is_reported_before_submission():
    with pytest.raises(tool.EnrollmentError, match='姓名'):
        tool.prepare_fields({'info': [{'title': '姓名', 'notNull': True}]}, {}, prompt=None)


def test_optional_fields_and_remark_are_empty_in_submitted_payload():
    form = {'info': [
        {'fullid': 'name', 'title': '姓名', 'notNull': True},
        {'fullid': 'note', 'title': '备注', 'notNull': False,
         'conent': '旧备注', 'content': '可选说明'},
        {'fullid': 'extra', 'title': '其他信息', 'conent': '预填内容'},
    ], 'remark': '接口返回的备注'}
    original = copy.deepcopy(form)
    client = Client(replies([{'signUpId': '456'}], form=form))

    def unexpected_prompt(_message):
        pytest.fail('非必填项不应提示填写')

    result = runner(client).run('123', answers={'name': '张三', 'note': '忽略此项'},
                                remark='旧命令参数', prompt=unexpected_prompt)
    assert result['status'] == 'success'
    submitted = [fields for path, fields in client.calls if path == tool.SUBMIT]
    assert len(submitted) == 1
    assert submitted[0]['remark'] == ''
    fields = json.loads(submitted[0]['data'])
    assert fields[0]['conent'] == '张三'
    assert fields[1]['conent'] == fields[2]['conent'] == ''
    assert fields[1]['content'] == '可选说明'
    assert form == original


def test_waits_for_open_time_and_stops_after_one_success():
    clock = Clock(110)
    client = Client(replies([{'signUpId': '456', 'getResult': False}],
                            activity=detail(joinstartdate=112000, ableJoinFlag=0,
                                            buttonType=2, unableJoinReason='未开始')))
    result = runner(client, clock).run('123')
    assert result['status'] == 'success'
    assert clock.now >= 112
    assert sum(path == '/v2/signup/submit' for path, _ in client.calls) == 1
    submitted = [value for path, value in client.calls if path == '/v2/signup/submit'][0]
    assert submitted['activityId'] == '123'
    assert json.loads(submitted['data']) == []
    assert submitted['useNumber'] == '0'


def test_form_hidden_before_opening_is_loaded_after_waiting():
    clock = Clock(110)
    values = replies([{'signUpId': '456'}],
                     activity=detail(joinstartdate=112000, ableJoinFlag=0,
                                     buttonType=2, unableJoinReason='报名未开始'))
    values['/v2/signup/info'] = [tool.BusinessError('11111', '报名未开始，请稍后重试'),
                                {'info': []}]
    client = Client(values)
    assert runner(client, clock).run('123')['status'] == 'success'
    assert clock.now >= 112
    assert [path for path, _ in client.calls].count('/v2/signup/info') == 2
    assert [path for path, _ in client.calls].count('/v2/signup/submit') == 1


def test_dry_run_does_not_wait_if_form_is_still_hidden():
    clock = Clock(110)
    values = replies([], activity=detail(joinstartdate=112000))
    values['/v2/signup/info'] = [tool.BusinessError('11111', '报名未开始，请稍后重试')]
    client = Client(values)
    with pytest.raises(tool.EnrollmentError, match='尚未开放|未开始'):
        runner(client, clock).run('123', dry_run=True)
    assert clock.sleeps == []
    assert not any(path == '/v2/signup/submit' for path, _ in client.calls)


def test_pending_submission_polls_result_without_resubmitting():
    client = Client(replies([{'signUpId': 'queue1', 'getResult': True}], result=[
        {'success': True, 'waitingTime': 600},
        {'success': True, 'waitingTime': 0, 'joinId': '456'}]))
    result = runner(client).run('123')
    assert result['signUpId'] == '456'
    assert [path for path, _ in client.calls].count('/v2/signup/submit') == 1
    assert [fields for path, fields in client.calls if path == '/v2/signup/result'] == [
        {'signUpId': 'queue1'}, {'signUpId': 'queue1'}]


def test_business_envelope_success_is_not_registration_success():
    client = Client(replies([{}]))
    with pytest.raises(tool.EnrollmentError, match='确认|标识|结果'):
        runner(client).run('123')


def test_explicit_failure_with_id_and_without_reason_is_not_success():
    client = Client(replies([{'signUpId': '456', 'success': False}]))
    with pytest.raises(tool.EnrollmentError, match='失败'):
        runner(client).run('123')


@pytest.mark.parametrize('value', [False, True, [], {}, 0, 'false'])
def test_invalid_registration_identifiers_are_not_existing_registrations(value):
    assert tool.assess_detail(detail(signUpId=value), 110000) == 'ready'


def test_captcha_preview_matches_actual_use_number(tmp_path):
    protocol = tool.load_protocol(Path(__file__).resolve().parents[1] / 'dm_cli.py')
    client = tool.DmClient(protocol, {'token': 'test', 'uid': '1'}, '{}')
    client.call = lambda path, _fields: detail() if path == '/v2/activity/detail' else {'info': []}
    out = tmp_path / 'preview.json'
    result = runner(client).run('123', validate='solved', dry_run=True, preview_path=out)
    preview = json.loads(out.read_text(encoding='utf-8'))
    payload = json.loads(protocol.decode_request(preview['body'], preview['key'].encode()))
    assert result['status'] == 'dry_run'
    assert payload['validate'] == 'solved'
    assert payload['useNumber'] == '1'


@pytest.mark.parametrize('failure', ['incomplete_http', 'truncated_gzip'])
def test_truncated_response_is_treated_as_uncertain_network_result(monkeypatch, failure):
    protocol = tool.load_protocol(Path(__file__).resolve().parents[1] / 'dm_cli.py')
    client = tool.DmClient(protocol, {'token': 'test', 'uid': '1'}, '{}')

    class Response:
        headers = {'Content-Encoding': 'gzip'}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self):
            if failure == 'incomplete_http':
                raise http.client.IncompleteRead(b'partial', 100)
            return gzip.compress(b'{"code":"100"}')[:-6]

    monkeypatch.setattr(tool.urllib.request, 'urlopen', lambda *_a, **_k: Response())
    with pytest.raises(tool.NetworkError):
        client.call('/v2/signup/submit', {'activityId': '123'})


def test_result_rejection_keeps_server_reason():
    client = Client(replies([{'signUpId': 'queue1', 'getResult': True}], result=[
        {'success': False, 'reason': '报名人数已满'}]))
    with pytest.raises(tool.EnrollmentError, match='已满'):
        runner(client).run('123')


def test_not_open_business_response_retries_a_bounded_number_of_times():
    client = Client(replies([tool.BusinessError('201', '报名未开始'),
                             {'signUpId': '456', 'getResult': False}]))
    assert runner(client).run('123')['status'] == 'success'
    assert [path for path, _ in client.calls].count('/v2/signup/submit') == 2


def test_retry_limit_is_enforced():
    client = Client(replies([tool.BusinessError('201', '系统繁忙')] * 2))
    with pytest.raises(tool.EnrollmentError, match='重试|次数'):
        runner(client, max_attempts=2).run('123')
    assert [path for path, _ in client.calls].count('/v2/signup/submit') == 2


def test_captcha_is_required_instead_of_sending_an_incomplete_request():
    client = Client(replies([], form={'info': [], 'openCaptcha': True, 'captchaId': 'test'}))
    with pytest.raises(tool.EnrollmentError, match='验证码|验证'):
        runner(client).run('123')
    assert not any(path == '/v2/signup/submit' for path, _ in client.calls)


def test_provided_validation_is_included_in_submission():
    client = Client(replies([{'signUpId': '456'}],
                            form={'info': [], 'openCaptcha': True, 'captchaId': 'test'}))
    runner(client).run('123', validate='solved-value')
    sent = [value for path, value in client.calls if path == '/v2/signup/submit'][0]
    assert sent['validate'] == 'solved-value'


def test_automatic_captcha_runs_after_open_and_preserves_verification_telemetry():
    clock = Clock(110)
    values = replies([{'signUpId': '456'}],
                     activity=detail(joinstartdate=112000),
                     form={'info': [], 'openCaptcha': True, 'captchaId': 'real-id'})
    client = Client(values)
    moments = []

    def solve(captcha_id):
        moments.append((captcha_id, clock.now))
        return {'validate': 'sdk-wrapped-value', 'useNumber': '2', 'useNumberDate': '100,101'}

    assert runner(client, clock, captcha_solver=solve).run('123')['status'] == 'success'
    assert moments == [('real-id', 112)]
    sent = [value for path, value in client.calls if path == '/v2/signup/submit'][0]
    assert sent['validate'] == 'sdk-wrapped-value'
    assert sent['useNumber'] == '2'
    assert sent['useNumberDate'] == '100,101'


def test_resource_preparation_finishes_before_open_without_oversleeping():
    clock = Clock(110)
    values = replies([{'signUpId': '456'}],
                     activity=detail(joinstartdate=150000),
                     form={'info': [], 'openCaptcha': True, 'captchaId': 'real-id'})
    client = Client(values)
    moments = []

    class Solver:
        def prepare(self, captcha_id=None):
            moments.append(('prepare', captcha_id, clock.now))
            clock.now += 5

        def __call__(self, captcha_id):
            moments.append(('solve', captcha_id, clock.now))
            return {'validate': 'new-token'}

    assert runner(client, clock, captcha_solver=Solver()).run('123')['status'] == 'success'
    assert any(kind == 'prepare' and at < 150 for kind, _cid, at in moments)
    assert moments[-1] == ('solve', 'real-id', 150)


def test_last_second_preparation_is_skipped_to_avoid_delaying_open_time():
    clock = Clock(110)
    prepared = []

    def prepare():
        prepared.append(True)
        clock.now += 5

    runner(Client({}), clock).wait_until(112, prepare=prepare)
    assert prepared == []
    assert clock.now == 112


def test_failed_automatic_captcha_never_submits():
    client = Client(replies([], form={'info': [], 'openCaptcha': True, 'captchaId': 'real-id'}))

    def solve(_captcha_id):
        raise RuntimeError('滑块验证失败')

    with pytest.raises(tool.EnrollmentError, match='验证失败'):
        runner(client, captcha_solver=solve).run('123')
    assert not any(path == '/v2/signup/submit' for path, _ in client.calls)


def test_captcha_solver_cannot_override_target_activity():
    client = Client(replies([{'signUpId': '456'}],
                            form={'info': [], 'openCaptcha': True, 'captchaId': 'real-id'}))
    solve = lambda _cid: {'validate': 'solved', 'activityId': '999', 'useNumber': '1'}
    runner(client, captcha_solver=solve).run('123')
    sent = [value for path, value in client.calls if path == '/v2/signup/submit'][0]
    assert sent['activityId'] == '123'


@pytest.mark.parametrize('manual', [False, True])
def test_interactive_cli_accepts_multiple_ids_and_closes_shared_browser(tmp_path, monkeypatch, capsys, manual):
    import dm_captcha
    profile = tmp_path / 'profile.json'
    standard = tmp_path / 'ua.json'
    profile.write_text('{"uid":"1","token":"test"}', encoding='utf-8')
    standard.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(tool, 'ROOT', tmp_path)
    monkeypatch.setattr(tool.sys.stdin, 'isatty', lambda: True)
    inputs = iter(['123', '456', ''])
    monkeypatch.setattr('builtins.input', lambda _prompt='': next(inputs))
    seen = []

    def call(_self, path, fields):
        assert path == '/v2/activity/detail'
        seen.append(fields['activityId'])
        return detail(activityId=int(fields['activityId']), signUpId='registered-' + fields['activityId'])

    monkeypatch.setattr(tool.DmClient, 'call', call)
    browsers = []

    class Browser:
        def __init__(self, **_kwargs):
            self.closed = False
            browsers.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

        def prepare(self, _captcha_id=None):
            pass

    monkeypatch.setattr(dm_captcha, 'CaptchaBrowser', Browser)
    args = ['--profile', str(profile), '--standard-ua', str(standard)]
    if manual:
        args.append('--manual-captcha')
    assert tool.main(args) == 0
    assert seen == ['123', '456']
    assert capsys.readouterr().out.count('已经报名') == 2
    assert len(browsers) == (0 if manual else 1)
    assert all(browser.closed for browser in browsers)


def test_lost_submit_response_reconciles_without_resubmitting():
    values = replies([tool.NetworkError('timeout')])
    values['/v2/activity/detail'].append(detail(signUpId='456'))
    client = Client(values)
    assert runner(client).run('123')['signUpId'] == '456'
    assert [path for path, _ in client.calls].count('/v2/signup/submit') == 1


def test_unconfirmed_submit_timeout_does_not_trigger_a_duplicate():
    values = replies([tool.NetworkError('timeout')])
    values['/v2/activity/detail'].extend([detail()] * 10)
    client = Client(values)
    with pytest.raises(tool.EnrollmentError, match='不确定|未确认'):
        runner(client, result_timeout=2).run('123')
    assert [path for path, _ in client.calls].count('/v2/signup/submit') == 1


def test_poll_timeout_is_bounded():
    client = Client(replies([{'signUpId': 'queue1', 'getResult': True}],
                            result=[{'success': True, 'waitingTime': 600}] * 10))
    with pytest.raises(tool.EnrollmentError, match='超时|未确认'):
        runner(client, result_timeout=1).run('123')


@pytest.mark.parametrize('value', ['0', '-1', 'abc', '123/submit'])
def test_rejects_invalid_activity_id_before_network(value):
    client = Client({})
    with pytest.raises(tool.EnrollmentError, match='ID'):
        runner(client).run(value)
    assert client.calls == []


def test_real_http_transport_sends_valid_encrypted_signed_payload_and_reads_gzip(monkeypatch):
    protocol = tool.load_protocol(Path(__file__).resolve().parents[1] / 'dm_cli.py')
    private = RSA.generate(1024)
    monkeypatch.setitem(protocol.CONFIG, 'rsa_pub_pem', private.public_key().export_key())
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length'])).decode('ascii')
            _, rsa = protocol.split_request(body)
            import base64
            key = PKCS1_v1_5.new(private).decrypt(base64.b64decode(rsa), b'bad')
            payload = json.loads(protocol.decode_request(body, key))
            seen.append((self.path, dict(self.headers), payload, key))
            raw = gzip.compress(json.dumps({'code': '100', 'data': detail()}).encode())
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = tool.DmClient(protocol, {'token': 'test', 'uid': '1', 'version': '5.0.0'},
                               '{"device":"test"}', base_url=f'http://127.0.0.1:{server.server_port}')
        assert client.call('/v2/activity/detail', {'activityId': '123'})['activityId'] == 123
        client.call('/v2/activity/detail', {'activityId': '123'})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert len(seen) == 2
    path, headers, payload, key = seen[0]
    assert path == '/v2/activity/detail'
    assert headers['Standardua'] == '{"device":"test"}'
    assert payload['token'] == 'test' and payload['uid'] == '1'
    assert protocol.sign_token(payload) == payload['signToken']
    assert payload['timestamp'].isdigit()
    assert key != seen[1][3]
