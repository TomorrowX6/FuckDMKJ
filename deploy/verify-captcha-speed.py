"""Verify one fresh official captcha; do not print validate or submit a signup."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dm_captcha import CaptchaBrowser


before = time.monotonic()
with CaptchaBrowser(emit=lambda message: None) as browser:
    browser.prepare('8cbcfc2941f6455d9f499f01bcb7186c')
    prepared = time.monotonic()
    result = browser.solve('8cbcfc2941f6455d9f499f01bcb7186c', timeout=60)
    report = {'verifiedAt': datetime.now(timezone.utc).isoformat(), 'verified': bool(result['validate']),
              'validateLength': len(result['validate']), 'attempts': result['attempts'],
              'preparationSeconds': prepared-before, 'solveSeconds': time.monotonic()-prepared,
              'stages': result['timings'], 'actualEngine': browser._browser.version,
              'signupSubmitted': False}
assert report['verified']
assert report['stages']['cachedImageReads'] >= 2
assert report['stages']['fallbackImageReads'] == 0
print(json.dumps(report))
