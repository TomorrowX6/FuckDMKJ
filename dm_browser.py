"""Apply the measured Android WebView identity through browser emulation APIs."""
import json
from pathlib import Path


PROFILE_PATH = Path(__file__).resolve().parent / 'network_profiles/android_webview.json'


def load_browser_profile():
    return json.loads(PROFILE_PATH.read_text(encoding='utf-8'))


def _configure_page(context, page, profile):
    session = context.new_cdp_session(page)

    def detach():
        try:
            session.detach()
        except Exception:
            pass  # Closing a target can detach its sessions first.

    try:
        session.send('Emulation.setUserAgentOverride', {
            'userAgent': profile['userAgent'],
            'platform': profile['platform'],
            'acceptLanguage': ','.join(profile['languages']),
            'userAgentMetadata': profile['userAgentMetadata'],
        })
        session.send('Emulation.setTouchEmulationEnabled', {
            'enabled': True, 'maxTouchPoints': profile['maxTouchPoints'],
        })
    except BaseException:
        detach()
        raise
    page.once('close', detach)


def create_android_context(browser, profile):
    language_header = ','.join(language + (f';q={max(0.1, 1 - index / 10):.1f}' if index else '')
                               for index, language in enumerate(profile['languages']))
    context = browser.new_context(
        user_agent=profile['userAgent'], viewport=profile['viewport'], screen=profile['screen'],
        device_scale_factor=profile['deviceScaleFactor'], is_mobile=True, has_touch=True,
        locale=profile['locale'], timezone_id=profile['timezoneId'],
        extra_http_headers={'Accept-Language': language_header},
    )
    return context


def new_android_page(context, profile):
    page = context.new_page()
    try:
        _configure_page(context, page, profile)
    except BaseException:
        try:
            page.close()
        except Exception:
            pass
        raise
    return page
