"""One isolated signup worker; no Telegram credentials or token arguments."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import threading
import time

from dm_bot_store import Store
from dm_captcha import CaptchaBrowser
from dm_grab import DmClient, EnrollmentRunner, EnrollmentError, EnrollmentUncertain, CaptchaSessionChanged, DETAIL, SUBMIT, CAPTCHA_HINT, load_protocol, number

ROOT = Path(__file__).resolve().parent


class JobCancelled(Exception):
    pass


class Worker:
    def __init__(self, store, job_id, logs):
        self.store, self.job_id, self.logs = store, job_id, Path(logs)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.interrupted = threading.Event()
        self.submitted = False
        self.secrets = set()
        self.captcha_sessions = {}
        self.open_at = store.job(job_id)['open_at']
        self.warmed = False

    def clean(self, value):
        text = str(value)
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, '[hidden]')
        return text

    def emit(self, kind, **fields):
        safe = {key: self.clean(value) if isinstance(value, str) else value for key, value in fields.items()}
        item = {'at': datetime.now(timezone.utc).isoformat(), 'kind': kind, 'job': self.job_id, **safe}
        with (self.logs / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + '\n')
        print(json.dumps(item, ensure_ascii=False), flush=True)

    def check(self, *, before_submit=False):
        # An in-flight submit must be reconciled rather than interrupted blindly.
        if before_submit or not self.submitted:
            if self.store.cancelled(self.job_id):
                raise JobCancelled('待抢任务已取消')
            if self.interrupted.is_set():
                raise InterruptedError('Worker is stopping before submission')

    def sleep(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check()
            if not self.warmed and self.open_at - 24 <= time.time() < self.open_at - 14:
                self.warmed = True
                previous = self.client.timeout
                try:
                    self.client.timeout = min(previous, max(0.1, self.open_at - time.time() - 14))
                    self.client.call(DETAIL, {'activityId': self.job['activity_id']})
                    self.emit('connection_warmed', message='只读连接已预热')
                except EnrollmentError as error:
                    self.emit('warmup_warning', message=str(error))
                finally:
                    self.client.timeout = previous
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))

    def run(self):
        self.job = self.store.job(self.job_id)
        if self.job and self.job['status'] == 'cancelling' and not self.job['submit_attempts']:
            self.store.finish(self.job_id, 'cancelled', {'message': '任务已在启动前取消'})
            return 0
        if not self.job or self.job['status'] != 'running':
            return 1
        worker = self

        class Client(DmClient):
            def call(self, endpoint, fields):
                worker.check(before_submit=endpoint == SUBMIT)
                profile, ua = worker.store.account(worker.job['uid'])
                self.profile, self.standard_ua = profile, ua
                worker.secrets.add(profile['token'])
                if fields.get('validate'):
                    worker.secrets.add(fields['validate'])
                if endpoint == SUBMIT:
                    binding = worker.captcha_sessions.get(fields.get('validate'))
                    if binding is not None and binding != (profile, ua):
                        raise CaptchaSessionChanged('提交前会话已变化，验证码尚未使用')
                    if not worker.store.begin_submit(worker.job_id):
                        raise JobCancelled('待抢任务已取消，未再次提交')
                    worker.submitted = True
                began = time.perf_counter()
                try:
                    result = super().call(endpoint, fields)
                except Exception as error:
                    worker.emit('api_error', endpoint=endpoint, elapsed=time.perf_counter() - began,
                                errorType=type(error).__name__, code=getattr(error, 'code', None),
                                message=worker.clean(error))
                    raise
                worker.emit('api_success', endpoint=endpoint, elapsed=time.perf_counter() - began,
                            httpVersion=(self.last_transport_info or {}).get('httpVersion'),
                            newConnections=(self.last_transport_info or {}).get('newConnections'))
                return result

        class Solver:
            def __init__(self, browser):
                self.browser = browser

            def prepare(self, captcha_id=None):
                worker.check()
                self.browser.prepare(captcha_id or CAPTCHA_HINT)

            def __call__(self, captcha_id):
                return self.solve(captcha_id)

            def solve(self, captcha_id, *, timeout=60):
                worker.check()
                session = worker.store.account(worker.job['uid'])
                worker.store.phase(worker.job_id, 'captcha')
                try:
                    value = self.browser.solve(captcha_id, timeout=timeout)
                    if isinstance(value, dict) and isinstance(value.get('validate'), str):
                        worker.secrets.add(value['validate'])
                        worker.captcha_sessions[value['validate']] = session
                finally:
                    if self.browser.last_timings:
                        worker.emit('captcha_timing', **self.browser.last_timings)
                worker.check()
                return value

        status, result = 'failed', {'message': '工作进程未正常完成'}
        try:
            self.check()
            profile, ua = self.store.account(self.job['uid'])
            self.secrets.add(profile['token'])
            with ExitStack() as resources:
                self.client = resources.enter_context(Client(load_protocol(ROOT / 'dm_cli.py'), profile, ua,
                                                              transport='curl', network_profile='app'))
                browser = resources.enter_context(CaptchaBrowser(emit=lambda message: self.emit('captcha', message=message)))
                # Playwright traces retain evaluate() results, including validate.
                # Keep structured timing/error events instead of recording secrets.
                try:
                    browser.prepare(CAPTCHA_HINT)
                except RuntimeError as error:
                    self.emit('warmup_warning', message=self.clean(error))
                self.check()
                self.store.phase(self.job_id, 'waiting')
                self.emit('armed', activityId=self.job['activity_id'], openAt=self.open_at)
                result = EnrollmentRunner(self.client, sleep=self.sleep, emit=self.emit,
                                          retry_interval=0.25, captcha_solver=Solver(browser),
                                          captcha_session=lambda: self.store.account(self.job['uid'])).run(self.job['activity_id'])
                status = 'success'
        except JobCancelled as error:
            status, result = 'cancelled', {'message': str(error)}
        except EnrollmentUncertain as error:
            status, result = 'uncertain', {'message': self.clean(error)}
        except InterruptedError:
            self.emit('interrupted', message='服务停止；由持久状态决定恢复或人工核对')
            return 0  # Leave durable running/submitting state for safe recovery.
        except Exception as error:
            status = 'uncertain' if self.submitted and not isinstance(error, EnrollmentError) else 'failed'
            result = {'message': self.clean(error), 'errorType': type(error).__name__}
            if getattr(error, 'code', None) == '10007':
                result['message'] = '会话已失效，请重新导入手机当前 token'
        self.store.finish(self.job_id, status, result)
        self.emit('finished', status=status, message=result.get('message', ''), signUpId=result.get('signUpId'))
        (self.logs / 'result.json').write_text(json.dumps({'status': status, **result}, ensure_ascii=False, indent=2), encoding='utf-8')
        return 0 if status in ('success', 'cancelled') else 2 if status == 'uncertain' else 1


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--job', required=True)
    parser.add_argument('--logs', required=True, type=Path)
    args = parser.parse_args()
    worker = Worker(Store(args.db), args.job, args.logs)
    signal.signal(signal.SIGTERM, lambda *_: worker.interrupted.set())
    signal.signal(signal.SIGINT, lambda *_: worker.interrupted.set())
    return worker.run()


if __name__ == '__main__':
    raise SystemExit(main())
