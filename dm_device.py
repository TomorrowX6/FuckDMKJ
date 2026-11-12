"""Persistent simulated Android installation based on the supported phone model."""
import secrets
import time

APP_VERSION = '5.0.0'


def simulated_ua(previous=None, *, now_ms=None):
    """Keep installation identity stable; do not fabricate a JPush registration."""
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if previous is None:
        return {
            'channelName': 'dmkj_Android', 'countryCode': 'CN',
            'createTime': now, 'device': 'OnePlus PHK110', 'hardware': 'qcom',
            'modifyTime': now, 'operator': '', 'screenResolution': '1240-2633',
            'startTime': now, 'sysVersion': 'Android 36 16', 'system': 'android',
            'uuid': secrets.token_hex(16), 'version': APP_VERSION,
        }
    ua = dict(previous)
    ua['startTime'] = max(now, ua['createTime'])
    if ua.get('version') != APP_VERSION:
        ua.update(version=APP_VERSION, modifyTime=ua['startTime'])
    return ua
