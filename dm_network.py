"""Measured network settings for the Android 16 / app 5.0.0 capture."""
import json
from pathlib import Path


def network_profile_options(name):
    if name == 'generic':
        return {}
    if name != 'app':
        raise ValueError(f'Unknown network profile: {name}')
    from curl_cffi import CurlHttpVersion, CurlOpt, CurlSslVersion

    profile = json.loads((Path(__file__).resolve().parent /
                          'network_profiles/dm_android16.json').read_text(encoding='utf-8'))
    return {
        'http_version': CurlHttpVersion.V1_1,
        'ja3': profile['ja3'],
        'extra_fp': {
            'tls_grease': False,
            'tls_permute_extensions': False,
            'tls_signature_algorithms': profile['signatureAlgorithms'],
            'header_order': ','.join(profile['httpHeaderOrder']),
        },
        'curl_options': {
            # libcurl encodes the maximum version in the high 16 bits.
            CurlOpt.SSLVERSION: int(CurlSslVersion.TLSv1_2) | (int(CurlSslVersion.TLSv1_2) << 16),
            CurlOpt.SSL_ENABLE_ALPN: 0,
            CurlOpt.SSL_ENABLE_NPN: 0,
            CurlOpt.SSL_ENABLE_ALPS: 0,
            CurlOpt.SSL_ENABLE_TICKET: 0,
            CurlOpt.TLS_SIGNED_CERT_TIMESTAMPS: 0,
            CurlOpt.TLS_STATUS_REQUEST: 1,
        },
    }
