#!/usr/bin/env python3
"""Private Telegram account/session and activity-task controller."""
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time

import requests

from dm_bot_store import Store, TERMINAL
from dm_grab import DmClient, EnrollmentError, activity_id, assess_detail, load_protocol
from dm_login import LoginClient, encrypt_password

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger('dm_bot')

UA_EXAMPLE = {'channelName': 'dmkj_Android', 'countryCode': 'CN', 'createTime': 1700000000000,
              'device': 'OnePlus PHK110', 'hardware': 'qcom', 'jPushId': 'YOUR_JPUSH_ID',
              'modifyTime': 1700001000000, 'operator': '%E4%B8%AD%E5%9B%BD%E7%A7%BB%E5%8A%A8',
              'screenResolution': '1240-2633', 'startTime': 1700001000000,
              'sysVersion': 'Android 36 16', 'system': 'android',
              'uuid': '00000000000000000000000000000000', 'version': '5.0.0'}
HELP = '''命令（私聊使用）：
/login 账号 密码
  主人或获准登录的用户使用账号密码登录，自动保存模拟设备配置和会话；密码不保存。
/login UID TOKEN standardUA_JSON
  主人或获准登录的用户导入现有会话，验证后保存。
/permissions UID TGID
  主人为 TGID 授予该 UID 的控制权限。
/permissions 1 TGID
  主人为 TGID 开通两种 /login；成功后自动获得对应 UID 的控制权。
/add UID 活动ID
  添加待抢任务，自动等待开放、验证和报名。
/cancel UID 活动ID
  取消待抢任务；不撤销已成功的报名。
/me UID
  查询个人信息。
/list UID
  查询该 UID 的待抢任务列表。
/help

standardUA 直接填写 JSON 对象，不要加外层单引号，内部有空格也支持。
jPushId 和版本号直接读取 JSON 中的 jPushId、version；未注册的 jPushId 可省略。
账号密码登录无需填写 UA；设备 UUID 按账号复用，未注册的 jPushId 不伪造。
密码含空格时用 JSON 双引号包裹，例如 /login ACCOUNT "Pass word!"。
示例（替换为真实手机数据和会话）：
/login 123456 YOUR_TOKEN ''' + json.dumps(UA_EXAMPLE, ensure_ascii=False, separators=(',', ':'))


def uid_value(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,32}', value):
        raise ValueError('UID 必须为数字')
    return value


def parse_login(arguments):
    fields = arguments.strip().split(None, 2)
    if len(fields) != 3:
        raise ValueError('格式：/login UID TOKEN standardUA_JSON')
    uid, token, tail = fields
    uid_value(uid)
    if len(token) > 2048 or not token.strip():
        raise ValueError('token 格式无效')
    try:
        ua, consumed = json.JSONDecoder().raw_decode(tail)
        if isinstance(ua, str):
            ua = json.loads(ua)
    except (ValueError, TypeError):
        raise ValueError('standardUA 必须是完整 JSON 对象；不要加外层单引号') from None
    if not isinstance(ua, dict):
        raise ValueError('standardUA 必须是 JSON 对象')
    if tail[consumed:].strip():
        raise ValueError('JSON 后不需要其他参数；jPushId 和 version 从 JSON 读取')
    push, version = ua.get('jPushId'), ua.get('version')
    if push not in (None, '') and (not isinstance(push, str) or len(push) > 256
            or any(character.isspace() for character in push)):
        raise ValueError('standardUA 中的 jPushId 必须是有效字符串或省略')
    if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+._A-Za-z0-9]*)?', version):
        raise ValueError('standardUA 中必须包含有效的 version 字符串')
    profile = {'uid': uid, 'token': token, 'version': version}
    if push:
        profile['jPushId'] = push
    else:
        ua.pop('jPushId', None)
    return profile, json.dumps(ua, ensure_ascii=False, separators=(',', ':'))


def parse_password_login(arguments):
    fields = arguments.strip().split(None, 1)
    if len(fields) != 2:
        raise ValueError('格式：/login 账号 密码，或 /login UID TOKEN standardUA_JSON')
    account, password = fields
    if password.startswith('"'):
        try:
            password = json.loads(password)
        except ValueError:
            raise ValueError('密码双引号格式无效，请使用 JSON 字符串格式') from None
    elif any(character.isspace() for character in password):
        raise ValueError('密码含空格时请用 JSON 双引号包裹')
    if len(account) > 128 or not account.isprintable():
        raise ValueError('账号格式无效')
    if not isinstance(password, str) or not password or len(password) > 1024:
        raise ValueError('密码格式无效')
    return account, password


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token, *, base_url='https://api.telegram.org', timeout=40):
        self.token, self.base_url, self.timeout = token, base_url.rstrip('/'), timeout
        self.local = threading.local()

    def call(self, method, **parameters):
        if not hasattr(self.local, 'session'):
            self.local.session = requests.Session()
        try:
            response = self.local.session.post(f'{self.base_url}/bot{self.token}/{method}',
                                                json=parameters, timeout=(10, self.timeout))
            value = response.json()
        except (requests.RequestException, ValueError):
            raise TelegramError('Telegram 网络请求失败') from None
        if not isinstance(value, dict) or not value.get('ok'):
            description = str(value.get('description', '响应无效')) if isinstance(value, dict) else '响应无效'
            raise TelegramError(description.replace(self.token, '[hidden]')[:300]) from None
        return value['result']

    def send(self, chat_id, text):
        return self.call('sendMessage', chat_id=chat_id, text=text[:4000], protect_content=True,
                         link_preview_options={'is_disabled': True})

    def delete(self, chat_id, message_id):
        return self.call('deleteMessage', chat_id=chat_id, message_id=message_id)


def client_factory(profile, ua):
    protocol = load_protocol(ROOT / 'dm_cli.py')
    return DmClient(protocol, profile, ua, transport='curl', network_profile='app')


def password_client_factory(ua):
    return LoginClient(load_protocol(ROOT / 'dm_cli.py'), ua)


def lock_instance(path):
    handle = Path(path).open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError('同一数据目录的机器人已经运行，未启动第二个实例') from None
    return handle


class Bot:
    def __init__(self, store, telegram, owner, *, clients=client_factory, password_clients=password_client_factory):
        self.store, self.telegram, self.owner, self.clients = store, telegram, int(owner), clients
        self.password_clients = password_clients

    def authorize(self, uid, sender):
        uid_value(uid)
        if not self.store.allowed(uid, sender, self.owner):
            raise ValueError('你没有此 UID 的控制权限')

    def handle(self, update):
        message = update.get('message')
        if not message or not isinstance(message.get('text'), str):
            return
        chat, sender = message.get('chat', {}), message.get('from', {})
        chat_id, tg_id = chat.get('id'), sender.get('id')
        if not isinstance(tg_id, int) or chat_id is None:
            return
        text = message['text'].strip()
        command, _, arguments = text.partition(' ')
        if '\n' in command:
            command, _, arguments = text.partition('\n')
        command = command.split('@', 1)[0].lower()
        if command == '/login':
            try:
                self.telegram.delete(chat_id, message['message_id'])
            except TelegramError:
                pass
        if chat.get('type') != 'private' or chat_id != tg_id:
            self.telegram.send(chat_id, '请在与机器人的私聊中使用命令')
            return
        profile = None
        login_secrets = []
        try:
            if command in ('/help', '/start'):
                self.telegram.send(chat_id, HELP)
                return
            if command == '/permissions' and tg_id != self.owner:
                raise ValueError('此命令仅主人可用')
            if command == '/login':
                if not self.store.can_login(tg_id, self.owner):
                    raise ValueError('仅主人或获准登录的用户可用，请联系主人使用 /permissions 1 TGID 授权')
                fields = arguments.split(None, 2)
                if len(fields) <= 2 or fields[1].startswith('"'):
                    account, password = parse_password_login(arguments)
                    login_secrets.extend((password, encrypt_password(password)))
                    ua = self.store.login_device(account)
                    with self.password_clients(ua) as client:
                        profile = client.login(account, password)
                    password = ''
                else:
                    profile, ua = parse_login(arguments)
                with self.clients(profile, ua) as client:
                    personal = client.call('/v3/user/mine_homepage', {})
                if str(personal.get('userId')) != profile['uid']:
                    raise ValueError('返回的用户标识与 UID 不一致，未导入')
                self.store.put_account(profile['uid'], profile, ua,
                                       controller=tg_id if tg_id != self.owner else None)
                self.telegram.send(chat_id, f"UID {profile['uid']} 会话验证成功，已保存。token 不会回显。")
                return
            args = arguments.split()
            if command == '/permissions':
                if len(args) != 2 or not args[1].isdigit() or not 0 < int(args[1]) < 2**63:
                    raise ValueError('格式：/permissions UID TGID')
                uid_value(args[0])
                if args[0] == '1':
                    self.store.grant_login(int(args[1]))
                    self.telegram.send(chat_id, f'已授权 TGID {args[1]} 使用两种 /login；验证成功后自动获得对应 UID 控制权')
                    return
                self.store.grant(args[0], int(args[1]))
                self.telegram.send(chat_id, f'已授权 TGID {args[1]} 控制 UID {args[0]}')
                return
            if command not in ('/add', '/cancel', '/me', '/list'):
                raise ValueError('未知命令，请使用 /help')
            if len(args) != (1 if command in ('/me', '/list') else 2):
                raise ValueError('参数数量不正确，请使用 /help')
            uid = args[0]
            self.authorize(uid, tg_id)
            profile, ua = self.store.account(uid)
            if command == '/cancel':
                target = activity_id(args[1])
                job = self.store.cancel(uid, target)
                if job is None:
                    answer = '没有可取消的待抢任务；已完成的报名未作任何变更'
                elif job['submit_attempts']:
                    answer = '已请求停止。此前可能已提交，正在核对结果；此命令不会撤销报名'
                else:
                    answer = '已取消待抢任务；若正在处理验证码，会在提交前停止'
                self.telegram.send(chat_id, answer)
                return
            if command == '/list':
                active = [j for j in self.store.jobs(('waiting', 'running', 'cancelling')) if j['uid'] == uid]
                tasks = '、'.join(j['activity_id'] for j in active) if active else '无'
                self.telegram.send(chat_id, f'UID：{uid}\n待抢任务：{tasks}')
                return
            with self.clients(profile, ua) as client:
                if command == '/me':
                    personal = client.call('/v3/user/mine_homepage', {})
                    lines = [f'UID：{uid}']
                    for key, label in [('name', '姓名'), ('nickname', '昵称'), ('cardName', '身份'), ('schoolName', '学校')]:
                        if personal.get(key):
                            lines.append(f'{label}：{str(personal[key])[:150]}')
                    self.telegram.send(chat_id, '\n'.join(lines))
                    return
                target = activity_id(args[1])
                detail = client.call('/v2/activity/detail', {'activityId': target})
            if assess_detail(detail, time.time() * 1000) == 'registered':
                self.telegram.send(chat_id, f'UID {uid} 已报名活动 {target}，未创建重复任务')
                return
            opened = float(detail.get('joinstartdate') or 0) / 1000
            job, created = self.store.add_job(uid, target, chat_id, tg_id,
                                             str(detail.get('activityName') or target), opened)
            stamp = datetime.fromtimestamp(opened).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z') if opened else '立即'
            self.telegram.send(chat_id, f"{'已添加' if created else '任务已存在'}：{job['title']}\nUID {uid}，活动 {target}\n开放：{stamp}\n任务：{job['id'][:12]}，结果会在私聊通知")
        except (ValueError, EnrollmentError) as error:
            explanation = str(error)
            if profile and profile.get('token'):
                explanation = explanation.replace(profile['token'], '[hidden]')
            for secret in login_secrets:
                explanation = explanation.replace(secret, '[hidden]')
            if command == '/login':
                pieces = arguments.split(None, 2)
                if len(pieces) >= 2:
                    explanation = explanation.replace(pieces[1], '[hidden]')
            if getattr(error, 'code', None) == '10007':
                explanation = '会话已失效，请联系主人重新 /login；机器人不会自动重试密码登录'
            self.telegram.send(chat_id, explanation[:1000])
        except Exception as error:
            if command != '/login' or isinstance(error, TelegramError):
                raise
            LOG.warning('login_request_error type=%s', type(error).__name__)
            self.telegram.send(chat_id, '登录请求未完成，结果未确认，请稍后手动重试')


class Scheduler:
    def __init__(self, store, telegram, owner, data_dir, *, max_active=4, lead_seconds=90):
        self.store, self.telegram, self.owner = store, telegram, owner
        self.data_dir, self.max_active, self.lead_seconds = Path(data_dir), max_active, lead_seconds
        self.processes = {}
        self.stop = threading.Event()
        self.thread = None
        self.notifier = None

    def start(self):
        self.store.recover()
        self.thread = threading.Thread(target=self.loop, name='job-scheduler', daemon=True)
        self.thread.start()
        self.notifier = threading.Thread(target=self.notify_loop, name='job-notifier', daemon=True)
        self.notifier.start()

    def tick(self):
        for job_id, process in list(self.processes.items()):
            if process.poll() is None:
                continue
            self.processes.pop(job_id)
            job = self.store.job(job_id)
            if job['status'] not in TERMINAL:
                status = 'uncertain' if job['submit_attempts'] else 'cancelled' if job['cancel_requested'] else 'failed'
                self.store.finish(job_id, status, {'message': '工作进程意外结束；提交过的任务未自动重试，请核对 App'})
        for job in self.store.jobs(('waiting',)):
            if len(self.processes) >= self.max_active:
                break
            if job['open_at'] > time.time() + self.lead_seconds or not self.store.claim(job['id']):
                continue
            folder = self.data_dir / 'jobs' / job['id']
            folder.mkdir(parents=True, exist_ok=True)
            try:
                with (folder / 'worker.log').open('ab', buffering=0) as stream:
                    process = subprocess.Popen([sys.executable, '-u', str(ROOT / 'dm_bot_worker.py'),
                                                '--db', str(self.store.path), '--job', job['id'],
                                                '--logs', str(folder)], stdin=subprocess.DEVNULL,
                                               stdout=stream, stderr=subprocess.STDOUT, cwd=str(ROOT))
                self.processes[job['id']] = process
                LOG.info('job_started id=%s uid=%s activity=%s', job['id'], job['uid'], job['activity_id'])
            except OSError:
                self.store.finish(job['id'], 'failed', {'message': '无法启动报名进程，请联系主人查看日志'})
    def notify(self):
        for job in self.store.unnotified():
            result = json.loads(job['result']) if job['result'] else {}
            labels = {'success': '报名成功', 'cancelled': '任务已取消', 'failed': '任务失败', 'uncertain': '结果待核对'}
            text = f"{labels[job['status']]}\nUID {job['uid']}，活动 {job['activity_id']}：{job['title']}\n任务 {job['id'][:12]}"
            if result.get('signUpId'):
                text += f"\n报名编号：{result['signUpId']}"
            if result.get('message'):
                text += '\n' + str(result['message'])[:800]
            try:
                self.telegram.send(job['chat_id'], text)
                if job['creator'] != self.owner:
                    try:
                        self.telegram.send(self.owner, text)
                    except TelegramError:
                        LOG.warning('owner_notification_unavailable job=%s', job['id'])
                self.store.notified(job['id'])
            except TelegramError:
                LOG.warning('job_notification_pending job=%s', job['id'])

    def loop(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as error:
                LOG.error('scheduler_error type=%s', type(error).__name__)
            self.stop.wait(1)

    def notify_loop(self):
        while not self.stop.is_set():
            try:
                self.notify()
            except Exception as error:
                LOG.error('notifier_error type=%s', type(error).__name__)
            self.stop.wait(5)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=45)
        if self.notifier:
            self.notifier.join(timeout=45)
        for process in self.processes.values():
            if process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + 72
        for process in self.processes.values():
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main():
    os.umask(0o077)
    data_dir = Path(os.environ.get('DM_BOT_DATA', ROOT / 'bot_data')).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    instance_lock = lock_instance(data_dir / 'bot.lock')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token, owner = os.environ.get('TG_BOT_TOKEN', ''), int(os.environ.get('TG_OWNER_ID', '0'))
    if not token or owner <= 0:
        raise SystemExit('TG_BOT_TOKEN and TG_OWNER_ID are required')
    api = Telegram(token)
    store = Store(data_dir / 'bot.sqlite3')
    bot = Bot(store, api, owner)
    scheduler = Scheduler(store, api, owner, data_dir, max_active=int(os.environ.get('DM_MAX_ACTIVE_JOBS', '4')))
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_args: stopping.set())
    signal.signal(signal.SIGINT, lambda *_args: stopping.set())
    identity = api.call('getMe')
    LOG.info('bot_started username=%s owner=%s', identity['username'], owner)
    webhook = api.call('getWebhookInfo')
    if webhook.get('url'):
        api.call('deleteWebhook', drop_pending_updates=False)
    if not store.offset_initialized():
        pending = api.call('getUpdates', offset=-1, timeout=0, allowed_updates=['message'])
        store.offset(pending[-1]['update_id'] + 1 if pending else 0)
        LOG.info('initial_update_offset_initialized')
    api.call('setMyCommands', commands=[{'command': name, 'description': description} for name, description in
             [('login', '密码登录或导入会话（需授权）'), ('permissions', '主人分配 UID 或登录权限'), ('add', '添加抢活动任务'),
              ('cancel', '取消待抢任务'), ('me', '个人信息'), ('list', '待抢任务列表'), ('help', '帮助及 UA 示例')]])
    scheduler.start()
    try:
        while not stopping.is_set():
            try:
                updates = api.call('getUpdates', offset=store.offset(), timeout=25, allowed_updates=['message'])
                for update in updates:
                    if stopping.is_set():
                        break
                    try:
                        bot.handle(update)
                    except TelegramError:
                        LOG.warning('telegram_reply_failed update=%s', update['update_id'])
                    except Exception as error:
                        LOG.error('command_error type=%s update=%s', type(error).__name__, update['update_id'])
                    store.offset(update['update_id'] + 1)
            except TelegramError as error:
                LOG.warning('telegram_poll_error message=%s', str(error))
                stopping.wait(5)
    finally:
        scheduler.close()
        instance_lock.close()
        LOG.info('bot_stopped')


if __name__ == '__main__':
    main()
