"""Fetch only the public SDK HTML asset, with pinned archive and asset hashes."""
import hashlib
from io import BytesIO
from pathlib import Path
import urllib.request
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://repo.maven.apache.org/maven2/io/github/yidun/captcha/3.6.5/captcha-3.6.5.aar'
ARCHIVE_SHA256 = '028370743fd1304e98fb3f092d6554387531d93afe2c3d34cec6572d07304ade'
ASSET_SHA256 = '1f078f3fe077361d866b5700f4840c11ecf47704fd3a63187af3fdf80e78aaaa'
ASSET = 'assets/mobile.v2.29.2.html'


def main():
    target = ROOT / 'captcha_sdk/mobile.v2.29.2.html'
    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == ASSET_SHA256:
        print('SDK asset already verified')
        return
    with urllib.request.urlopen(URL, timeout=30) as response:
        archive = response.read(16 * 1024 * 1024 + 1)
    if hashlib.sha256(archive).hexdigest() != ARCHIVE_SHA256:
        raise RuntimeError('SDK archive checksum mismatch')
    with ZipFile(BytesIO(archive)) as package:
        if package.getinfo(ASSET).file_size > 2 * 1024 * 1024:
            raise RuntimeError('SDK asset is unexpectedly large')
        html = package.read(ASSET)
    if hashlib.sha256(html).hexdigest() != ASSET_SHA256:
        raise RuntimeError('SDK HTML checksum mismatch')
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.html.tmp')
    temporary.write_bytes(html)
    temporary.chmod(0o644)
    temporary.replace(target)
    print('SDK asset downloaded and verified')


if __name__ == '__main__':
    main()
