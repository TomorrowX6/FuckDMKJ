import json
import re

from dm_device import simulated_ua
from dm_bot_store import Store


def test_simulated_device_has_coherent_template_without_claiming_push_registration():
    ua = simulated_ua(now_ms=1700000000000)
    assert ua['device'] == 'OnePlus PHK110' and ua['hardware'] == 'qcom'
    assert ua['system'] == 'android' and ua['sysVersion'] == 'Android 36 16'
    assert ua['screenResolution'] == '1240-2633'
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
