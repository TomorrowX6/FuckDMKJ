import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import urllib.parse
import getpass

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad
import pytest

import dm_grab

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'dm_login.py'
if MODULE.exists():
    spec = importlib.util.spec_from_file_location('dm_login', MODULE)
    login = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(login)
else:
    login = SimpleNamespace()

UA = json.dumps({'version': '5.0.0', 'jPushId': 'local-device', 'uuid': 'local-test-device'})


@pytest.mark.parametrize('password,expected', [
    ('password123', '8995DEF5C18EBD65FD1417716C994074'),
    (' Pass w0rd! ', 'CA2AF2BCB29571D2E40895B4B0AE937E'),
    ('中文😀', '55191F6E0A7D7349DBD76A3D47546A2B'),
    ('12345678', 'BDCD22CB084276EEACC8E8C1E0FE8D72'),
])
def test_password_encryption_matches_independent_jce_vectors(password, expected):
    assert login.encrypt_password(password) == expected


@contextmanager
def login_server(monkeypatch, reply):
    protocol = dm_grab.load_protocol(Path(__file__).resolve().parents[1] / 'dm_cli.py')
    private = RSA.generate(1024)
    monkeypatch.setitem(protocol.CONFIG, 'rsa_pub_pem', private.public_key().export_key())
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length'])).decode('ascii')
            aes, rsa = protocol.split_request(body)
            key = PKCS1_v1_5.new(private).decrypt(base64.b64decode(rsa), b'bad')
            payload = json.loads(protocol.decode_request(body, key))
            seen.append({'path': self.path, 'headers': dict(self.headers), 'payload': payload, 'body': body})
            value = reply(self.path, payload) if callable(reply) else reply
            raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
            encrypted = AES.new(key, AES.MODE_CBC, protocol.RESPONSE_IV).encrypt(pad(raw, 16))
            encoded = protocol.app_b64encode(encrypted).encode('ascii')
            self.send_response(200)
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    protocol.BASE = f'http://127.0.0.1:{server.server_port}'
    try:
        yield protocol, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_login_uses_signed_d_without_old_auth_and_saves_only_session_values(tmp_path, monkeypatch):
    reply = {'code': '100', 'data': {'token': 'fresh-token', 'uid': 42, 'name': 'Private Name', 'password': 'server-secret'}}
    destination = tmp_path / 'payload.json'
    old = b'{"uid":"old","token":"old-token"}\n'
    destination.write_bytes(old)
    with login_server(monkeypatch, reply) as (protocol, seen):
        with login.LoginClient(protocol, UA) as client:
            profile = login.login_and_save(client, 'test-account', 'password123', destination)
            assert client.last_transport_info['connectionPolicy'] == 'fresh_write'
        assert len(seen) == 1
        request = seen[0]
        assert request['path'] == '/v2/login/phone'
        assert set(urllib.parse.parse_qs(request['body'])) == {'d'}
        payload = request['payload']
        assert payload['account'] == 'test-account'
        assert payload['pwd'] == '8995DEF5C18EBD65FD1417716C994074'
        assert payload['version'] == '5.0.0' and payload['jPushId'] == 'local-device'
        assert 'uid' not in payload and 'token' not in payload
        assert payload['signToken'] == protocol.sign_token(payload)
        assert 'password123' not in request['body']
    assert profile == {'uid': '42', 'token': 'fresh-token', 'version': '5.0.0', 'jPushId': 'local-device'}
    assert json.loads(destination.read_text()) == profile
    backups = list(tmp_path.glob('payload.json.*.bak'))
    assert len(backups) == 1 and backups[0].read_bytes() == old
    assert all('password123' not in p.read_text() and 'server-secret' not in p.read_text() for p in tmp_path.iterdir())
    with dm_grab.DmClient(protocol, profile, UA) as consumer:
        assert consumer.build(dm_grab.DETAIL, {'activityId': '123'})['signedPayload']['token'] == 'fresh-token'


@pytest.mark.parametrize('reply', [
    {'code': '400', 'msg': '账号或密码不正确'},
    {'code': '100', 'data': {'uid': '42'}},
    {'code': '100', 'data': {'token': 'new-token', 'uid': False}},
    {'code': '100', 'data': {'token': False, 'uid': '42'}},
])
def test_login_failure_preserves_existing_session(tmp_path, monkeypatch, reply):
    path = tmp_path / 'payload.json'
    original = b'{"token":"keep-this","uid":"99"}'
    path.write_bytes(original)
    with login_server(monkeypatch, reply) as (protocol, seen):
        with login.LoginClient(protocol, UA) as client:
            with pytest.raises(dm_grab.EnrollmentError):
                login.login_and_save(client, 'test-account', 'password123', path)
    assert len(seen) == 1
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_interactive_login_hides_password_and_does_not_print_token(tmp_path, monkeypatch, capsys):
    ua_file, profile_file = tmp_path / 'standardua.json', tmp_path / 'payload.json'
    ua_file.write_text(UA, encoding='utf-8')
    prompts = []
    monkeypatch.setattr('builtins.input', lambda prompt: prompts.append(prompt) or 'test-account')
    monkeypatch.setattr('getpass.getpass', lambda prompt: prompts.append(prompt) or 'password123')
    with login_server(monkeypatch, {'code': '100', 'data': {'uid': '42', 'token': 'new-secret-token'}}) as (protocol, seen):
        monkeypatch.setattr(login, 'load_protocol', lambda _: protocol)
        assert login.main(['--standard-ua', str(ua_file), '--output', str(profile_file)]) == 0
    assert len(seen) == 1 and len(prompts) == 2
    output = capsys.readouterr()
    assert all(secret not in output.out + output.err for secret in ['password123', '8995DEF5C18EBD65FD1417716C994074', 'new-secret-token'])
    assert json.loads(profile_file.read_text())['token'] == 'new-secret-token'


def test_terminal_without_hidden_input_does_not_fall_back_to_echo(tmp_path, monkeypatch, capsys):
    ua = tmp_path / 'ua.json'
    ua.write_text(UA)
    output = tmp_path / 'payload.json'
    monkeypatch.setattr(login, 'load_protocol', lambda _: object())

    def unavailable(_prompt):
        raise getpass.GetPassWarning('No hidden terminal')

    monkeypatch.setattr('getpass.getpass', unavailable)
    assert login.main(['--account', 'local-account', '--standard-ua', str(ua), '--output', str(output)]) == 1
    assert not output.exists()
    assert 'PowerShell' in capsys.readouterr().err


def test_atomic_profile_replace_failure_preserves_old_file_and_removes_temporary(tmp_path, monkeypatch):
    path = tmp_path / 'payload.json'
    old = b'{"token":"old-token","uid":"42"}'
    path.write_bytes(old)

    def denied(self, target):
        raise PermissionError('Local filesystem denied replace')

    monkeypatch.setattr(Path, 'replace', denied)
    with pytest.raises(PermissionError):
        login.save_profile(path, {'token': 'new-token', 'uid': '99', 'version': '5.0.0'})
    assert path.read_bytes() == old
    assert not list(tmp_path.glob('*.tmp'))
    assert len(list(tmp_path.glob('payload.json.*.bak'))) == 1


def test_authenticated_client_still_requires_token_and_uid():
    with pytest.raises(dm_grab.EnrollmentError, match='token'):
        dm_grab.DmClient(object(), {'version': '5.0.0'}, UA)


@pytest.mark.parametrize('sender', [100000001, 77])
def test_bot_password_login_uses_generated_device_and_verifies_before_granting(tmp_path, monkeypatch, sender):
    from dm_bot import Bot
    from dm_bot_store import Store
    store = Store(tmp_path/'bot.sqlite3')
    store.grant_login(77)

    class Telegram:
        def __init__(self): self.sent,self.deleted=[],[]
        def send(self,chat_id,text): self.sent.append((chat_id,text))
        def delete(self,chat_id,message_id): self.deleted.append((chat_id,message_id))

    def reply(endpoint, payload):
        if endpoint == '/v2/login/phone':
            assert 'uid' not in payload and 'token' not in payload
            assert payload['pwd'] == login.encrypt_password('password123')
            return {'code':'100','data':{'uid':'42','token':'fresh-token'}}
        assert endpoint == '/v3/user/mine_homepage' and payload['uid']=='42'
        return {'code':'100','data':{'userId':'42'}}

    api = Telegram()
    with login_server(monkeypatch,reply) as (protocol,seen):
        bot = Bot(store,api,100000001,
                  password_clients=lambda ua: login.LoginClient(protocol,ua),
                  clients=lambda profile,ua: dm_grab.DmClient(protocol,profile,ua))
        bot.handle({'message':{'message_id':1,'text':'/login example-account password123',
                    'from':{'id':sender},'chat':{'id':sender,'type':'private'}}})
    assert len(seen)==2
    headers = [{key.lower():value for key,value in item['headers'].items()} for item in seen]
    ua = json.loads(headers[0]['standardua'])
    assert ua == json.loads(headers[1]['standardua'])
    assert ua['uuid'] and ua['device']=='OnePlus PHK110' and 'jPushId' not in ua
    assert 'jPushId' not in seen[0]['payload'] and 'jPushId' not in seen[1]['payload']
    assert store.account('42')[0]=={'uid':'42','token':'fresh-token','version':'5.0.0'}
    assert store.allowed('42',sender,100000001)
    assert not store.allowed('99',77,100000001)
    assert api.deleted==[(sender,1)] and '42' in api.sent[0][1]
    assert all('password123' not in text and 'fresh-token' not in text for _,text in api.sent)
    assert b'password123' not in (tmp_path/'bot.sqlite3').read_bytes()
