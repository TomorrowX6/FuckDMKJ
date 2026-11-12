"""Persistent simulated Android installations from complete upstream phone profiles."""
from collections import Counter
from functools import lru_cache
import json
from pathlib import Path
import secrets
import time

APP_VERSION = '5.0.0'
PROFILE_PATH = Path(__file__).resolve().parent/'device_profiles/android.json'
ANDROID_RELEASES = {29:10, 30:11, 31:12, 32:12, 33:13, 34:14, 35:15, 36:16}


@lru_cache(maxsize=1)
def load_profiles():
    dataset = json.loads(PROFILE_PATH.read_text(encoding='utf-8'))
    profiles = dataset['profiles']
    if dataset.get('schemaVersion') != 1 or not profiles:
        raise ValueError('设备配置库格式无效')
    names = set()
    for profile in profiles:
        for key in ('id','manufacturer','model','hardware','androidRelease'):
            if not isinstance(profile.get(key),str) or not profile[key].strip():
                raise ValueError('设备配置库缺少必要字段')
        if (type(profile.get('sdk')) is not int
                or ANDROID_RELEASES.get(profile['sdk']) != int(profile['androidRelease'].split('.')[0])):
            raise ValueError('设备配置库中的 Android 版本与 API 等级不一致')
        if not all(type(profile.get(key)) is int and 240 <= profile[key] <= 8000 for key in ('width','height')):
            raise ValueError('设备配置库中的屏幕尺寸无效')
        name = device_name(profile)
        if name in names:
            raise ValueError('设备配置库包含重复机型')
        names.add(name)
    return tuple(profiles)


def device_name(profile):
    return profile['manufacturer']+' '+profile['model']


def simulated_ua(previous=None, *, now_ms=None, used_devices=()):
    """Keep installation identity stable; do not fabricate a JPush registration."""
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if previous is None:
        counts = Counter(used_devices)
        profiles = load_profiles()
        minimum = min(counts[device_name(profile)] for profile in profiles)
        profile = secrets.choice([profile for profile in profiles if counts[device_name(profile)] == minimum])
        return {
            'channelName': 'dmkj_Android', 'countryCode': 'CN',
            'createTime': now, 'device': device_name(profile), 'hardware': profile['hardware'],
            'modifyTime': now, 'operator': '', 'screenResolution': f"{profile['width']}-{profile['height']}",
            'startTime': now, 'sysVersion': f"Android {profile['sdk']} {profile['androidRelease']}", 'system': 'android',
            'uuid': secrets.token_hex(16), 'version': APP_VERSION,
        }
    ua = dict(previous)
    ua['startTime'] = max(now, ua['createTime'])
    if ua.get('version') != APP_VERSION:
        ua.update(version=APP_VERSION, modifyTime=ua['startTime'])
    return ua
