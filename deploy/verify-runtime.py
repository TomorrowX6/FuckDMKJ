"""Local-only Chromium/protocol check on the deployed host; never signs up."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dm_browser import load_browser_profile
from dm_captcha import CaptchaBrowser
from dm_grab import load_protocol
from dm_bot_store import Store


with TemporaryDirectory(prefix='dm-runtime-') as temporary:
    store = Store(Path(temporary) / 'test.sqlite3')
    store.put_account('42', {'uid': '42', 'token': 'local-only', 'version': '5.0.0'}, '{}')
    assert store.account('42')[0]['token'] == 'local-only'
    protocol = load_protocol(ROOT / 'dm_cli.py')
    body = protocol.make_d(protocol.prepare_payload({'uid': '42', 'token': 'local-only'}), protocol.rand_key())
    assert body.startswith('d=')
    with CaptchaBrowser() as browser:
        page = browser._new_page()
        page.goto(f'http://127.0.0.1:{browser._server.server_port}/__runtime_probe')
        identity = page.evaluate('({ua:navigator.userAgent,platform:navigator.platform,mobile:navigator.userAgentData.mobile,touch:navigator.maxTouchPoints})')
        assert identity['ua'] == load_browser_profile()['userAgent']
        assert identity['platform'] == 'Linux aarch64' and identity['mobile'] and identity['touch'] == 5
        engine = browser._browser.version
    print(json.dumps({'sqlite': True, 'crypto': True, 'chromium': True, 'actualEngine': engine,
                      'identity': identity, 'signupSubmitted': False}))
