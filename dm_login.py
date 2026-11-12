#!/usr/bin/env python3
"""到梦空间账号密码登录，保存报名 CLI 所需的 token、uid 等参数。"""
import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
import warnings

from Crypto.Cipher import DES
from Crypto.Util.Padding import pad

from dm_grab import DmClient, EnrollmentError, identifier, load_protocol


ROOT = Path(__file__).resolve().parent
PROTOCOL_PATH = ROOT / 'dm_cli.py'
LOGIN_ENDPOINT = '/v2/login/phone'
PASSWORD_KEY = b'514345744E41596C4E41496C'


def encrypt_password(password):
    if not isinstance(password, str) or not password:
        raise EnrollmentError('密码不可为空')
    # The app's legacy 3DES key has parity-equivalent K2/K3. DES3.new rejects
    # such keys, while Android/JCE accept them; explicit EDE preserves its wire format.
    block = DES.new(PASSWORD_KEY[:8], DES.MODE_ECB).encrypt(pad(password.encode('utf-8'), 8))
    block = DES.new(PASSWORD_KEY[8:16], DES.MODE_ECB).decrypt(block)
    return DES.new(PASSWORD_KEY[16:24], DES.MODE_ECB).encrypt(block).hex().upper()


def session_profile(user, common):
    token, uid = user.get('token'), user.get('uid')
    if not isinstance(token, str) or not token.strip() or not identifier(uid):
        raise EnrollmentError('登录响应缺少有效的 token 或 uid，未更新账号文件')
    profile = {'uid': str(uid), 'token': token, 'version': common['version']}
    if common.get('jPushId'):
        profile['jPushId'] = common['jPushId']
    return profile


class LoginClient(DmClient):
    def __init__(self, protocol, standard_ua, **options):
        device = json.loads(standard_ua)
        if not isinstance(device, dict):
            raise EnrollmentError('standardUA 必须是 JSON 对象')
        common = {'version': str(device.get('version') or '5.0.0')}
        if device.get('jPushId'):
            common['jPushId'] = str(device['jPushId'])
        options.setdefault('transport', 'curl')
        options.setdefault('network_profile', 'app')
        super().__init__(protocol, common, standard_ua, require_auth=False, **options)

    def login(self, account, password):
        if not isinstance(account, str) or not account.strip():
            raise EnrollmentError('账号不可为空')
        user = self.call(LOGIN_ENDPOINT, {'account': account.strip(), 'pwd': encrypt_password(password)})
        return session_profile(user, self.profile)


def save_profile(destination, profile):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix=destination.name + '.',
                                         suffix='.tmp', dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(profile, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if destination.exists():
            stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
            backup = destination.with_name(destination.name + '.' + stamp + '-' + secrets.token_hex(4) + '.bak')
            shutil.copy2(destination, backup)
        temporary.replace(destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def login_and_save(client, account, password, destination):
    profile = client.login(account, password)
    save_profile(destination, profile)
    return profile


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', help='账号；不填时交互输入')
    parser.add_argument('--output', type=Path, default=ROOT / 'requests/mine_homepage/payload.json',
                        help='保存账号参数的文件；默认供报名 CLI 直接使用')
    parser.add_argument('--standard-ua', type=Path, default=ROOT / 'requests/mine_homepage/standardua.json')
    parser.add_argument('--cli', type=Path, default=PROTOCOL_PATH)
    args = parser.parse_args(argv)
    password = ''
    try:
        standard_ua = args.standard_ua.read_text(encoding='utf-8-sig').strip()
        protocol = load_protocol(args.cli)
        account = args.account if args.account is not None else input('账号：')
        if not account.strip():
            raise EnrollmentError('账号不可为空')
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            password = getpass.getpass('密码（隐藏输入）：')
        with LoginClient(protocol, standard_ua) as client:
            login_and_save(client, account, password, args.output)
        print(f'登录成功，账号参数已保存至：{args.output.resolve()}')
        return 0
    except (EnrollmentError, OSError, ValueError) as error:
        message = str(error)
        if password:
            try:
                message = message.replace(encrypt_password(password), '[hidden]')
            except (EnrollmentError, ValueError):
                pass
            message = message.replace(password, '[hidden]')
        print(f'登录失败：{message}', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\n登录已取消，未更新账号文件', file=sys.stderr)
        return 130
    except getpass.GetPassWarning:
        print('当前终端无法隐藏密码，请在 PowerShell 窗口运行此命令', file=sys.stderr)
        return 1
    finally:
        password = ''


if __name__ == '__main__':
    sys.exit(main())
