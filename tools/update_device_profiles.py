"""Rebuild the bundled phone data from pinned AuroraOSS properties (GPL-3.0+)."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REVISION = '2f03e8babee8adf791d4f3a3b17c268a704be76c'
BASE = f'https://gitlab.com/AuroraOSS/gplayapi/-/raw/{REVISION}/'
PREFIX = 'lib/src/main/resources/com/aurora/gplayapi/device/'
FILES = ('hw_mate20', 'mi_a1', 'nk_drx', 'nothing_p1', 'op_8_pro', 'oppo_r17',
         'poco_f1', 'px_9a', 'rm_5_pro', 'rm_5i', 'rm_7', 'rm_note_12_4g',
         'sm_a13_5g', 'sm_f34_5g', 'sm_j5_prime', 'sm_s20_plus', 'sm_s25u', 'xm_11a', 'xp_5_dual')


def fetch(path):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(BASE+path, timeout=30) as response:
                content = response.read(512*1024+1)
            if len(content) > 512*1024:
                raise ValueError('Source file is unexpectedly large')
            return content
        except OSError:
            if attempt == 2:
                raise
            time.sleep(attempt+1)


def phone(name):
    source = PREFIX+name+'.properties'
    raw = fetch(source)
    fields = {}
    for line in raw.decode('utf-8').splitlines():
        if line.lstrip().startswith(('#', '!')) or '=' not in line:
            continue
        key, value = line.split('=', 1)
        fields[key.strip()] = value.strip()
    sdk = int(fields['Build.VERSION.SDK_INT'])
    if sdk < 29 or 'arm64-v8a' not in fields.get('Platforms', '').split(','):
        return None
    profile = {'id':name, 'manufacturer':fields['Build.MANUFACTURER'],
               'model':fields['Build.MODEL'], 'hardware':fields['Build.HARDWARE'],
               'androidRelease':fields['Build.VERSION.RELEASE'], 'sdk':sdk,
               'width':int(fields['Screen.Width']), 'height':int(fields['Screen.Height']),
               'sourcePath':source, 'sourceSha256':hashlib.sha256(raw).hexdigest()}
    if not all(profile[key] for key in ('manufacturer','model','hardware','androidRelease')):
        raise ValueError('Incomplete device profile: '+name)
    if not all(240 <= profile[key] <= 8000 for key in ('width','height')):
        raise ValueError('Invalid display dimensions: '+name)
    return profile


def main():
    with ThreadPoolExecutor(max_workers=5) as pool:
        profiles = [item for item in pool.map(phone, FILES) if item is not None]
    if len(profiles) < 10:
        raise ValueError('Too few complete phone profiles')
    assert len({(p['manufacturer'],p['model']) for p in profiles}) == len(profiles)
    license_text = fetch('LICENSE')
    dataset = {'schemaVersion':1, 'sourceProject':'AuroraOSS/gplayapi',
               'sourceRevision':REVISION, 'license':'GPL-3.0-or-later', 'copyright':'2020 AuroraOSS',
               'modified':'2026-11-12: Extracted phone fields; filtered to ARM64 and Android SDK 29+.',
               'profiles':profiles}
    folder = ROOT/'device_profiles'
    folder.mkdir(exist_ok=True)
    (folder/'COPYING').write_bytes(license_text)
    temporary = folder/'android.json.tmp'
    temporary.write_text(json.dumps(dataset,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    temporary.replace(folder/'android.json')
    print(json.dumps({'phoneProfiles':len(profiles),'brands':sorted({p['manufacturer'] for p in profiles}),
                      'sourceRevision':REVISION}))


if __name__ == '__main__':
    main()
