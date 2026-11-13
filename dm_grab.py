#!/usr/bin/env python3
"""到梦空间活动报名 CLI：输入活动 ID，等待开放并提交报名。"""
import argparse
from contextlib import ExitStack
import copy
from datetime import datetime, timedelta, timezone
import gzip
import http.client
import importlib.util
import json
import math
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib


ROOT = Path(__file__).resolve().parent
PROTOCOL_PATH = ROOT / 'dm_cli.py'
DETAIL = '/v2/activity/detail'
INFO = '/v2/signup/info'
SUBMIT = '/v2/signup/submit'
RESULT = '/v2/signup/result'
READ_ONLY_ENDPOINTS = frozenset({DETAIL, INFO, RESULT, '/v3/user/unread', '/v3/user/mine_homepage'})
CHINA = timezone(timedelta(hours=8))
CAPTCHA_HINT = '8cbcfc2941f6455d9f499f01bcb7186c'


class EnrollmentError(Exception):
    pass


class EnrollmentUncertain(EnrollmentError):
    pass


class CaptchaSessionChanged(EnrollmentError):
    """Raised only before a request is sent or its durable submit gate is entered."""


class BusinessError(EnrollmentError):
    def __init__(self, code, message):
        self.code = str(code)
        super().__init__(message or f'接口返回业务码 {code}')


class NetworkError(EnrollmentError):
    pass


def load_protocol(path):
    path = Path(path)
    if not path.is_file():
        raise EnrollmentError(f'未找到协议脚本：{path}')
    spec = importlib.util.spec_from_file_location('dm_protocol_for_grab', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def truth(value):
    return value is True or value == 1 or str(value).lower() in ('true', '1')


def identifier(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return False
    if isinstance(value, int):
        return value > 0
    return value.strip().lower() not in ('', '0', '-1', 'null', 'none', 'true', 'false')


def number(value, default=0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def activity_id(value):
    value = str(value).strip()
    if not re.fullmatch(r'[1-9][0-9]*', value):
        raise EnrollmentError('活动 ID 必须是正整数')
    return value


def assess_detail(detail, now_ms):
    if identifier(detail.get('signUpId')):
        return 'registered'
    if truth(detail.get('isCancel')) or truth(detail.get('cancel')):
        raise EnrollmentError('活动已取消')
    if (truth(detail.get('isEnd')) or truth(detail.get('end')) or
            str(detail.get('status')) in ('6', '8') or
            str(detail.get('buttonType')) in ('6', '13')):
        raise EnrollmentError('活动已结束')
    end = number(detail.get('joinenddate'))
    if end and now_ms >= end:
        raise EnrollmentError('报名时间已结束')
    maximum = number(detail.get('joinmaxnum'))
    if maximum > 0 and number(detail.get('joinNum')) >= maximum:
        raise EnrollmentError('报名人数已满')
    reason = str(detail.get('unableJoinReason') or '').strip()
    ready = (truth(detail.get('ableJoinFlag')) or truth(detail.get('canJoin')) or
             str(detail.get('buttonType')) == '3')
    future = number(detail.get('joinstartdate')) > now_ms
    if not ready and reason and not any(word in reason for word in
            ('未开始', '未开放', '未到', '尚未', '等待报名')):
        raise EnrollmentError(reason)
    if future:
        return 'waiting'
    if not ready:
        raise EnrollmentError(reason or str(detail.get('countdownText') or '当前活动不可报名'))
    return 'ready'


def prepare_fields(form, answers, prompt=None):
    fields = []
    allowed = {'conent', 'content', 'fullid', 'key', 'notList', 'notNull', 'system', 'title'}
    for original in form.get('info') or []:
        field = {key: copy.deepcopy(value) for key, value in original.items()
                 if key in allowed and value is not None}
        title = str(field.get('title') or field.get('fullid') or '报名信息')
        required = truth(field.get('notNull'))
        value = answers.get(str(field.get('fullid')), answers.get(title, field.get('conent', ''))) if required else ''
        value = '' if value is None else str(value)
        if required and not value.strip():
            if prompt is not None:
                value = prompt(f'请输入{title}：').strip()
            if not value:
                raise EnrollmentError(f'缺少必填报名信息：{title}；可用 --answers 指定 JSON 文件')
        field.update(conent=value)
        for key, default in [('key', 0), ('system', 0), ('notList', False), ('notNull', False)]:
            field.setdefault(key, default)
        fields.append(field)
    return fields


class DmClient:
    def __init__(self, protocol, profile, standard_ua, *, base_url=None, timeout=10,
                 transport='urllib', verify=True, network_profile='generic', require_auth=True):
        self.protocol = protocol
        self.profile = {key: value for key, value in profile.items()
                        if key not in ('timestamp', 'signToken', 'activityId')}
        if require_auth and (not self.profile.get('token') or not self.profile.get('uid')):
            raise EnrollmentError('账号文件缺少 token 或 uid')
        json.loads(standard_ua)
        self.standard_ua = standard_ua
        self.base_url = (base_url or protocol.BASE).rstrip('/')
        self.timeout = timeout
        self.transport = transport
        if network_profile not in ('app', 'generic'):
            raise EnrollmentError(f'未知网络配置：{network_profile}')
        self.network_profile = network_profile if transport == 'curl' else 'generic'
        self.last_transport_info = None
        self._session = None
        self._write_session = None
        self._curl_share = None
        self._closed = False
        if transport == 'curl':
            try:
                from curl_cffi import CurlInfo, CurlHttpVersion, CurlOpt, CurlShare
                from curl_cffi.requests import Session
            except ImportError as error:
                raise EnrollmentError('请先安装 TLS 依赖：python -m pip install -r requirements-grab.txt') from error
            self._curl_info = CurlInfo
            self._curl_opt = CurlOpt
            self._session_type = Session
            self._http_versions = {int(CurlHttpVersion.V1_0): 'HTTP/1.0',
                                   int(CurlHttpVersion.V1_1): 'HTTP/1.1',
                                   int(CurlHttpVersion.V2_0): 'HTTP/2',
                                   int(CurlHttpVersion.V3): 'HTTP/3'}
            # Share DNS/TLS session data, but never share live connection pools.
            self._curl_share = CurlShare(connect=False)
            self._session_options = dict(http_version=CurlHttpVersion.V2TLS, verify=verify,
                                        default_headers=False, allow_redirects=False, retry=0,
                                        discard_cookies=True, curl_share=self._curl_share,
                                        curl_infos=[CurlInfo.NUM_CONNECTS, CurlInfo.CONNECT_TIME,
                                                    CurlInfo.APPCONNECT_TIME, CurlInfo.TOTAL_TIME])
            from dm_network import network_profile_options
            self._session_options.update(network_profile_options(self.network_profile))
            self._session = Session(**self._session_options)
        elif transport != 'urllib':
            raise EnrollmentError(f'未知网络传输：{transport}')

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        if self._closed:
            return
        failure = None
        for resource in (self._session, self._write_session, self._curl_share):
            if resource is not None:
                try:
                    resource.close()
                except Exception as error:
                    failure = failure or error
        self._closed = True
        if failure is not None:
            raise EnrollmentError(f'关闭网络客户端失败：{failure}') from failure

    def request_session(self, endpoint):
        if endpoint in READ_ONLY_ENDPOINTS:
            return self._session
        if self._write_session is None:
            # libcurl can replay POST on a dead reused connection even with retry=0.
            options = dict(self._session_options)
            options['curl_options'] = dict(options.get('curl_options', {}))
            options['curl_options'].update({self._curl_opt.FRESH_CONNECT: 1, self._curl_opt.FORBID_REUSE: 1})
            self._write_session = self._session_type(**options)
        return self._write_session

    def build(self, endpoint, fields):
        signed = self.protocol.prepare_payload(dict(self.profile, **fields))
        key = self.protocol.rand_key()
        body = self.protocol.make_d(signed, key)
        headers = {'standardUA': self.standard_ua,
                   'Content-Type': 'application/x-www-form-urlencoded',
                   'User-Agent': 'okhttp/3.11.0', 'Accept-Encoding': 'gzip'}
        if self.network_profile == 'app':
            headers = {'standardUA': self.standard_ua,
                       'Content-Type': 'application/x-www-form-urlencoded',
                       'Content-Length': str(len(body)),
                       'Host': urllib.parse.urlsplit(self.base_url).netloc,
                       'Connection': 'Keep-Alive', 'Accept-Encoding': 'gzip',
                       'User-Agent': 'okhttp/3.11.0'}
        return {'url': self.base_url + endpoint, 'headers': headers,
                'body': body, 'key': key.decode('ascii'),
                'iv': self.protocol.REQUEST_IV.decode('ascii'), 'signedPayload': signed}

    def call(self, endpoint, fields):
        if self._closed:
            raise EnrollmentError('网络客户端已经关闭')
        self.last_transport_info = None
        built = self.build(endpoint, fields)
        try:
            if self._session is not None:
                response = self.request_session(endpoint).post(built['url'], data=built['body'].encode('ascii'),
                                              headers=built['headers'], timeout=self.timeout,
                                              accept_encoding='gzip')
                info = response.infos
                ci = self._curl_info
                connect = info.get(ci.CONNECT_TIME)
                handshake = info.get(ci.APPCONNECT_TIME)
                self.last_transport_info = {
                    'transport': 'curl_cffi',
                    'networkProfile': self.network_profile,
                    'connectionPolicy': 'pooled_read' if endpoint in READ_ONLY_ENDPOINTS else 'fresh_write',
                    'httpVersion': self._http_versions.get(int(response.http_version), str(response.http_version)),
                    'newConnections': info.get(ci.NUM_CONNECTS),
                    'tcpConnectSeconds': connect,
                    'tlsHandshakeSeconds': max(0, handshake - connect) if handshake is not None and connect is not None else None,
                    'totalSeconds': info.get(ci.TOTAL_TIME),
                }
                if response.status_code == 429:
                    raise BusinessError('429', '请求过于频繁，请稍后重试')
                if not 200 <= response.status_code < 300:
                    raise NetworkError(f'HTTP {response.status_code}，接口未返回可确认的报名结果')
                # libcurl has already decoded the advertised gzip content.
                raw = response.content
            else:
                request = urllib.request.Request(built['url'], data=built['body'].encode('ascii'),
                                                 headers=built['headers'], method='POST')
                started = time.perf_counter()
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                    if response.headers.get('Content-Encoding', '').lower() == 'gzip':
                        raw = gzip.decompress(raw)
                    self.last_transport_info = {'transport': 'urllib',
                        'httpVersion': 'HTTP/1.1' if response.version == 11 else 'HTTP/1.0',
                        'newConnections': None, 'totalSeconds': time.perf_counter() - started}
        except urllib.error.HTTPError as error:
            if error.code == 429:
                raise BusinessError('429', '请求过于频繁，请稍后重试') from error
            raise NetworkError(f'HTTP {error.code}，接口未返回可确认的报名结果') from error
        except (OSError, urllib.error.URLError, http.client.HTTPException, EOFError, zlib.error) as error:
            raise NetworkError(f'网络请求失败：{error}') from error
        try:
            result = json.loads(self.protocol.decode_response(raw, built['key'].encode('ascii')))
        except (ValueError, UnicodeError) as error:
            raise NetworkError('接口响应无法解析') from error
        if not isinstance(result, dict):
            raise NetworkError('接口响应格式异常')
        if str(result.get('code')) != '100':
            message = result.get('msg') or result.get('message')
            if not message and isinstance(result.get('data'), dict):
                message = result['data'].get('reason')
            raise BusinessError(result.get('code'), message)
        if not isinstance(result.get('data'), dict):
            raise NetworkError('接口未返回有效的数据对象')
        return result['data']


class EnrollmentRunner:
    def __init__(self, client, *, clock=time.time, sleep=time.sleep, emit=None,
                 max_attempts=10, retry_interval=1, result_timeout=60, captcha_solver=None,
                 captcha_hint=CAPTCHA_HINT, captcha_session=None):
        self.client = client
        self.clock = clock
        self.sleep = sleep
        self.emit = emit or (lambda *_args, **_kwargs: None)
        self.max_attempts = max_attempts
        self.retry_interval = retry_interval
        self.result_timeout = result_timeout
        self.captcha_solver = captcha_solver
        self.captcha_hint = captcha_hint
        self.captcha_session = captcha_session or (lambda: (
            getattr(self.client, 'profile', {}), getattr(self.client, 'standard_ua', '')))

    def prepare_captcha(self, captcha_id=None):
        prepare = getattr(self.captcha_solver, 'prepare', None)
        if callable(prepare):
            try:
                prepare(captcha_id)
            except RuntimeError as error:
                self.emit('warmup_warning', message=f'预热未完成，到点重新加载：{error}')

    def wait_until(self, when, *, prepare=None, prefetch=None):
        prepared = False
        prefetched = False
        while when > self.clock():
            remaining = when - self.clock()
            if prepare is not None and not prepared and remaining <= 30:
                # SDK warmup has a 20-second budget; keep five seconds spare.
                if remaining >= 25:
                    prepare()
                prepared = True
                continue
            if prefetch is not None and not prefetched and remaining <= 12:
                prefetched = True
                if remaining >= 5:
                    prefetch(timeout=min(10, remaining - 2))
                continue
            self.emit('waiting', message=f'距离提交还有 {remaining:.1f} 秒')
            delay = min(30, remaining)
            if prepare is not None and not prepared:
                delay = min(delay, max(0.01, remaining - 30))
            if prefetch is not None and not prefetched:
                delay = min(delay, max(0.01, remaining - 12))
            self.sleep(delay)

    def success(self, signup_id, already=False):
        result = {'status': 'success', 'signUpId': str(signup_id),
                  'alreadyRegistered': already}
        self.emit('success', message=('已经报名' if already else '报名成功'),
                  signUpId=str(signup_id))
        return result

    def poll_result(self, signup_id):
        deadline = self.clock() + self.result_timeout
        while self.clock() < deadline:
            try:
                result = self.client.call(RESULT, {'signUpId': str(signup_id)})
            except NetworkError:
                self.sleep(min(1, max(0.01, deadline - self.clock())))
                continue
            if not truth(result.get('success')):
                raise EnrollmentError(result.get('reason') or '报名结果：失败')
            waiting = number(result.get('waitingTime'), -100)
            if waiting == 0:
                if identifier(result.get('joinId')):
                    return self.success(result['joinId'])
                raise EnrollmentUncertain('报名结果缺少报名标识，尚未确认成功')
            delay = waiting / 1000 if 300 < waiting < 3000 else 0.6
            self.emit('pending', message='报名正在处理中，查询结果', signUpId=str(signup_id))
            self.sleep(min(delay, max(0.01, deadline - self.clock())))
        raise EnrollmentUncertain(f'查询报名结果超时；报名标识 {signup_id}，请用 --inspect 核对')

    def reconcile(self, target):
        deadline = self.clock() + self.result_timeout
        self.emit('uncertain', message='提交响应未确认，正在查询报名状态')
        while self.clock() < deadline:
            try:
                detail = self.client.call(DETAIL, {'activityId': target})
                if identifier(detail.get('signUpId')):
                    return self.success(detail['signUpId'])
            except NetworkError:
                pass
            self.sleep(min(1, max(0.01, deadline - self.clock())))
        raise EnrollmentUncertain('提交结果不确定，未重复提交；请使用 --inspect 核对报名状态')

    def run(self, target, *, answers=None, remark='', validate='', captcha_value='',
            prompt=None, at=None, dry_run=False, preview_path=None):
        target = activity_id(target)
        detail = self.client.call(DETAIL, {'activityId': target})
        state = assess_detail(detail, self.clock() * 1000)
        self.emit('detail', message=str(detail.get('activityName') or target),
                  activityId=target, joinstartdate=detail.get('joinstartdate'))
        if state == 'registered':
            return self.success(detail['signUpId'], already=True)
        when = at if at is not None else number(detail.get('joinstartdate')) / 1000
        can_prepare = (not dry_run and not validate and
                       callable(getattr(self.captcha_solver, 'prepare', None)))
        can_prefetch = (not dry_run and not validate and not captcha_value and
                        callable(getattr(self.captcha_solver, 'solve', None)))
        # Per-run only: never carry a validate into another activity or retry.
        prefetched = None
        prefetch_attempted = False

        def valid_solution(value):
            return (isinstance(value, dict) and isinstance(value.get('validate'), str)
                    and bool(value['validate'].strip()))

        def prefetch(captcha_id, *, timeout):
            nonlocal prefetched, prefetch_attempted
            if not can_prefetch or not captcha_id or prefetch_attempted:
                return
            prefetch_attempted = True
            session = copy.deepcopy(self.captcha_session())
            began = self.clock()
            self.emit('captcha_prefetch_started', message='提前完成验证码验证', timeout=timeout)
            try:
                solution = self.captcha_solver.solve(str(captcha_id), timeout=timeout)
            except RuntimeError as error:
                self.emit('captcha_prefetch_failed', message='提前验证未完成，到点重新验证',
                          errorType=type(error).__name__, elapsedSeconds=self.clock() - began)
                return
            if not valid_solution(solution):
                self.emit('captcha_prefetch_failed', message='提前验证结果无效，到点重新验证')
            elif session != self.captcha_session():
                self.emit('captcha_prefetch_discarded', message='会话已更新，到点重新验证', reason='session_changed')
            else:
                prefetched = (str(captcha_id), solution, self.clock(), session)
                self.emit('captcha_prefetch_ready', message='提前验证完成，等待开放',
                          elapsedSeconds=self.clock() - began)

        for attempt in range(1, self.max_attempts + 1):
            try:
                form = self.client.call(INFO, {'activityId': target})
                break
            except BusinessError as error:
                if dry_run or not any(word in str(error) for word in
                                      ('未开始', '未开放', '系统繁忙', '稍后重试')):
                    raise
                if attempt >= self.max_attempts:
                    raise EnrollmentError(f'读取报名表达到重试次数上限：{error}') from error
                if when > self.clock():
                    self.emit('form_wait', message='报名表尚未开放，到报名时间重新读取')
                    self.wait_until(when, prepare=self.prepare_captcha if can_prepare else None,
                                    prefetch=(lambda **kw: prefetch(self.captcha_hint, **kw)) if can_prefetch else None)
                else:
                    self.sleep(self.retry_interval)
        fields = prepare_fields(form, answers or {}, prompt)
        params = {'activityId': target, 'data': json.dumps(fields, ensure_ascii=False,
                  separators=(',', ':')), 'remark': '',
                  'useNumber': '0'}

        def solve_captcha():
            try:
                return self.captcha_solver(str(form['captchaId']))
            except RuntimeError as error:
                raise EnrollmentError(f'自动验证码失败：{error}') from error

        def use_solution(solution):
            if not valid_solution(solution):
                raise EnrollmentError('自动验证码模块未返回有效的验证结果')
            params.pop('useNumberDate', None)
            if solution.get('useNumberDate'):
                params['useNumberDate'] = str(solution['useNumberDate'])
            params['useNumber'] = str(solution.get('useNumber') or '1')
            params['validate'] = solution['validate']
            return solution['validate']

        if dry_run:
            if validate:
                params.update(validate=validate, useNumber='1')
            if captcha_value:
                params['captchaValue'] = captcha_value
            built = self.client.build(SUBMIT, params)
            preview = Path(preview_path or ROOT / 'requests' / f'activity-{target}-preview.json')
            preview.parent.mkdir(parents=True, exist_ok=True)
            preview.write_text(json.dumps(built, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            self.emit('dry_run', message=f'已生成预览：{preview}')
            return {'status': 'dry_run', 'path': str(preview)}
        warmup = None
        if can_prepare and truth(form.get('openCaptcha')) and form.get('captchaId'):
            warmup = lambda: self.prepare_captcha(str(form['captchaId']))
        early = None
        if can_prefetch and truth(form.get('openCaptcha')) and form.get('captchaId'):
            early = lambda **kw: prefetch(str(form['captchaId']), **kw)
        self.wait_until(when, prepare=warmup, prefetch=early)
        if number(detail.get('joinenddate')) and self.clock() * 1000 >= number(detail['joinenddate']):
            raise EnrollmentError('报名时间已结束')
        automatic_validate = (not validate and self.captcha_solver is not None and
                              truth(form.get('openCaptcha')) and bool(form.get('captchaId')))
        if truth(form.get('openCaptcha')) and form.get('captchaId'):
            captcha_solution = None
            if prefetched is not None:
                cid, solution, completed, session = prefetched
                prefetched = None
                age = self.clock() - completed
                reason = ('captcha_id_changed' if cid != str(form['captchaId']) else
                          'stale' if not 0 <= age <= 15 else
                          'session_changed' if session != self.captcha_session() else None)
                if reason:
                    self.emit('captcha_prefetch_discarded', message='提前验证结果不适用，重新验证',
                              reason=reason, ageSeconds=age)
                else:
                    captcha_solution = solution
                    self.emit('captcha_prefetch_reused', message='使用刚完成的验证码结果', ageSeconds=age)
            if not validate and self.captcha_solver is not None:
                if captcha_solution is None:
                    captcha_solution = solve_captcha()
                validate = use_solution(captcha_solution)
            if not validate and prompt is not None:
                validate = prompt('需要易盾验证；完成验证后输入 validate（空白停止）：').strip()
            if not validate:
                raise EnrollmentError('此活动需要易盾验证码；完成验证后通过 --validate 提供验证结果')
        elif truth(form.get('needCaptcha')):
            if not captcha_value and prompt is not None:
                captcha_value = prompt('请输入 App 图形验证码计算结果：').strip()
            if not captcha_value:
                raise EnrollmentError('此活动需要图形验证码，请通过 --captcha-value 提供结果')
        if validate:
            params['validate'] = validate
            if params['useNumber'] == '0':
                params['useNumber'] = '1'
        if captcha_value:
            params['captchaValue'] = captcha_value
        for attempt in range(1, self.max_attempts + 1):
            if number(detail.get('joinenddate')) and self.clock() * 1000 >= number(detail['joinenddate']):
                raise EnrollmentError('报名时间已结束')
            self.emit('submit', message=f'提交报名，第 {attempt} 次', activityId=target, attempt=attempt)
            try:
                try:
                    result = self.client.call(SUBMIT, params)
                except CaptchaSessionChanged:
                    if not automatic_validate:
                        raise
                    self.emit('captcha_session_refresh', message='提交前会话更新，重新验证；尚未发出报名请求')
                    validate = use_solution(solve_captcha())
                    if number(detail.get('joinenddate')) and self.clock() * 1000 >= number(detail['joinenddate']):
                        raise EnrollmentError('报名时间已结束')
                    # Only one local refresh. A second change stops before any write.
                    result = self.client.call(SUBMIT, params)
            except NetworkError:
                return self.reconcile(target)
            except BusinessError as error:
                retryable = any(word in str(error) for word in
                                ('未开始', '未开放', '未到报名', '系统繁忙', '稍后重试'))
                if not retryable or validate or captcha_value:
                    raise
                if attempt < self.max_attempts:
                    self.emit('retry', message=str(error), attempt=attempt)
                    self.sleep(self.retry_interval)
                    continue
                raise EnrollmentError(f'达到重试次数上限：{error}') from error
            signup_id = result.get('signUpId')
            if truth(result.get('getResult')):
                if not identifier(signup_id):
                    raise EnrollmentUncertain('排队响应缺少报名标识，无法确认结果')
                return self.poll_result(signup_id)
            if result.get('success') is False:
                raise EnrollmentError(result.get('reason') or '报名失败，服务端未提供原因')
            if identifier(signup_id):
                return self.success(signup_id)
            if truth(result.get('success')) and identifier(result.get('joinId')):
                return self.success(result['joinId'])
            raise EnrollmentUncertain('提交响应缺少有效报名标识，无法确认报名结果')
        raise EnrollmentError('达到重试次数上限')


def positive(value):
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError('必须是正数')
    return parsed


def parse_at(value):
    try:
        if value.isdigit():
            numeric = int(value)
            return numeric / 1000 if numeric > 100000000000 else numeric
        date = datetime.fromisoformat(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=CHINA)
        return date.timestamp()
    except ValueError as error:
        raise argparse.ArgumentTypeError('时间格式：2026-11-12 13:10:00，默认北京时间') from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('activity_id', nargs='?', help='活动 ID；不填则启动后输入')
    parser.add_argument('--inspect', action='store_true', help='只查询活动与报名状态')
    parser.add_argument('--dry-run', action='store_true', help='生成加密请求预览，不提交报名')
    parser.add_argument('--at', type=parse_at, help='指定提交时间；默认活动报名开始时间')
    parser.add_argument('--answers', type=Path, help='报名信息 JSON：字段 fullid 或标题对应填写值')
    parser.add_argument('--remark', default='', help='兼容旧参数；非必填备注统一留空')
    parser.add_argument('--validate', default='', help='已完成易盾验证后取得的 validate')
    parser.add_argument('--captcha-value', default='', help='图形验证码计算结果')
    parser.add_argument('--manual-captcha', action='store_true', help='关闭自动易盾滑块，手动提供验证结果')
    parser.add_argument('--show-captcha', action='store_true', help='显示自动滑块验证窗口')
    parser.add_argument('--profile', type=Path, default=ROOT / 'requests/mine_homepage/payload.json')
    parser.add_argument('--standard-ua', type=Path, default=ROOT / 'requests/mine_homepage/standardua.json')
    parser.add_argument('--cli', type=Path, default=PROTOCOL_PATH)
    parser.add_argument('--interval', type=positive, default=1, help='可重试失败的间隔秒数，默认 1')
    parser.add_argument('--max-attempts', type=int, default=10, help='最多提交次数，默认 10')
    parser.add_argument('--result-timeout', type=positive, default=60, help='排队结果等待秒数，默认 60')
    parser.add_argument('--transport', choices=['curl', 'urllib'], default='curl',
                        help='网络传输，默认 curl；urllib 为兼容模式')
    parser.add_argument('--network-profile', choices=['app', 'generic'], default='app',
                        help='curl 网络配置：app 使用实测 TLS1.2/HTTP1.1；generic 优先 HTTP2')
    args = parser.parse_args(argv)
    if args.max_attempts < 1:
        parser.error('--max-attempts 必须大于 0')
    log = ROOT / 'logs' / f'dm-grab-{datetime.now().strftime("%Y%m%d-%H%M%S")}.jsonl'

    def emit(kind, **fields):
        record = dict(kind=kind, at=datetime.now(CHINA).isoformat(), **fields)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        print(f'[{datetime.now(CHINA).strftime("%H:%M:%S")}] {fields.get("message", kind)}', flush=True)

    try:
        profile = json.loads(args.profile.read_text(encoding='utf-8-sig'))
        standard_ua = args.standard_ua.read_text(encoding='utf-8-sig').strip()
        protocol = load_protocol(args.cli)
        answers = json.loads(args.answers.read_text(encoding='utf-8-sig')) if args.answers and not args.inspect else {}
        if not isinstance(answers, dict):
            raise EnrollmentError('--answers 文件必须是 JSON 对象')
        interactive = args.activity_id is None and sys.stdin.isatty()
        with ExitStack() as resources:
            client = resources.enter_context(DmClient(protocol, profile, standard_ua, transport=args.transport,
                                                     network_profile=args.network_profile))
            captcha_solver = None
            if not (args.manual_captcha or args.inspect or args.dry_run or args.validate or args.captcha_value):
                from dm_captcha import CaptchaBrowser
                captcha_solver = resources.enter_context(CaptchaBrowser(headless=not args.show_captcha,
                                        emit=lambda message: emit('captcha', message=message)))
                if interactive:
                    try:
                        captcha_solver.prepare()
                    except RuntimeError as error:
                        emit('warmup_warning', message=f'预热未完成，需要验证时重新加载：{error}')
            worker = EnrollmentRunner(client, emit=emit, max_attempts=args.max_attempts,
                                      retry_interval=args.interval, result_timeout=args.result_timeout,
                                      captcha_solver=captcha_solver)
            while True:
                try:
                    raw_id = args.activity_id if args.activity_id is not None else input('请输入活动 ID（回车退出）：')
                except EOFError:
                    return 0
                if args.activity_id is None and not raw_id.strip():
                    return 0
                try:
                    target = activity_id(raw_id)
                    if args.inspect:
                        detail = client.call(DETAIL, {'activityId': target})
                        selected = {key: detail.get(key) for key in ('activityId', 'activityName', 'statusText',
                                    'joindate', 'joinNum', 'joinmaxnum', 'signUpId', 'unableJoinReason',
                                    'joinstartdate', 'joinenddate', 'buttonType', 'ableJoinFlag')}
                        print(json.dumps(selected, ensure_ascii=False, indent=2))
                    else:
                        worker.run(target, answers=answers, remark=args.remark, validate=args.validate,
                                   captcha_value=args.captcha_value, prompt=input if sys.stdin.isatty() else None,
                                   at=args.at, dry_run=args.dry_run)
                except (EnrollmentError, OSError, ValueError, RuntimeError) as error:
                    uncertain = isinstance(error, EnrollmentUncertain)
                    emit('uncertain' if uncertain else 'error', message=str(error))
                    if not interactive:
                        return 2 if uncertain else 1
                if not interactive:
                    return 0
    except KeyboardInterrupt:
        print('\n已停止。若正在提交，请先用 --inspect 核对报名状态。')
        return 130
    except EnrollmentUncertain as error:
        emit('uncertain', message=str(error))
        return 2
    except (EnrollmentError, OSError, ValueError, EOFError, RuntimeError) as error:
        emit('error', message=str(error))
        return 1


if __name__ == '__main__':
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    sys.exit(main())
