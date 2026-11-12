"""Assert the actual emitted wire data against the Android capture."""
from contextlib import contextmanager
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import ssl
import threading
import time

from curl_cffi import CurlOpt
import pytest

import dm_grab as tool
from test_dm_tls import certificates, protocol

ROOT = Path(__file__).resolve().parents[1]
PROFILE = {'token': 'local-test-token', 'uid': '42', 'version': '5.0.0'}
UA = '{"version":"5.0.0"}'


@contextmanager
def captured_server(certificates):
    hellos, requests = [], []
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(certificates[1]), str(certificates[2]))
    context.set_alpn_protocols(['h2', 'http/1.1'])

    class Server(ThreadingHTTPServer):
        def get_request(self):
            sock, address = super().get_request()
            sock.settimeout(3)
            deadline = time.monotonic() + 3
            while True:
                hello = sock.recv(65536, socket.MSG_PEEK)
                if len(hello) >= 5 and len(hello) >= 5 + int.from_bytes(hello[3:5], 'big'):
                    hellos.append(hello[:5 + int.from_bytes(hello[3:5], 'big')])
                    break
                if not hello or time.monotonic() > deadline:
                    raise TimeoutError('Incomplete ClientHello')
                time.sleep(0.001)
            return context.wrap_socket(sock, server_side=True), address

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_POST(self):
            length = int(self.headers['Content-Length'])
            body = self.rfile.read(length)
            requests.append({'path': self.path, 'headerNames': list(self.headers),
                'alpn': self.connection.selected_alpn_protocol(), 'tlsVersion': self.connection.version(),
                'requestVersion': self.request_version, 'body': body})
            raw = gzip.compress(b'{"code":"100","data":{"ok":true}}')
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Content-Encoding', 'gzip')
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = Server(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, hellos, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def connect_locally(client, port):
    options = client._session.curl_options
    options[CurlOpt.RESOLVE] = [f'appdmkj.5idream.net:{port}:127.0.0.1']
    options[CurlOpt.PROXY] = ''


def test_app_profile_emits_captured_hello_and_http1_headers(certificates, protocol):
    module, _ = protocol
    with captured_server(certificates) as (port, hellos, requests):
        with tool.DmClient(module, PROFILE, UA, transport='curl', network_profile='app',
                           base_url=f'https://appdmkj.5idream.net:{port}', verify=str(certificates[0])) as client:
            connect_locally(client, port)
            assert client.call('/v3/user/unread', {}) == {'ok': True}
            assert client.call('/v3/user/unread', {}) == {'ok': True}
            assert client.last_transport_info['newConnections'] == 0
    expected = bytes.fromhex(json.loads((ROOT / 'network_profiles/dm_android16.json').read_text())['clientHelloHex'])
    assert len(hellos) == 1
    # The only changing field in these fresh TLS 1.2 hellos is the 32-byte random.
    assert hellos[0][:11] + hellos[0][43:] == expected[:11] + expected[43:]
    assert len(requests) == 2
    assert all(r['tlsVersion'] == 'TLSv1.2' and r['alpn'] is None for r in requests)
    assert all(r['requestVersion'] == 'HTTP/1.1' for r in requests)
    assert all(r['headerNames'] == ['standardUA', 'Content-Type', 'Content-Length', 'Host',
                                   'Connection', 'Accept-Encoding', 'User-Agent'] for r in requests)


def test_app_profile_rejects_untrusted_certificate(certificates, protocol):
    module, _ = protocol
    with captured_server(certificates) as (port, hellos, requests):
        with tool.DmClient(module, PROFILE, UA, transport='curl', network_profile='app',
                           base_url=f'https://appdmkj.5idream.net:{port}') as client:
            connect_locally(client, port)
            with pytest.raises(tool.NetworkError):
                client.call('/v3/user/unread', {})
    assert hellos
    assert requests == []


def test_app_profile_rejects_wrong_hostname_with_trusted_ca(certificates, protocol):
    module, _ = protocol
    with captured_server(certificates) as (port, hellos, requests):
        with tool.DmClient(module, PROFILE, UA, transport='curl', network_profile='app',
                           base_url=f'https://wrong.example:{port}', verify=str(certificates[0])) as client:
            client._session.curl_options.update({CurlOpt.RESOLVE: [f'wrong.example:{port}:127.0.0.1'], CurlOpt.PROXY: ''})
            with pytest.raises(tool.NetworkError):
                client.call('/v3/user/unread', {})
    assert hellos
    assert requests == []
