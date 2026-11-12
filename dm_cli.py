#!/usr/bin/env python3
"""到梦空间 5.0.0 CLI，协议已对照运行时 SDK 验证。

请求只有 d 字段，没有单独的 k 字段：
  d = Base64_app(AES_Base64_app(签名后的排序 JSON) + RSA_Base64(AES_KEY))
RSA 使用 1024 位公钥和 PKCS#1 v1.5；RSA 字符串是末尾的 172 个字符。
Base64_app 在每完整 45 个输入字节后插入空格。

signToken = MD5(SHA512(排序 JSON 的 UTF-8).hexdigest()[1::4]).hexdigest().upper()
签名前去除 signToken，空值转空字符串，键按 Java UTF-16 字符串顺序排序。
请求 IV: 1628092121312213；响应解密工具的 IV: 9618953120112110。

依赖：pycryptodome。enc/call 自动更新时间戳和签名；sign 只计算签名。
JSON 参数可写成 @文件路径，避免将账号参数放进命令行历史。
"""
import argparse
import base64
from collections.abc import Mapping
import hashlib
import json
import math
from pathlib import Path
import secrets
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad


REQUEST_IV = b'1628092121312213'
RESPONSE_IV = b'9618953120112110'
IV = REQUEST_IV  # 保留旧脚本的导入名称。
BASE = 'https://appdmkj.5idream.net'
RSA_MODULUS = int(
    '9eed813259ad13f963176bcab53d04a8d8dd79e85cfa73588dd162cc9b9747d4a'
    '05d1a8fd11ce7cb38186729bfdd82f6b24141df2b3b021f0d0f9bca4d1120e1'
    '1554e5fd79dd8373b8025f8bf17a03e463dea876bc4862e8ad19cb38f864ea74'
    'e4aa53ef15735801dc8fcd3e98e75ef5133d50d8515c5b88d40d3a39b055f6ab', 16)
RSA_EXPONENT = 65537
CONFIG = {
    'rsa_modulus': RSA_MODULUS,
    'rsa_exponent': RSA_EXPONENT,
    'rsa_pub_pem': RSA.construct((RSA_MODULUS, RSA_EXPONENT)).export_key().decode('ascii'),
    'payload_field': 'd',
    'key_field': None,  # RSA 密文位于 d 内部，不能再追加独立字段。
    'request_iv': REQUEST_IV.decode('ascii'),
    'response_iv': RESPONSE_IV.decode('ascii'),
}


def normalize_params(payload):
    """Match the app's Map<String, String> inputs, including null -> empty."""
    if not isinstance(payload, Mapping):
        raise ValueError('参数必须是 JSON 对象')
    result = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            raise ValueError('参数名必须是字符串')
        if value is None:
            value = ''
        elif isinstance(value, bool):
            value = 'true' if value else 'false'
        elif isinstance(value, (str, int)):
            value = str(value)
        elif isinstance(value, float) and math.isfinite(value):
            value = str(value)
        else:
            raise ValueError(f'参数 {key!r} 必须是标量；嵌套 JSON 请先转成字符串')
        result[key] = value
    return result


def canonical_json(payload):
    values = normalize_params(payload)
    ordered = {key: values[key] for key in sorted(values, key=lambda value: value.encode('utf-16-be', 'surrogatepass'))}
    text = json.dumps(ordered, ensure_ascii=False, separators=(',', ':'))
    return text.replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')


def sign_token(payload):
    values = normalize_params(payload)
    values.pop('signToken', None)
    sha512_hex = hashlib.sha512(canonical_json(values).encode('utf-8')).hexdigest()
    selected = sha512_hex[1::4]
    return hashlib.md5(selected.encode('ascii')).hexdigest().upper()


def prepare_payload(payload, timestamp=None):
    values = normalize_params(payload)
    values.pop('signToken', None)
    values['timestamp'] = str(int(time.time() * 1000) if timestamp is None else timestamp)
    values['signToken'] = sign_token(values)
    return values


def rand_key():
    alphabet = string.ascii_lowercase + string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(16)).encode('ascii')


def _check_key(key):
    if not isinstance(key, bytes) or len(key) != 16:
        raise ValueError('AES KEY 必须正好为 16 字节')


def aes_enc(plain, key, iv=REQUEST_IV):
    _check_key(key)
    return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plain, 16))


def aes_dec(ciphertext, key, iv=REQUEST_IV):
    _check_key(key)
    return unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext), 16)


def app_b64encode(data):
    encoded = base64.b64encode(data).decode('ascii')
    blocks = len(data) // 45
    return ''.join(encoded[index * 60:(index + 1) * 60] + ' ' for index in range(blocks)) + encoded[blocks * 60:]


def app_b64decode(value):
    raw = value.encode('ascii') if isinstance(value, str) else value
    compact = bytes(byte for byte in raw if byte > 32)
    return base64.b64decode(compact, validate=True)


def _public_key(public_key=None):
    value = CONFIG['rsa_pub_pem'] if public_key is None else public_key
    return value if isinstance(value, RSA.RsaKey) else RSA.import_key(value)


def rsa_wrap(key, public_key=None):
    _check_key(key)
    encrypted = PKCS1_v1_5.new(_public_key(public_key)).encrypt(key)
    return base64.b64encode(encrypted).decode('ascii')


def pack_request(aes_ciphertext, rsa_ciphertext):
    """Combine the two ASCII Base64 strings before the outer encoding."""
    if not app_b64decode(aes_ciphertext) or len(app_b64decode(aes_ciphertext)) % 16:
        raise ValueError('AES 密文长度错误')
    rsa_ciphertext = ''.join(rsa_ciphertext.split())
    if len(app_b64decode(rsa_ciphertext)) != _public_key().size_in_bytes():
        raise ValueError('RSA 密文长度与公钥不匹配')
    combined = (aes_ciphertext + rsa_ciphertext).encode('ascii')
    return urllib.parse.urlencode({CONFIG['payload_field']: app_b64encode(combined)}, quote_via=urllib.parse.quote, safe='')


def make_d(payload, key):
    raw = payload if isinstance(payload, str) else canonical_json(payload)
    ciphertext = app_b64encode(aes_enc(raw.encode('utf-8'), key))
    return pack_request(ciphertext, rsa_wrap(key))


def _wire_value(value):
    text = value.strip()
    field = CONFIG['payload_field']
    if text.startswith(field + '='):
        values = urllib.parse.parse_qs(text, keep_blank_values=True).get(field, [])
        if len(values) != 1:
            raise ValueError('请求必须包含一个 d 字段')
        return values[0]
    return urllib.parse.unquote(text) if '%' in text else text


def split_request(value):
    combined = app_b64decode(_wire_value(value)).decode('ascii')
    rsa_bytes = _public_key().size_in_bytes()
    rsa_chars = ((rsa_bytes + 2) // 3) * 4
    if len(combined) <= rsa_chars:
        raise ValueError('不是完整的 d 请求封装')
    aes_part, rsa_part = combined[:-rsa_chars], combined[-rsa_chars:]
    if len(app_b64decode(rsa_part)) != rsa_bytes:
        raise ValueError('请求中的 RSA 密文长度错误')
    aes_bytes = app_b64decode(aes_part)
    if not aes_bytes or len(aes_bytes) % 16:
        raise ValueError('请求中的 AES 密文长度错误')
    return aes_part, rsa_part


def decode_request(value, key):
    aes_part, _ = split_request(value)
    return aes_dec(app_b64decode(aes_part), key, REQUEST_IV).decode('utf-8')


def decode_response(value, key):
    text = value.decode('utf-8') if isinstance(value, bytes) else value
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return aes_dec(app_b64decode(text), key, RESPONSE_IV).decode('utf-8')
    return text


def _read_argument(value):
    return Path(value[1:]).read_text(encoding='utf-8-sig') if value.startswith('@') else value


def _payload(value):
    result = json.loads(_read_argument(value))
    if not isinstance(result, dict):
        raise ValueError('参数必须是 JSON 对象')
    return result


def cmd_dec(args):
    key = args.key.encode('utf-8')
    value = _read_argument(args.value).strip()
    if args.response:
        plain = decode_response(value, key)
    else:
        try:
            aes_part, _ = split_request(value)
        except (ValueError, UnicodeError):
            # Legacy dec accepted a raw d=<AES Base64> value. Unlike form
            # decoding, unquote preserves a literal '+' inside that Base64.
            legacy = value
            prefix = CONFIG['payload_field'] + '='
            if legacy.startswith(prefix):
                legacy = legacy[len(prefix):]
            legacy = urllib.parse.unquote(legacy)
            plain = aes_dec(app_b64decode(legacy), key).decode('utf-8')
        else:
            plain = aes_dec(app_b64decode(aes_part), key).decode('utf-8')
    print(plain)


def cmd_enc(args):
    key = args.key.encode('utf-8') if args.key else rand_key()
    payload = _payload(args.json)
    if not args.no_sign:
        payload = prepare_payload(payload, timestamp=args.timestamp)
    print('KEY =', key.decode('utf-8'))
    print(make_d(payload, key))


def cmd_sign(args):
    payload = _payload(args.json)
    if args.timestamp is not None:
        payload['timestamp'] = str(args.timestamp)
    print(sign_token(payload))


def cmd_call(args):
    key = args.key.encode('utf-8') if args.key else rand_key()
    payload = prepare_payload(_payload(args.json), timestamp=args.timestamp)
    body = make_d(payload, key)
    url = args.endpoint if args.endpoint.startswith(('http://', 'https://')) else BASE + '/' + args.endpoint.lstrip('/')
    headers = {'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'okhttp/3.11.0'}
    if args.standard_ua:
        headers['standardUA'] = _read_argument(args.standard_ua).strip()
    for header in args.header:
        name, separator, value = header.partition(':')
        if not separator or not name.strip():
            raise ValueError('--header 格式为 名称:值')
        headers[name.strip()] = value.strip()
    request = urllib.request.Request(url, data=body.encode('ascii'), headers=headers)
    raw = urllib.request.urlopen(request, timeout=args.timeout).read()
    try:
        print(decode_response(raw, key))
    except (ValueError, UnicodeError):
        print(raw.decode('utf-8', 'replace'))


def main(argv=None):
    parser = argparse.ArgumentParser(description='到梦空间 v5 CLI：已验证的 RSA/AES 请求封装和 signToken')
    sub = parser.add_subparsers(required=True)
    dec = sub.add_parser('dec', help='解密完整 d 请求或原始 AES 密文')
    dec.add_argument('key')
    dec.add_argument('value', help='密文、d=表单或 @文件')
    dec.add_argument('--response', action='store_true', help='使用响应 IV')
    dec.set_defaults(f=cmd_dec)
    enc = sub.add_parser('enc', help='构造带新时间戳、签名及 RSA 密钥封装的 d 请求')
    enc.add_argument('json', help='JSON 对象或 @文件')
    enc.add_argument('--key')
    enc.add_argument('--timestamp', type=int, help='固定毫秒时间戳，默认当前时间')
    enc.add_argument('--no-sign', action='store_true', help='加密现有参数，保留其时间戳和签名')
    enc.set_defaults(f=cmd_enc)
    sign = sub.add_parser('sign', help='计算给定参数的 signToken，不自动添加时间戳')
    sign.add_argument('json')
    sign.add_argument('--timestamp', type=int)
    sign.set_defaults(f=cmd_sign)
    call = sub.add_parser('call', help='签名、加密并发送请求')
    call.add_argument('endpoint')
    call.add_argument('json')
    call.add_argument('--key')
    call.add_argument('--timestamp', type=int)
    call.add_argument('--standard-ua', help='standardUA JSON 头或 @文件')
    call.add_argument('--header', action='append', default=[], help='附加 HTTP 头：名称:值')
    call.add_argument('--timeout', type=float, default=20)
    call.set_defaults(f=cmd_call)
    args = parser.parse_args(argv)
    try:
        args.f(args)
    except urllib.error.HTTPError as error:
        parser.exit(1, f'HTTP {error.code}: {error.read().decode("utf-8", "replace")}\n')
    except (ValueError, UnicodeError, OSError, urllib.error.URLError) as error:
        parser.exit(1, f'错误: {error}\n')


if __name__ == '__main__':
    main()
