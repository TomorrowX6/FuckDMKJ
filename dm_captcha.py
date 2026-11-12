"""Automatic YiDun jigsaw verification using the official Android SDK page."""
import argparse
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import random
import re
import threading
import time
import urllib.parse

from dm_browser import create_android_context, load_browser_profile, new_android_page


SDK_HTML = Path(__file__).resolve().parent / 'captcha_sdk/mobile.v2.29.2.html'
ANDROID_UA = load_browser_profile()['userAgent']
_IMAGE_ENGINE_READY = False


class CaptchaError(RuntimeError):
    pass


def warm_image_engine():
    """Pay imports and image-codec/kernel setup before the signup critical path."""
    global _IMAGE_ENGINE_READY
    if _IMAGE_ENGINE_READY:
        return 0.0
    started = time.monotonic()
    try:
        import cv2
        import numpy as np
    except ImportError as error:
        raise CaptchaError('请先安装滑块依赖：python -m pip install -r requirements-grab.txt') from error
    image = np.zeros((16, 16, 4), dtype=np.uint8)
    image[:, :, 3] = 255
    ok, encoded = cv2.imencode('.png', image)
    if not ok or cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED) is None:
        raise CaptchaError('图像库初始化失败')
    gray = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    cv2.Canny(gray, 100, 200)
    cv2.matchTemplate(gray, gray[:4, :4], cv2.TM_CCOEFF_NORMED)
    _IMAGE_ENGINE_READY = True
    return time.monotonic() - started


class _ImageResponses:
    """Read the newest completed browser image response, scoped to one page."""
    def __init__(self, page):
        self.page = page
        self.urls, self.responses = {}, {}
        self.finished, self.failed = set(), set()
        self.listeners = {'request': self._request, 'response': self._response,
                          'requestfinished': self._finished, 'requestfailed': self._failed}
        for event, callback in self.listeners.items():
            page.on(event, callback)

    @staticmethod
    def key(url):
        return urllib.parse.urldefrag(url)[0]

    def _request(self, request):
        if request.resource_type == 'image':
            self.urls[self.key(request.url)] = request

    def _response(self, response):
        if response.request.resource_type == 'image':
            self.responses[response.request] = response

    def _finished(self, request):
        if request.resource_type == 'image':
            self.finished.add(request)

    def _failed(self, request):
        if request.resource_type == 'image':
            self.failed.add(request)

    def target(self, url):
        request = self.urls.get(self.key(url))
        for _ in range(20):
            if request is None or request.redirected_to is None:
                return request
            request = request.redirected_to
        return None

    def read(self, url, *, timeout):
        from playwright.sync_api import Error
        request = self.target(url)
        if request is None or request in self.failed:
            return None
        if request not in self.finished:
            self.page.wait_for_event('requestfinished',
                predicate=lambda _: self.target(url) in self.finished, timeout=timeout)
            request = self.target(url)
        response = self.responses.get(request)
        if response is None or response.status in (301, 302, 303, 304, 307, 308):
            return None
        if not response.ok:
            raise CaptchaError(f'验证码图片下载失败：HTTP {response.status}')
        # The requestfinished event is the completion barrier for body retrieval.
        try:
            return response.body() or None
        except Error:
            return None

    def close(self):
        for event, callback in self.listeners.items():
            self.page.remove_listener(event, callback)
        self.urls.clear()
        self.responses.clear()
        self.finished.clear()
        self.failed.clear()


def locate_gap(background_bytes, front_bytes):
    try:
        import cv2
        import numpy as np
    except ImportError as error:
        raise CaptchaError('请先安装滑块依赖：python -m pip install -r requirements-grab.txt') from error
    bg = cv2.imdecode(np.frombuffer(background_bytes, np.uint8), cv2.IMREAD_COLOR)
    front = cv2.imdecode(np.frombuffer(front_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    if bg is None or front is None or len(front.shape) != 3 or front.shape[2] != 4:
        raise CaptchaError('验证码图片格式无效或拼图没有透明通道')
    x, y, width, height = cv2.boundingRect(front[:, :, 3])
    if not width or not height or width >= bg.shape[1] or height > bg.shape[0]:
        raise CaptchaError('验证码图片尺寸无效')
    piece = front[y:y + height, x:x + width, :3]
    bg_edge = cv2.Canny(cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY), 100, 200)
    piece_edge = cv2.Canny(cv2.cvtColor(piece, cv2.COLOR_BGR2GRAY), 100, 200)
    matched = cv2.matchTemplate(bg_edge, piece_edge, cv2.TM_CCOEFF_NORMED)
    _, score, _, location = cv2.minMaxLoc(matched)
    if not math.isfinite(score) or score < 0.15:
        raise CaptchaError('验证码图片缺口定位不可靠')
    return {'distance': location[0] - x, 'naturalWidth': bg.shape[1], 'score': score}


def parse_validate_callback(value):
    if (not isinstance(value, (list, tuple)) or len(value) < 2 or
            value[0] != 'true' or not isinstance(value[1], str) or not value[1].strip()):
        message = value[2] if isinstance(value, (list, tuple)) and len(value) > 2 else ''
        raise CaptchaError(str(message or '易盾未返回有效的验证结果'))
    return value[1]


def drag_slider(page, target_left, *, timeout=8):
    """Align the rendered piece, including touch slop, before releasing it."""
    from playwright.sync_api import TimeoutError as BrowserTimeout
    deadline = time.monotonic() + timeout

    def bounded_box(locator):
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise CaptchaError('读取滑块位置超时')
        try:
            value = locator.bounding_box(timeout=min(remaining, 2000))
        except BrowserTimeout as error:
            raise CaptchaError('读取滑块位置超时') from error
        if value is None:
            raise CaptchaError('滑块或拼图暂不可见')
        return value

    front = page.locator('.yidun_jigsaw').first
    slider = bounded_box(page.locator('.yidun_slider').first)
    initial = bounded_box(front)
    distance = target_left - initial['x']
    if not math.isfinite(distance) or distance <= 0:
        raise CaptchaError('滑块目标坐标无效')
    sx, sy = slider['x'] + slider['width'] / 2, slider['y'] + slider['height'] / 2
    cdp = page.context.new_cdp_session(page)

    def touch(kind, x=None, y=None):
        if kind != 'touchEnd' and time.monotonic() >= deadline:
            raise CaptchaError('滑块拖动超时')
        cdp.send('Input.dispatchTouchEvent', {'type': kind, 'touchPoints': [] if x is None else
                 [{'x': x, 'y': y, 'radiusX': 4, 'radiusY': 4, 'force': 1, 'id': 1}]})

    touch('touchStart', sx, sy)
    try:
        time.sleep(0.17)
        for index in range(1, 66):
            t = index / 65
            eased = t * t * (3 - 2 * t)
            touch('touchMove', sx + distance * eased,
                  sy + math.sin(math.pi * t) * random.uniform(-1.5, 1.5))
            time.sleep(random.uniform(0.012, 0.024))
        for _ in range(4):
            time.sleep(0.06)
            error = target_left - bounded_box(front)['x']
            if abs(error) < 0.8:
                break
            start = distance
            steps = max(3, min(12, int(abs(error) / 2)))
            for index in range(1, steps + 1):
                touch('touchMove', sx + start + error * index / steps, sy)
                time.sleep(0.018)
            distance += error
        actual = bounded_box(front)['x']
        if abs(target_left - actual) >= 1:
            raise CaptchaError('拼图位置未对齐')
        time.sleep(0.15)
        return {'targetLeft': target_left, 'actualLeft': actual}
    finally:
        touch('touchEnd')
        cdp.detach()


class _Handler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if urllib.parse.urlsplit(self.path).path == '/__dm_warmup':
            body = ('<!doctype html><meta charset="utf-8">'
                    '<script src="https://cstaticdun.126.net/load.min.js?v='
                    + str(int(time.time() // 60)) + '"></script>').encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


class CaptchaBrowser:
    """Own one browser and SDK resource cache for a CLI session."""
    def __init__(self, *, headless=True, emit=None):
        self.headless = headless
        self.emit = emit or (lambda _message: None)
        self._playwright = self._browser = self._context = None
        self._browser_profile = None
        self._server = self._thread = self._warm_page = None
        self._owner = None
        self._prepared_id = None
        self._prepared_at = 0
        self.last_timings = None
        self.image_engine_warm_seconds = 0.0

    def __enter__(self):
        return self.start()

    def __exit__(self, *_args):
        self.close()

    def _check_owner(self):
        if self._owner is not None and self._owner != threading.get_ident():
            raise CaptchaError('浏览器会话必须在创建它的线程中使用')

    def start(self, *, timeout=15):
        self._check_owner()
        if self._browser is not None and self._browser.is_connected():
            return self
        self.close()
        if not SDK_HTML.is_file():
            raise CaptchaError(f'缺少验证码 SDK 页面：{SDK_HTML}')
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise CaptchaError('请先安装滑块依赖：python -m pip install -r requirements-grab.txt') from error
        self._owner = threading.get_ident()
        try:
            self.image_engine_warm_seconds = warm_image_engine()
            self._server = ThreadingHTTPServer(('127.0.0.1', 0), partial(_Handler, directory=str(SDK_HTML.parent)))
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
            self._playwright = sync_playwright().start()
            executable = os.environ.get('DM_BROWSER_EXECUTABLE')
            launch_options = {'executable_path': executable} if executable else {'channel': 'chrome'}
            self._browser = self._playwright.chromium.launch(**launch_options, headless=self.headless,
                                                            timeout=max(1, int(timeout * 1000)))
            self._browser_profile = load_browser_profile()
            self._context = create_android_context(self._browser, self._browser_profile)
            return self
        except BaseException as error:
            self.close()
            if isinstance(error, Exception):
                raise CaptchaError(f'无法启动验证码浏览器：{error}') from error
            raise

    def _new_page(self):
        self._check_owner()
        try:
            return new_android_page(self._context, self._browser_profile)
        except Exception as error:
            raise CaptchaError(f'无法配置浏览器页面：{error}') from error

    def close(self):
        self._check_owner()
        for resource in (self._context, self._browser):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        if self._server is not None:
            if self._thread is not None and self._thread.is_alive():
                self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._playwright = self._browser = self._context = None
        self._browser_profile = None
        self._server = self._thread = self._warm_page = None
        self._owner = None
        self._prepared_id = None
        self._prepared_at = 0

    def _sdk_url(self, captcha_id, *, extra_data=None, preload=False):
        params = {'captchaId': captcha_id, 'os': 'android', 'osVer': '16', 'sdkVer': '3.6.5',
                  'popupStyles.width': '320', 'lang': 'zh-CN', 'defaultFallback': 'false',
                  'errorFallbackCount': '3', 'mobileTimeout': '20000', 'size': 'small',
                  'logable': 'false', 'disableReport': 'true'}
        if extra_data is not None:
            params['extraData'] = extra_data
        if preload:
            params['preload'] = 'true'
        return f'http://127.0.0.1:{self._server.server_port}/{SDK_HTML.name}?' + urllib.parse.urlencode(params)

    def prepare(self, captcha_id=None, *, timeout=20):
        if captcha_id is not None and not re.fullmatch(r'[0-9a-fA-F]{32}', captcha_id):
            raise CaptchaError('活动返回的 captchaId 格式无效')
        deadline = time.monotonic() + timeout
        self.start(timeout=min(15, timeout))
        if (self._warm_page is not None and not self._warm_page.is_closed() and
                self._prepared_id == captcha_id and time.monotonic() - self._prepared_at < 20):
            return
        if self._warm_page is None or self._warm_page.is_closed():
            self._warm_page = self._new_page()
        def remaining():
            budget = int((deadline - time.monotonic()) * 1000)
            if budget <= 0:
                raise CaptchaError('浏览器资源预热超时')
            return budget
        url = self._sdk_url(captcha_id, preload=True) if captcha_id else f'http://127.0.0.1:{self._server.server_port}/__dm_warmup'
        try:
            self._warm_page.goto(url, wait_until='domcontentloaded', timeout=remaining())
            condition = 'Boolean(window.initNECaptcha && window.NECaptcha)' if captcha_id else 'Boolean(window.initNECaptcha)'
            self._warm_page.wait_for_function(condition, timeout=remaining())
            self._prepared_id, self._prepared_at = captcha_id, time.monotonic()
            self.emit('验证码浏览器已预热，等待需要时获取新挑战')
        except Exception as error:
            raise CaptchaError(f'验证码资源预热未完成：{error}') from error

    def __call__(self, captcha_id):
        return self.solve(captcha_id)

    def solve(self, captcha_id, *, max_attempts=3, timeout=60):
        self.last_timings = None
        if not re.fullmatch(r'[0-9a-fA-F]{32}', captcha_id):
            raise CaptchaError('活动返回的 captchaId 格式无效')
        if max_attempts < 1 or not math.isfinite(timeout) or timeout <= 0:
            raise CaptchaError('验证码次数和超时必须为正数')
        started = time.monotonic()
        deadline = started + timeout
        extra_data = str(int(time.time() * 1000))
        emit = self.emit
        timings = {name: 0.0 for name in ['setupSeconds', 'pageSeconds', 'imagesSeconds',
                                        'matchSeconds', 'dragSeconds', 'verifySeconds']}
        timings.update(cachedImageReads=0, fallbackImageReads=0, attempts=0)

        @contextmanager
        def stage(name):
            before = time.monotonic()
            try:
                yield
            finally:
                timings[name] += time.monotonic() - before

        def milliseconds(maximum=30000):
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                raise CaptchaError('自动滑块验证超时')
            return min(remaining, maximum)
        page = None
        images = None
        try:
            with stage('setupSeconds'):
                self.start(timeout=milliseconds(15000) / 1000)
                from playwright.sync_api import TimeoutError as BrowserTimeout
                page = self._new_page()
                images = _ImageResponses(page)
                page.add_init_script('''
                window.__dmCaptchaResult = null;
                window.__dmCaptchaEvents = [];
                window.JSInterface = new Proxy({}, {get: (_, name) => (...args) => {
                    if (name === 'onValidate') {
                        window.__dmCaptchaResult = args;
                        if (args[3] !== 'true') window.__dmCaptchaEvents.push({args, at: Date.now()});
                    }
                }});
            ''')
            url = self._sdk_url(captcha_id, extra_data=extra_data)
            with stage('pageSeconds'):
                page.goto(url, wait_until='domcontentloaded', timeout=milliseconds())

            def image_bytes(url):
                cached = images.read(url, timeout=milliseconds(10000))
                if cached is not None:
                    timings['cachedImageReads'] += 1
                    return cached
                timings['fallbackImageReads'] += 1
                response = self._context.request.get(url, timeout=milliseconds(10000))
                if not response.ok:
                    raise CaptchaError('验证码图片下载失败')
                return response.body()

            last_error = '验证未通过'
            for attempt in range(1, max_attempts + 1):
                timings['attempts'] = attempt
                emit(f'自动处理易盾滑块，第 {attempt} 次')
                background = page.locator('img.yidun_bg-img').first
                front = page.locator('img.yidun_jigsaw').first
                old_src = None
                try:
                    with stage('imagesSeconds'):
                        background.wait_for(state='visible', timeout=milliseconds())
                        front.wait_for(state='visible', timeout=milliseconds(10000))
                        old_src = urllib.parse.urljoin(page.url, background.get_attribute('src', timeout=milliseconds(5000)))
                        front_src = urllib.parse.urljoin(page.url, front.get_attribute('src', timeout=milliseconds(5000)))
                        bg, fg = image_bytes(old_src), image_bytes(front_src)
                    with stage('matchSeconds'):
                        gap = locate_gap(bg, fg)
                    box = background.bounding_box(timeout=milliseconds(5000))
                    if box is None:
                        raise CaptchaError('验证码背景图片暂不可见')
                    target = box['x'] + gap['distance'] * box['width'] / gap['naturalWidth']
                    page.evaluate('window.__dmCaptchaResult = null')
                    with stage('dragSeconds'):
                        drag_slider(page, target, timeout=min(8, milliseconds() / 1000))
                    with stage('verifySeconds'):
                        page.wait_for_function('Boolean(window.__dmCaptchaResult)', timeout=milliseconds(15000))
                        validate = parse_validate_callback(page.evaluate('window.__dmCaptchaResult'))
                    events = page.evaluate('window.__dmCaptchaEvents')
                    dates = [extra_data] + [str(event['at']) for event in events
                            if event['args'][0] != 'true' or not event['args'][1]]
                    emit('易盾滑块验证通过')
                    timings['totalSeconds'] = time.monotonic() - started
                    return {'validate': validate, 'extraData': extra_data,
                            'useNumber': str(len(events)), 'useNumberDate': ','.join(dates),
                            'attempts': attempt, 'timings': dict(timings)}
                except (CaptchaError, BrowserTimeout) as error:
                    last_error = str(error)
                    if attempt >= max_attempts:
                        break
                    emit('滑块未通过，刷新后重试')
                    try:
                        page.locator('.yidun_refresh').first.click(timeout=milliseconds(5000))
                        if old_src:
                            page.wait_for_function("old => { const image = document.querySelector('img.yidun_bg-img'); "
                                                   "return Boolean(image && image.src && image.src !== old); }",
                                                   arg=old_src, timeout=milliseconds(10000))
                    except BrowserTimeout:
                        last_error = '刷新验证码超时'
            raise CaptchaError(f'自动滑块验证失败：{last_error}')
        except CaptchaError:
            raise
        except Exception as error:
            raise CaptchaError(f'自动验证码运行失败：{error}') from error
        finally:
            timings['totalSeconds'] = time.monotonic() - started
            self.last_timings = dict(timings)
            try:
                emit('验证码分段耗时：' + json.dumps({key: round(value, 6) if isinstance(value, float) else value
                                                  for key, value in timings.items()}, separators=(',', ':')))
            finally:
                try:
                    if images is not None:
                        images.close()
                finally:
                    if page is not None:
                        page.close()


def solve_yidun(captcha_id, *, max_attempts=3, timeout=60, headless=True, emit=None):
    browser = CaptchaBrowser(headless=headless, emit=emit)
    try:
        return browser.solve(captcha_id, max_attempts=max_attempts, timeout=timeout)
    finally:
        browser.close()


def main():
    parser = argparse.ArgumentParser(description='只验证易盾滑块，不提交活动报名')
    parser.add_argument('captcha_id')
    parser.add_argument('--show', action='store_true', help='显示验证窗口')
    parser.add_argument('--output', type=Path, help='将验证结果保存到本地 JSON 文件')
    args = parser.parse_args()
    try:
        solution = solve_yidun(args.captcha_id, headless=not args.show)
    except CaptchaError as error:
        parser.exit(1, str(error) + '\n')
    if args.output:
        args.output.write_text(json.dumps(solution, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'verified': True, 'validateLength': len(solution['validate']),
                      'attempts': solution.get('attempts', int(solution['useNumber']))}))


if __name__ == '__main__':
    main()
