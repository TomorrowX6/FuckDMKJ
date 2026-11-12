import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import gzip
import ipaddress
import json
from pathlib import Path
import socketserver
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import DataReceived, RequestReceived, StreamEnded
import pytest

import dm_grab as tool


@pytest.fixture
def certificates(tmp_path):
    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Local transport test CA')])
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')]))
            .issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),
                           x509.DNSName('appdmkj.5idream.net'),
                           x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    ca_path, cert_path, key_path = [tmp_path / n for n in ('ca.pem', 'cert.pem', 'key.pem')]
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return ca_path, cert_path, key_path


@contextmanager
def tls_server(certificates, protocol, private, http2):
    seen, connections, negotiated = [], [], []
    dropped_submit = []

    def consume(body, headers):
        _, rsa = protocol.split_request(body.decode('ascii'))
        key = PKCS1_v1_5.new(private).decrypt(base64.b64decode(rsa), b'invalid')
        payload = json.loads(protocol.decode_request(body.decode('ascii'), key))
        seen.append({'headers': dict(headers), 'payload': payload, 'key': key})
        return gzip.compress(b'{"code":"100","data":{"ok":true}}')

    class H2Handler(socketserver.BaseRequestHandler):
        def handle(self):
            connections.append(self.client_address)
            negotiated.append(self.request.selected_alpn_protocol())
            self.request.settimeout(3)
            conn = H2Connection(config=H2Configuration(client_side=False, header_encoding='utf-8'))
            conn.initiate_connection()
            self.request.sendall(conn.data_to_send())
            streams = {}
            while True:
                try:
                    data = self.request.recv(65535)
                except (OSError, TimeoutError):
                    return
                if not data:
                    return
                for event in conn.receive_data(data):
                    if isinstance(event, RequestReceived):
                        streams[event.stream_id] = {'headers': event.headers, 'body': bytearray()}
                    elif isinstance(event, DataReceived):
                        streams[event.stream_id]['body'].extend(event.data)
                        conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                    elif isinstance(event, StreamEnded):
                        stream = streams.pop(event.stream_id)
                        raw = consume(bytes(stream['body']), stream['headers'])
                        conn.send_headers(event.stream_id, [(':status', '200'), ('content-type', 'application/json'),
                                          ('content-encoding', 'gzip'), ('content-length', str(len(raw)))])
                        conn.send_data(event.stream_id, raw, end_stream=True)
                pending = conn.data_to_send()
                if pending:
                    self.request.sendall(pending)

    class H1Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def setup(self):
            super().setup()
            connections.append(self.client_address)
            negotiated.append(self.request.selected_alpn_protocol())

        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            raw = consume(body, self.headers.items())
            if self.path == tool.SUBMIT and not dropped_submit:
                dropped_submit.append(True)
                self.close_connection = True
                return
            status = 429 if self.path == '/limit' else 302 if self.path == '/redirect' else 200
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Content-Length', str(len(raw) + (100 if self.path == '/truncated' else 0)))
            if self.path == '/redirect':
                self.send_header('Location', '/other')
            self.end_headers()
            self.wfile.write(raw)
            if self.path == '/truncated':
                self.close_connection = True

        def log_message(self, *_args):
            pass

    class H2Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = H2Server(('127.0.0.1', 0), H2Handler) if http2 else ThreadingHTTPServer(('127.0.0.1', 0), H1Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(certificates[1]), str(certificates[2]))
    context.set_alpn_protocols(['h2'] if http2 else ['http/1.1'])
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'https://127.0.0.1:{server.server_address[1]}', seen, connections, negotiated
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def protocol(monkeypatch):
    module = tool.load_protocol(Path(__file__).resolve().parents[1] / 'dm_cli.py')
    private = RSA.generate(1024)
    monkeypatch.setitem(module.CONFIG, 'rsa_pub_pem', private.public_key().export_key())
    return module, private


@pytest.mark.parametrize('http2', [True, False])
def test_curl_negotiates_tls_and_reuses_connection_with_valid_wire_payload(certificates, protocol, http2):
    crypto, private = protocol
    with tls_server(certificates, crypto, private, http2) as (url, seen, connections, negotiated):
        with tool.DmClient(crypto, {'uid': '1', 'token': 'test'}, '{}', base_url=url,
                           transport='curl', verify=str(certificates[0])) as client:
            assert client.call(tool.DETAIL, {'activityId': '123'}) == {'ok': True}
            first = dict(client.last_transport_info)
            assert client.call(tool.DETAIL, {'activityId': '456'}) == {'ok': True}
            second = dict(client.last_transport_info)
        assert len(connections) == 1
        assert negotiated == ['h2' if http2 else 'http/1.1']
        assert first['newConnections'] == 1 and second['newConnections'] == 0
        assert first['httpVersion'] == ('HTTP/2' if http2 else 'HTTP/1.1')
        assert len(seen) == 2
        assert [item['payload']['activityId'] for item in seen] == ['123', '456']
        assert all(crypto.sign_token(item['payload']) == item['payload']['signToken'] for item in seen)
        assert seen[0]['key'] != seen[1]['key']
        headers = {key.lower(): value for key, value in seen[0]['headers'].items()}
        assert headers['standardua'] == '{}'
        assert headers['user-agent'] == 'okhttp/3.11.0'
        assert 'sec-ch-ua' not in headers


@pytest.mark.parametrize('path,exception', [('/limit', tool.BusinessError),
                                          ('/truncated', tool.NetworkError),
                                          ('/redirect', tool.NetworkError)])
def test_curl_maps_errors_without_automatically_replaying_post(certificates, protocol, path, exception):
    crypto, private = protocol
    with tls_server(certificates, crypto, private, False) as (url, seen, _connections, _negotiated):
        with tool.DmClient(crypto, {'uid': '1', 'token': 'test'}, '{}', base_url=url,
                           transport='curl', verify=str(certificates[0])) as client:
            with pytest.raises(exception):
                client.call(path, {'activityId': '123'})
        assert len(seen) == 1


def test_curl_rejects_untrusted_certificate(certificates, protocol):
    crypto, private = protocol
    with tls_server(certificates, crypto, private, False) as (url, seen, _connections, _negotiated):
        with tool.DmClient(crypto, {'uid': '1', 'token': 'test'}, '{}', base_url=url,
                           transport='curl') as client:
            with pytest.raises(tool.NetworkError):
                client.call('/detail', {'activityId': '123'})
        assert seen == []


@pytest.mark.parametrize('network_profile', ['generic', 'app'])
def test_lost_submit_response_is_not_replayed_after_a_pooled_read(certificates, protocol, network_profile):
    crypto, private = protocol
    with tls_server(certificates, crypto, private, False) as (url, seen, connections, _negotiated):
        with tool.DmClient(crypto, {'uid': '1', 'token': 'test'}, '{}', base_url=url,
                           transport='curl', verify=str(certificates[0]), network_profile=network_profile) as client:
            assert client.call(tool.DETAIL, {'activityId': '123'}) == {'ok': True}
            with pytest.raises(tool.NetworkError):
                client.call(tool.SUBMIT, {'activityId': '123'})
        assert len(seen) == 2  # One read, exactly one received submission.
        assert len(connections) == 2


@pytest.mark.parametrize('network_profile', ['generic', 'app'])
def test_unknown_post_endpoint_is_not_put_on_the_read_connection_pool(certificates, protocol, network_profile):
    crypto, private = protocol
    with tls_server(certificates, crypto, private, False) as (url, seen, connections, _negotiated):
        with tool.DmClient(crypto, {'uid': '1', 'token': 'test'}, '{}', base_url=url,
                           transport='curl', verify=str(certificates[0]), network_profile=network_profile) as client:
            client.call('/custom/write', {'activityId': '123'})
            client.call('/custom/write', {'activityId': '456'})
        assert len(seen) == len(connections) == 2
