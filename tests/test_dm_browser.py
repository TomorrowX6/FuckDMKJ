"""Exercise browser identity through actual page JS and local HTTP requests."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import dm_captcha
import pytest

EXPECTED_UA = ('Mozilla/5.0 (Linux; Android 16; PHK110 Build/BP2A.250605.015; wv) '
               'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 '
               'Chrome/153.0.8010.36 Mobile Safari/537.36')


@contextmanager
def identity_server():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            headers = {k.lower(): v for k, v in self.headers.items()}
            received.append({'path': self.path, 'headers': headers})
            if self.path.startswith('/headers'):
                body = json.dumps(headers).encode()
                content_type = 'application/json'
            else:
                body = b'''<meta name="viewport" content="width=device-width,initial-scale=1">
                <script>window.__firstIdentity={
                  userAgent:navigator.userAgent,platform:navigator.platform,
                  languages:Array.from(navigator.languages),maxTouchPoints:navigator.maxTouchPoints,
                  uaData:navigator.userAgentData ? navigator.userAgentData.toJSON() : null
                };</script><body>identity probe</body>'''
                content_type = 'text/html'
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Accept-CH', 'Sec-CH-UA-Full-Version-List, Sec-CH-UA-Platform-Version, '
                             'Sec-CH-UA-Arch, Sec-CH-UA-Bitness, Sec-CH-UA-Model')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def observe_identity(page, url):
    page.goto(url)
    return page.evaluate('''async () => ({
      firstScript:window.__firstIdentity,
      userAgent:navigator.userAgent, platform:navigator.platform,
      language:navigator.language, languages:Array.from(navigator.languages),
      uaData:navigator.userAgentData ? await navigator.userAgentData.getHighEntropyValues(
        ['architecture','bitness','model','platformVersion','fullVersionList','uaFullVersion','wow64']) : null,
      maxTouchPoints:navigator.maxTouchPoints, hardwareConcurrency:navigator.hardwareConcurrency,
      dpr:devicePixelRatio, screen:[screen.width,screen.height,screen.availWidth,screen.availHeight],
      viewport:[innerWidth,innerHeight], timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
      headers:await fetch('/headers').then(r=>r.json())
    })''')


def test_resident_browser_identity_agrees_in_js_headers_and_new_pages():
    with identity_server() as (url, received):
        with dm_captcha.CaptchaBrowser() as browser:
            identities = []
            for _ in range(2):
                page = browser._new_page()
                identities.append(observe_identity(page, url))
                page.close()
            image_headers = browser._context.request.get(url + '/headers?image-download').json()
    for identity in identities:
        first = identity['firstScript']
        assert first['platform'] == 'Linux aarch64'
        assert first['userAgent'] == EXPECTED_UA
        assert first['languages'] == ['zh-CN', 'en-US']
        assert first['maxTouchPoints'] == 5
        assert first['uaData']['brands'][0] == {'brand': 'Android WebView', 'version': '153'}
        assert identity['userAgent'] == EXPECTED_UA
        assert identity['platform'] == 'Linux aarch64'
        assert identity['language'] == 'zh-CN'
        assert identity['languages'] == ['zh-CN', 'en-US']
        assert identity['timezone'] == 'Asia/Shanghai'
        assert identity['maxTouchPoints'] == 5
        assert identity['dpr'] == 3.5
        assert identity['screen'] == [355, 793, 355, 793]
        assert identity['viewport'] == [355, 753]
        hints = identity['uaData']
        assert hints['platform'] == 'Android' and hints['mobile'] is True
        assert hints['platformVersion'] == '16.0.0' and hints['model'] == 'PHK110'
        assert hints['architecture'] == hints['bitness'] == ''
        assert hints['uaFullVersion'] == '153.0.8010.36'
        assert hints['brands'][0] == {'brand': 'Android WebView', 'version': '153'}
        assert hints['fullVersionList'][0] == {'brand': 'Android WebView', 'version': '153.0.8010.36'}
        headers = identity['headers']
        assert headers['user-agent'] == EXPECTED_UA
        assert headers['sec-ch-ua-platform'] == '"Android"'
        assert headers['sec-ch-ua-mobile'] == '?1'
        assert headers['sec-ch-ua-model'] == '"PHK110"'
        assert '"Android WebView";v="153"' in headers['sec-ch-ua']
        assert all(language in headers['accept-language'] for language in ['zh-CN', 'en-US'])
    assert image_headers['user-agent'] == EXPECTED_UA
    assert all(language in image_headers['accept-language'] for language in ['zh-CN', 'en-US'])
    assert all(r['headers']['user-agent'] == EXPECTED_UA for r in received if r['path'] == '/')
    initial_requests = [r['headers'] for r in received if r['path'] == '/']
    assert len(initial_requests) == 2
    assert all(r['sec-ch-ua-platform'] == '"Android"' and r['sec-ch-ua-mobile'] == '?1'
               and '"Android WebView";v="153"' in r['sec-ch-ua'] for r in initial_requests)


def test_invalid_page_identity_is_reported_and_page_is_closed():
    with dm_captcha.CaptchaBrowser() as browser:
        browser._browser_profile['maxTouchPoints'] = 0  # Real CDP validation failure.
        with pytest.raises(dm_captcha.CaptchaError, match='配置'):
            browser._new_page()
        assert browser._context.pages == []
        browser._browser_profile['maxTouchPoints'] = 5
        page = browser._new_page()
        assert page.evaluate('navigator.maxTouchPoints') == 5
        page.close()
