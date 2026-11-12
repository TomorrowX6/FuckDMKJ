import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
import time
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


PATH = Path(__file__).resolve().parents[1] / 'dm_captcha.py'
if PATH.exists():
    spec = importlib.util.spec_from_file_location('dm_captcha', PATH)
    captcha = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(captcha)
else:
    captcha = SimpleNamespace()


def test_gap_detection_accounts_for_transparent_front_margin():
    rng = np.random.default_rng(42)
    bg = rng.integers(0, 256, (80, 240, 3), dtype=np.uint8)
    front = np.zeros((80, 60, 4), dtype=np.uint8)
    front[20:50, 10:40, :3] = bg[20:50, 130:160]
    front[20:50, 10:40, 3] = 255
    _, bg_bytes = cv2.imencode('.png', bg)
    _, front_bytes = cv2.imencode('.png', front)
    gap = captcha.locate_gap(bg_bytes.tobytes(), front_bytes.tobytes())
    assert abs(gap['distance'] - 120) <= 1
    assert gap['naturalWidth'] == 240


def test_invalid_images_fail_before_browser_movement():
    with pytest.raises(captcha.CaptchaError, match='图片'):
        captcha.locate_gap(b'not-image', b'not-image')


def test_uses_final_sdk_validate_without_replacing_it_with_raw_check_value():
    value = 'CN31_business-specific-SDK-value_v_i_1'
    assert captcha.parse_validate_callback(['true', value, '验证成功', 'false', '', '', 'jigsaw', '']) == value


@pytest.mark.parametrize('value', [['false', '', '验证失败'], ['true', '', ''], [], ['false', 'error-token', '失败']])
def test_rejected_or_empty_callbacks_are_not_success(value):
    with pytest.raises(captcha.CaptchaError):
        captcha.parse_validate_callback(value)


def test_drag_corrects_touch_dead_zone_using_observed_piece_position():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**({'executable_path': os.environ['DM_BROWSER_EXECUTABLE']} if os.environ.get('DM_BROWSER_EXECUTABLE') else {'channel': 'chrome'}), headless=True)
        context = browser.new_context(viewport={'width': 390, 'height': 844},
                                      is_mobile=True, has_touch=True)
        page = context.new_page()
        try:
            page.set_content('''<meta name="viewport" content="width=device-width,initial-scale=1">
                <style>body{touch-action:none}.yidun_slider{position:absolute;left:10px;top:140px;
                width:40px;height:40px;background:blue}.yidun_jigsaw{position:absolute;left:10px;
                top:20px;width:40px;height:40px;background:red}</style>
                <div class="yidun_slider"></div><div class="yidun_jigsaw"></div>
                <script>
                let origin; const slider=document.querySelector('.yidun_slider');
                const piece=document.querySelector('.yidun_jigsaw');
                slider.addEventListener('touchstart',e=>{origin=e.touches[0].clientX});
                window.addEventListener('touchmove',e=>{
                    if(origin===undefined)return;
                    piece.style.left=(10+Math.max(0,e.touches[0].clientX-origin-7.5))+'px';
                    e.preventDefault();
                },{passive:false});
                window.addEventListener('touchend',()=>{document.body.dataset.released='true'});
                </script>''')
            captcha.drag_slider(page, 146, timeout=8)
            actual = page.locator('.yidun_jigsaw').bounding_box()['x']
            assert abs(actual - 146) < 1
            assert page.locator('body').get_attribute('data-released') == 'true'
        finally:
            context.close()
            browser.close()


def test_drag_bounds_locator_waits_by_its_own_deadline():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**({'executable_path': os.environ['DM_BROWSER_EXECUTABLE']} if os.environ.get('DM_BROWSER_EXECUTABLE') else {'channel': 'chrome'}), headless=True)
        page = browser.new_page()
        page.set_default_timeout(1000)
        try:
            page.set_content('<p>no slider</p>')
            started = time.monotonic()
            with pytest.raises(captcha.CaptchaError, match='超时'):
                captcha.drag_slider(page, 100, timeout=0.02)
            assert time.monotonic() - started < 0.6
        finally:
            browser.close()


@pytest.fixture
def sdk_page(tmp_path, monkeypatch):
    rng = np.random.default_rng(42)
    bg = rng.integers(0, 256, (80, 240, 3), dtype=np.uint8)
    front = np.zeros((80, 60, 4), dtype=np.uint8)
    front[20:50, 10:40, :3] = bg[20:50, 130:160]
    front[20:50, 10:40, 3] = 255
    cv2.imwrite(str(tmp_path / 'bg.png'), bg)
    cv2.imwrite(str(tmp_path / 'front.png'), front)
    html = tmp_path / 'sdk.html'
    html.write_text('''<meta name="viewport" content="width=device-width,initial-scale=1">
        <style>body{touch-action:none}img.yidun_bg-img{position:absolute;left:10px;top:20px;
        width:240px;height:80px}.yidun_slider{position:absolute;left:10px;top:140px;
        width:40px;height:40px;background:blue}img.yidun_jigsaw{position:absolute;left:10px;
        top:20px;width:60px;height:80px}</style><body>
        <script>
        window.initNECaptcha = function(){}; window.NECaptcha = function(){};
        const preload=new URLSearchParams(location.search).get('preload')==='true';
        if(!preload){
        document.body.insertAdjacentHTML('beforeend', '<img class="yidun_bg-img" src="bg.png">'+
          '<img class="yidun_jigsaw" src="front.png"><div class="yidun_slider"></div>'+
          '<button class="yidun_refresh">refresh</button>');
        let origin; const piece=document.querySelector('.yidun_jigsaw');
        document.querySelector('.yidun_slider').addEventListener('touchstart',e=>{
            origin=e.touches[0].clientX;
        });
        window.addEventListener('touchmove',e=>{
            if(origin===undefined)return;
            piece.style.left=(10+Math.max(0,e.touches[0].clientX-origin-7.5))+'px';
            e.preventDefault();
        },{passive:false});
        window.addEventListener('touchend',()=>{
            window.JSInterface.onValidate('true','SDK-final-test-value','ok','false','','','jigsaw','');
        });
        document.querySelector('.yidun_refresh').onclick=()=>{
            document.querySelector('.yidun_bg-img').src='bg.png?retry='+Date.now();
        };
        }
        </script>''', encoding='utf-8')
    monkeypatch.setattr(captcha, 'SDK_HTML', html)
    return html


def test_solver_recovers_from_one_image_timeout_within_same_sdk_session(sdk_page, monkeypatch):
    from playwright.sync_api import APIRequestContext, TimeoutError as BrowserTimeout, Response, Error
    original = APIRequestContext.get
    requests = []

    def get(self, url, **kwargs):
        requests.append(url)
        if len(requests) == 1:
            raise BrowserTimeout('one simulated image download timeout')
        return original(self, url, **kwargs)

    monkeypatch.setattr(APIRequestContext, 'get', get)
    # Exercise the fallback when Chromium cannot return a completed image body.
    def unavailable_body(self):
        raise Error('Response body is unavailable in this target')
    monkeypatch.setattr(Response, 'body', unavailable_body)
    result = captcha.solve_yidun('a' * 32, timeout=15, max_attempts=2)
    assert result['validate'] == 'SDK-final-test-value'
    assert len(requests) == 3
    # A failed image download does not trigger the SDK onValidate callback.
    assert result['useNumber'] == '1'
    assert result['useNumberDate'] == result['extraData']


def test_solver_reuses_browser_image_responses_without_duplicate_gets(sdk_page, monkeypatch):
    from playwright.sync_api import APIRequestContext
    seen = []
    original = captcha._Handler.do_GET

    def record(handler):
        seen.append(handler.path)
        return original(handler)

    def no_duplicate(*_args, **_kwargs):
        pytest.fail('Healthy image responses must not be fetched a second time')

    monkeypatch.setattr(captcha._Handler, 'do_GET', record)
    monkeypatch.setattr(APIRequestContext, 'get', no_duplicate)
    with captcha.CaptchaBrowser() as session:
        session.prepare('a' * 32)
        result = session.solve('a' * 32, timeout=15)
    assert result['validate'] == 'SDK-final-test-value'
    assert seen.count('/bg.png') == seen.count('/front.png') == 1
    assert result['timings']['cachedImageReads'] == 2
    assert result['timings']['fallbackImageReads'] == 0
    assert all(result['timings'][key] >= 0 for key in
               ['pageSeconds', 'imagesSeconds', 'matchSeconds', 'dragSeconds', 'verifySeconds'])
    assert result['timings']['totalSeconds'] >= sum(result['timings'][key] for key in
               ['pageSeconds', 'imagesSeconds', 'matchSeconds', 'dragSeconds', 'verifySeconds'])


def test_resident_browser_reuses_chrome_but_not_old_verification_state(sdk_page, monkeypatch):
    from playwright.sync_api import BrowserType
    launched = []
    original = BrowserType.launch

    def launch(self, *args, **kwargs):
        browser = original(self, *args, **kwargs)
        launched.append(browser)
        return browser

    monkeypatch.setattr(BrowserType, 'launch', launch)
    with captcha.CaptchaBrowser() as session:
        session.prepare('a' * 32)
        first = session.solve('a' * 32, timeout=15)
        second = session.solve('a' * 32, timeout=15)
        assert first['validate'] == second['validate'] == 'SDK-final-test-value'
        assert first['extraData'] != second['extraData']
        assert first['useNumber'] == second['useNumber'] == '1'
        assert first['useNumberDate'] == first['extraData']
        assert second['useNumberDate'] == second['extraData']
        assert len(launched) == 1
        assert launched[0].is_connected()
    assert not launched[0].is_connected()


@pytest.fixture
def image_server():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20" fill="red"/></svg>'
    release = threading.Event()
    release.set()
    state = {'body': svg, 'status': 200, 'requests': 0, 'release': release}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/redirect.svg':
                self.send_response(302)
                self.send_header('Location', '/image.svg')
                self.end_headers()
                return
            if self.path == '/image.svg':
                state['requests'] += 1
                body, status = state['body'], state['status']
                content_type = 'image/svg+xml'
            else:
                src = '/redirect.svg' if self.path == '/redirect' else '/image.svg'
                body = ('<meta name="viewport" content="width=device-width,initial-scale=1">'
                        f'<img id="piece" src="{src}" width="20" height="20">').encode()
                status, content_type = 200, 'text/html'
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            if self.path == '/image.svg':
                release.wait(5)
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_image_response_reuse_follows_redirects(image_server):
    url, state = image_server
    with captcha.CaptchaBrowser() as session:
        page = session._new_page()
        responses = captcha._ImageResponses(page)
        try:
            page.goto(url + '/redirect')
            assert responses.read(url + '/redirect.svg', timeout=1000) == state['body']
            assert state['requests'] == 1
        finally:
            responses.close()
            page.close()


def test_image_response_reuse_reads_latest_bytes_for_same_url(image_server):
    url, state = image_server
    with captcha.CaptchaBrowser() as session:
        page = session._new_page()
        responses = captcha._ImageResponses(page)
        try:
            page.goto(url)
            assert b'red' in responses.read(url + '/image.svg', timeout=1000)
            state['body'] = state['body'].replace(b'red', b'blue')
            # Fragment-only src changes do not refetch images; a real reload does.
            with page.expect_response(lambda response: response.url == url + '/image.svg', timeout=3000):
                page.reload()
            assert b'blue' in responses.read(url + '/image.svg#second', timeout=1000)
            assert state['requests'] == 2
        finally:
            responses.close()
            page.close()


def test_image_response_read_waits_for_completed_body_with_bounded_deadline(image_server, monkeypatch):
    from playwright.sync_api import Response, TimeoutError as BrowserTimeout
    url, state = image_server
    state['release'].clear()

    def incomplete_body(_self):
        pytest.fail('Body retrieval before requestfinished can wait without a timeout')

    monkeypatch.setattr(Response, 'body', incomplete_body)
    with captcha.CaptchaBrowser() as session:
        page = session._new_page()
        responses = captcha._ImageResponses(page)
        try:
            with page.expect_response(lambda response: response.url == url + '/image.svg'):
                page.goto(url, wait_until='domcontentloaded')
            started = time.monotonic()
            with pytest.raises(BrowserTimeout):
                responses.read(url + '/image.svg', timeout=50)
            assert time.monotonic() - started < 0.8
        finally:
            state['release'].set()
            responses.close()
            page.close()


def test_http_error_image_is_not_treated_as_a_successful_cached_body(image_server):
    url, state = image_server
    state['status'] = 404
    with captcha.CaptchaBrowser() as session:
        page = session._new_page()
        responses = captcha._ImageResponses(page)
        try:
            page.goto(url)
            with pytest.raises(captcha.CaptchaError, match='404'):
                responses.read(url + '/image.svg', timeout=1000)
        finally:
            responses.close()
            page.close()
