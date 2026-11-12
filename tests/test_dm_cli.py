"""Protocol tests against independent fixtures emitted by the Android SDK."""
import base64
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import parse_qs, quote_plus

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import unpad
import pytest


CLI_PATH = Path(__file__).resolve().parents[1] / 'dm_cli.py'
spec = importlib.util.spec_from_file_location('dm_cli_under_test', CLI_PATH)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
FIXTURES = json.loads((Path(__file__).parent / 'fixtures' / 'dm-protocol-sdk.json').read_text(encoding='utf-8'))
PROTOCOL = [item for item in FIXTURES if item['kind'] == 'protocol']
BASE64_CASES = [item for item in FIXTURES if item['kind'] == 'base64']


@pytest.mark.parametrize('case', PROTOCOL)
def test_sign_token_reproduces_sdk_including_unicode_and_null(case):
    original = copy.deepcopy(case['params'])
    assert cli.sign_token(case['params']) == case['signToken']
    assert case['params'] == original


@pytest.mark.parametrize('case', PROTOCOL)
def test_canonical_json_matches_sdk_utf16_order_and_escaping(case):
    assert cli.canonical_json(case['params']) == case['serialized']


@pytest.mark.parametrize('case', BASE64_CASES)
def test_app_base64_preserves_sdk_spaces_at_complete_45_byte_blocks(case):
    assert cli.app_b64encode(bytes([case['inputByte']]) * case['size']) == case['encoded']


@pytest.mark.parametrize('case', PROTOCOL)
def test_request_has_only_d_and_embeds_rsa_after_aes(case):
    body = cli.pack_request(case['aesCiphertext'], case['rsaCiphertext'])
    assert parse_qs(body, keep_blank_values=True) == {'d': [case['wireValue']]}


@pytest.mark.parametrize('case', PROTOCOL)
def test_decode_request_accepts_captured_outer_d_envelope(case):
    wire = 'd=' + quote_plus(case['wireValue'], safe='')
    assert cli.decode_request(wire, case['key'].encode()) == case['requestPlaintext']


@pytest.mark.parametrize('case', PROTOCOL)
def test_prepare_payload_refreshes_timestamp_and_replaces_stale_signature(case):
    supplied = dict(case['params'], timestamp='old', signToken='STALE')
    original = copy.deepcopy(supplied)
    result = cli.prepare_payload(supplied, timestamp=case['params']['timestamp'])
    assert result == json.loads(case['requestPlaintext'])
    assert supplied == original


def test_response_decryption_uses_distinct_response_iv():
    case = PROTOCOL[0]
    assert cli.decode_response(case['responseCiphertext'], case['key'].encode()) == case['responsePlaintext']


def test_plain_json_response_is_preserved():
    raw = '{"code":100,"data":{"count":0}}'
    assert cli.decode_response(raw.encode(), b'abcdefghijklmnop') == raw


@pytest.fixture(scope='module')
def local_rsa_key():
    return RSA.generate(1024)


def test_rsa_wrap_is_pkcs1_v1_5_for_the_exact_aes_key(local_rsa_key):
    key = b'abcdefghijklmnop'
    wrapped = cli.rsa_wrap(key, local_rsa_key.public_key().export_key())
    ciphertext = base64.b64decode(wrapped, validate=True)
    assert len(ciphertext) == 128
    assert PKCS1_v1_5.new(local_rsa_key).decrypt(ciphertext, b'invalid') == key


def test_make_d_encrypts_key_inside_d_and_uses_request_iv(monkeypatch, local_rsa_key):
    case = PROTOCOL[0]
    monkeypatch.setitem(cli.CONFIG, 'rsa_pub_pem', local_rsa_key.public_key().export_key())
    payload = json.loads(case['requestPlaintext'])
    body = cli.make_d(payload, case['key'].encode())
    fields = parse_qs(body)
    assert list(fields) == ['d']
    packed = base64.b64decode(fields['d'][0]).decode('ascii')
    rsa_part = packed[-172:]
    aes_part = packed[:-172]
    assert PKCS1_v1_5.new(local_rsa_key).decrypt(base64.b64decode(rsa_part), b'invalid') == case['key'].encode()
    plain = unpad(AES.new(case['key'].encode(), AES.MODE_CBC, b'1628092121312213').decrypt(base64.b64decode(aes_part)), 16)
    assert plain.decode('utf-8') == case['requestPlaintext']


def test_aes_key_must_be_16_bytes_as_required_by_the_app():
    with pytest.raises(ValueError):
        cli.aes_enc(b'{}', b'x' * 24)


def test_enc_command_builds_a_fresh_signed_request():
    case = PROTOCOL[0]
    result = subprocess.run([sys.executable, str(CLI_PATH), 'enc', json.dumps(case['params']),
        '--key', case['key'], '--timestamp', case['params']['timestamp']], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    wire = next(line for line in result.stdout.splitlines() if line.startswith('d='))
    assert cli.decode_request(wire, case['key'].encode()) == case['requestPlaintext']


def test_dec_command_preserves_literal_plus_in_legacy_aes_value():
    result = subprocess.run([sys.executable, str(CLI_PATH), 'dec', 'abcdefghijklmnop',
        'd=ZyoW9r6yEh+OADVRtEcztQ=='], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '{"n":"0"}'


def test_dec_command_still_decodes_current_form_envelope():
    case = PROTOCOL[0]
    wire = 'd=' + quote_plus(case['wireValue'], safe='')
    result = subprocess.run([sys.executable, str(CLI_PATH), 'dec', case['key'], wire], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == case['requestPlaintext']
