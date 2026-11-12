from collections import Counter
import hashlib
import json
import re

import pytest

import dm_device
from dm_device import simulated_ua, load_profiles, device_name
from dm_bot_store import Store


@pytest.mark.parametrize('profile', load_profiles(), ids=lambda profile:profile['id'])
def test_simulated_device_has_coherent_template_without_claiming_push_registration(profile, monkeypatch):
    monkeypatch.setattr(dm_device,'load_profiles',lambda:(profile,))
    ua = simulated_ua(now_ms=1700000000000)
    assert ua['device'] == device_name(profile) and ua['hardware'] == profile['hardware']
    assert ua['system'] == 'android' and ua['sysVersion'] == f"Android {profile['sdk']} {profile['androidRelease']}"
    assert ua['screenResolution'] == f"{profile['width']}-{profile['height']}"
    assert ua['createTime'] == ua['modifyTime'] == ua['startTime'] == 1700000000000
    assert re.fullmatch('[a-f0-9]{32}', ua['uuid'])
    assert 'jPushId' not in ua


def test_simulated_device_reuses_identity_and_only_refreshes_start_time():
    first = simulated_ua(now_ms=1700000000000)
    second = simulated_ua(first, now_ms=1700000001000)
    assert second == dict(first, startTime=1700000001000)
    assert first['startTime'] == 1700000000000
    upgraded = simulated_ua(dict(first, version='4.0.0'), now_ms=1700000002000)
    assert upgraded['uuid'] == first['uuid']
    assert upgraded['version'] == '5.0.0' and upgraded['modifyTime'] == 1700000002000


def test_account_device_survives_store_restart_and_accounts_are_isolated(tmp_path):
    path = tmp_path / 'bot.sqlite3'
    first = json.loads(Store(path).login_device('example-account'))
    second = json.loads(Store(path).login_device('example-account'))
    other = json.loads(Store(path).login_device('another-account'))
    assert first['uuid'] == second['uuid'] and first['createTime'] == second['createTime']
    assert first['uuid'] != other['uuid']
    with Store(path).connection() as db:
        rows = db.execute('SELECT account_key,standard_ua FROM login_devices').fetchall()
    assert all('example-account' not in row['account_key'] and 'another-account' not in row['account_key'] for row in rows)


def test_new_password_accounts_use_all_models_before_repeating_and_balance_after(tmp_path):
    store = Store(tmp_path/'bot.sqlite3')
    count = len(load_profiles())
    generated = [json.loads(store.login_device(f'example-account-{index}')) for index in range(count*2+3)]
    assert len({ua['device'] for ua in generated[:count]}) == count
    assert len({ua['device'] for ua in generated[count:count*2]}) == count
    usage = Counter(ua['device'] for ua in generated)
    assert max(usage.values())-min(usage.values()) <= 1
    assert len({ua['uuid'] for ua in generated}) == len(generated)
    assert json.loads(store.login_device('example-account-0'))['device'] == generated[0]['device']


def test_legacy_password_device_is_preserved_without_loading_new_pool(tmp_path, monkeypatch):
    store = Store(tmp_path/'bot.sqlite3')
    legacy = {'device':'OnePlus PHK110','hardware':'qcom','sysVersion':'Android 36 16',
              'screenResolution':'1240-2633','uuid':'legacy-installation-uuid','createTime':1700000000000,
              'modifyTime':1700000000000,'startTime':1700000000000,'version':'5.0.0','extraField':'keep-me'}
    account_key = hashlib.sha256(b'legacy-account').hexdigest()
    with store.connection() as db:
        db.execute('INSERT INTO login_devices VALUES(?,?)',(account_key,json.dumps(legacy)))
    monkeypatch.setattr(dm_device,'load_profiles',lambda:pytest.fail('Existing identity must not be reassigned'))
    actual = json.loads(Store(store.path).login_device('legacy-account'))
    assert {k:v for k,v in actual.items() if k!='startTime'} == {k:v for k,v in legacy.items() if k!='startTime'}
    assert actual['startTime'] >= legacy['startTime']


def test_model_allocation_accounts_for_imported_devices_without_changing_or_double_counting_them(tmp_path):
    store = Store(tmp_path/'bot.sqlite3')
    count = len(load_profiles())
    imported = {'device':device_name(load_profiles()[0]),'uuid':'existing-installation','version':'5.0.0'}
    store.put_account('42',{'uid':'42','token':'example-token'},json.dumps(imported))
    generated = [json.loads(store.login_device(f'account-{index}')) for index in range(count-1)]
    assert imported['device'] not in {ua['device'] for ua in generated}
    store.put_account('99',{'uid':'99','token':'example-token'},json.dumps(generated[0]))
    second_cycle = [json.loads(store.login_device(f'second-account-{index}')) for index in range(count)]
    assert len({ua['device'] for ua in second_cycle}) == count
    assert json.loads(store.account('42')[1]) == imported


def test_existing_database_migrates_without_changing_accounts_grants_or_jobs(tmp_path):
    path = tmp_path/'bot.sqlite3'
    store = Store(path)
    profile = {'uid':'42','token':'test-token','version':'5.0.0'}
    store.put_account('42',profile,'{}')
    store.grant('42',77)
    job,_ = store.add_job('42','123',77,77,'Activity',1700000000)
    with store.connection() as db:
        db.execute('DROP TABLE login_devices')
        db.execute('DROP TABLE login_grants')
    upgraded = Store(path)
    assert upgraded.account('42')==(profile,'{}')
    assert upgraded.allowed('42',77,100000001)
    assert upgraded.job(job['id'])==store.job(job['id'])
    assert not upgraded.can_login(77,100000001)
    upgraded.grant_login(77)
    assert upgraded.can_login(77,100000001)
