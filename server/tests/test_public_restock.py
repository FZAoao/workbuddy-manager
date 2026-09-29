"""公开补货链接：最小权限、账号 JSON 兼容与配额边界回归测试。"""
from __future__ import annotations

import asyncio
import concurrent.futures
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import UploadFile  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, restocksvc, security, upstreamsvc  # noqa: E402
from server.services import accountimport, reload, tencent  # noqa: E402


def _account(uid: str, *, access: str | None = None, refresh: str | None = None) -> dict:
    return {
        'account': {'uid': uid, 'nickname': f'昵称-{uid}', 'enterpriseId': ''},
        'auth': {
            'accessToken': access or f'AT-{uid}',
            'refreshToken': refresh or f'RT-{uid}',
            'expiresAt': int(time.time()) + 3600,
            'domain': 'copilot.tencent.com',
            'realm': 'cn',
        },
        'device_token': f'DT-{uid}',
    }


def _file(name: str, payload: object) -> tuple[str, tuple[str, bytes, str]]:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return ('files', (name, raw, 'application/json'))


class PublicRestockTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig = (
            config.AUTH_DIR,
            config.DB_PATH,
            config.USERS_FILE,
            config.STATIC_DIR,
        )
        config.AUTH_DIR = root / 'default-auths'
        config.AUTH_DIR.mkdir()
        config.DB_PATH = root / 'restock.db'
        config.USERS_FILE = root / 'users.json'
        config.STATIC_DIR = root / 'no-static'
        security.save_users({
            'secret': 'S' * 64,
            'users': [{
                'username': 'admin',
                'role': 'admin',
                'pwd_hash': security.make_hash('pw'),
            }],
            'api_keys': [],
        })
        db._conn = None
        db.connect()
        with restocksvc._rate_lock:
            restocksvc._token_hits.clear()
            restocksvc._ip_hits.clear()
        from server.main import app
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.AUTH_DIR, config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = self._orig
        with restocksvc._rate_lock:
            restocksvc._token_hits.clear()
            restocksvc._ip_hits.clear()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _mint(self, **kwargs) -> dict:
        return restocksvc.create_link('公开补货', created_by='admin', **kwargs)

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {'X-Restock-Token': token}

    def _upload(self, token: str, files: list, **kwargs):
        headers = dict(kwargs.pop('headers', {}))
        headers.update(self._headers(token))
        return self.client.post('/api/public/restock/import', files=files,
                                headers=headers, **kwargs)

    def _login(self) -> None:
        response = self.client.post('/api/login', json={'username': 'admin', 'password': 'pw'})
        self.assertEqual(response.status_code, 200, response.text)

    def test_missing_wrong_and_bearer_token(self) -> None:
        self.assertEqual(self.client.get('/api/public/restock/info').status_code, 401)
        self.assertEqual(self.client.get(
            '/api/public/restock/info', headers=self._headers('wbr_wrong')).status_code, 401)

        created = self._mint()
        response = self.client.get('/api/public/restock/info', headers={
            'Authorization': f"Bearer {created['token']}",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['upstream']['name'], '默认上游')

    def test_disabled_expired_and_exhausted_tokens_have_distinct_status(self) -> None:
        disabled = self._mint(enabled=False)
        self.assertEqual(self.client.get(
            '/api/public/restock/info', headers=self._headers(disabled['token'])).status_code, 403)

        expired = self._mint()
        db.execute('UPDATE restock_links SET expires_at = ? WHERE id = ?',
                   (int(time.time()) - 1, expired['id']))
        self.assertEqual(self.client.get(
            '/api/public/restock/info', headers=self._headers(expired['token'])).status_code, 410)

        exhausted = self._mint(max_batches=1)
        db.execute('UPDATE restock_links SET used_batches = 1 WHERE id = ?', (exhausted['id'],))
        response = self.client.get('/api/public/restock/info',
                                   headers=self._headers(exhausted['token']))
        self.assertEqual(response.status_code, 403)
        self.assertIn('批次', response.json()['detail'])

    def test_missing_disabled_or_unmanaged_target_never_falls_back_to_default(self) -> None:
        target_dir = Path(self._tmp.name) / 'group-auths'
        target_dir.mkdir()
        group = upstreamsvc.create_upstream(
            '分组甲', 'http://127.0.0.1:7991', auth_dir=str(target_dir))
        created = self._mint(upstream_id=group['id'])

        upstreamsvc.update_upstream(group['id'], {'auth_dir': ''})
        response = self.client.get('/api/public/restock/info',
                                   headers=self._headers(created['token']))
        self.assertEqual(response.status_code, 409)
        self.assertIn('本地账号目录', response.json()['detail'])

        upstreamsvc.update_upstream(group['id'], {'auth_dir': str(target_dir), 'enabled': False})
        response = self.client.get('/api/public/restock/info',
                                   headers=self._headers(created['token']))
        self.assertEqual(response.status_code, 409)
        self.assertIn('已停用', response.json()['detail'])
        self.assertFalse(restocksvc.list_links()[0]['target_available'])

        db.execute('DELETE FROM upstreams WHERE id = ?', (group['id'],))
        response = self.client.get('/api/public/restock/info',
                                   headers=self._headers(created['token']))
        self.assertEqual(response.status_code, 409)
        self.assertIn('不存在', response.json()['detail'])
        self.assertEqual(list(config.AUTH_DIR.glob('*.json')), [])

    def test_client_cannot_override_token_bound_target(self) -> None:
        group_dir = Path(self._tmp.name) / 'other-auths'
        group_dir.mkdir()
        group = upstreamsvc.create_upstream(
            '其它池', 'http://127.0.0.1:7992', auth_dir=str(group_dir))
        created = self._mint()  # 绑定默认池
        uid = 'bound-target-001'
        response = self._upload(
            created['token'],
            [_file('account.json', _account(uid))],
            params={'upstream_id': group['id']},
            data={'upstream_id': str(group['id'])},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue((config.AUTH_DIR / f'workbuddy-{uid}.json').exists())
        self.assertFalse((group_dir / f'workbuddy-{uid}.json').exists())

    def test_supported_json_shapes_import_as_one_batch(self) -> None:
        created = self._mint()
        nested = _account('shape-nested')
        bare = {
            'uid': 'shape-bare', 'access_token': 'AT-bare',
            'refresh_token': 'RT-bare', 'nickname': '裸账号', 'realm': 'global',
        }
        array = [{'uid': 'shape-array', 'accessToken': 'AT-array'}]
        data = {'uid': 'shape-data', 'data': {'accessToken': 'AT-data'}}
        result = {'result': {'uid': 'shape-result', 'access_token': 'AT-result'}}
        with mock.patch.object(reload, 'request_reload_or_restart', return_value=True) as trigger:
            response = self._upload(created['token'], [
                _file('nested.json', nested),
                _file('bare.json', bare),
                _file('array.json', array),
                _file('data.json', data),
                _file('result.json', result),
            ])
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body['total'], body['succeeded'], body['failed']), (5, 5, 0))
        self.assertTrue(all('saved_file' not in item for item in body['results']))
        self.assertEqual(trigger.call_count, 5)
        self.assertEqual({call.args[0] for call in trigger.call_args_list}, {
            'shape-nested', 'shape-bare', 'shape-array', 'shape-data', 'shape-result',
        })

    def test_invalid_files_are_isolated_and_reload_only_successes(self) -> None:
        created = self._mint()
        with mock.patch.object(reload, 'request_reload_or_restart', return_value=True) as trigger:
            response = self._upload(created['token'], [
                _file('good.json', _account('mixed-good')),
                _file('bad.json', b'{not-json'),
                _file('no-uid.json', {'accessToken': 'AT'}),
                _file('no-token.json', {'uid': 'mixed-no-token'}),
            ])
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body['succeeded'], body['failed']), (1, 3))
        errors = '\n'.join(item.get('error', '') for item in body['results'])
        self.assertIn('不是有效的 JSON', errors)
        self.assertIn('缺少 uid', errors)
        self.assertIn('缺少 accessToken', errors)
        trigger.assert_called_once_with('mixed-good', upstream=None)

    def test_file_count_file_size_and_total_size_limits(self) -> None:
        created = self._mint()
        too_many = [_file(f'{i}.json', {'uid': f'u{i}', 'accessToken': 'AT'})
                    for i in range(accountimport.MAX_FILES + 1)]
        response = self._upload(created['token'], too_many)
        self.assertEqual(response.status_code, 400)
        self.assertIn(str(accountimport.MAX_FILES), response.json()['detail'])

        oversized = b'x' * (accountimport.MAX_FILE_BYTES + 1)
        response = self._upload(created['token'], [_file('large.json', oversized)])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['failed'], 1)
        self.assertIn('文件过大', response.json()['results'][0]['error'])

        # 每个文件都没有超过单文件上限，但第 33 个使整批超过 16 MiB。
        chunk = b'x' * accountimport.MAX_FILE_BYTES
        response = self._upload(created['token'], [
            _file(f'chunk-{i}.json', chunk) for i in range(33)
        ])
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['total'], 33)
        self.assertIn('总大小超过', body['results'][-1]['error'])

    def test_total_size_abort_uses_upload_index_even_after_read_failure(self) -> None:
        class BrokenUpload:
            filename = 'broken.json'

            async def read(self):
                raise OSError('boom')

        uploads = [
            BrokenUpload(),
            UploadFile(filename='huge.json', file=io.BytesIO(
                b'x' * (accountimport.MAX_REQUEST_BYTES + 1))),
            UploadFile(filename='later.json', file=io.BytesIO(b'{}')),
        ]
        result = asyncio.run(accountimport.import_uploads(
            uploads,
            group=upstreamsvc.default_upstream(),
            auth_dir=config.AUTH_DIR,
        ))
        self.assertEqual(result['total'], 3)
        self.assertEqual([item['file'] for item in result['results']],
                         ['broken.json', 'huge.json', 'later.json'])
        self.assertIn('读取失败', result['results'][0]['error'])
        self.assertTrue(all('总大小超过' in item['error'] for item in result['results'][1:]))

    def test_default_no_overwrite_and_explicit_overwrite_link(self) -> None:
        uid = 'overwrite-001'
        existing = config.AUTH_DIR / f'workbuddy-{uid}.json'
        existing.write_text(json.dumps(_account(uid, access='AT-old')), encoding='utf-8')

        safe = self._mint()
        response = self._upload(safe['token'], [
            _file('new.json', _account(uid, access='AT-new')),
        ])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['failed'], 1)
        self.assertIn('不允许覆盖', response.json()['results'][0]['error'])
        self.assertIn('AT-old', existing.read_text())

        overwrite = self._mint(allow_overwrite=True)
        response = self._upload(overwrite['token'], [
            _file('new.json', _account(uid, access='AT-new')),
        ])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['succeeded'], 1)
        self.assertTrue(response.json()['results'][0]['updated'])
        self.assertIn('AT-new', existing.read_text())

    def test_disabled_file_also_blocks_default_overwrite(self) -> None:
        uid = 'disabled-existing'
        disabled = config.AUTH_DIR / f'workbuddy-{uid}.json.disabled'
        disabled.write_text(json.dumps(_account(uid)), encoding='utf-8')
        created = self._mint()
        response = self._upload(created['token'], [_file('same.json', _account(uid))])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['failed'], 1)
        self.assertFalse((config.AUTH_DIR / f'workbuddy-{uid}.json').exists())

    def test_concurrent_default_imports_cannot_race_into_overwrite(self) -> None:
        account = tencent.parse_auth_json(_account('concurrent-no-overwrite'))
        original_write = tencent._atomic_write_json

        def slow_write(target, payload):
            # 扩大“检查不存在 → 落盘”之间的竞态窗口；没有导入锁时多个线程会同时成功。
            time.sleep(0.03)
            return original_write(target, payload)

        def submit(_index: int) -> bool:
            try:
                tencent.write_imported_auth(
                    account,
                    config.AUTH_DIR,
                    allow_overwrite=False,
                )
                return True
            except ValueError:
                return False

        with mock.patch.object(tencent, '_atomic_write_json', side_effect=slow_write):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                outcomes = list(pool.map(submit, range(8)))

        self.assertEqual(outcomes.count(True), 1)
        self.assertEqual(outcomes.count(False), 7)
        self.assertTrue((config.AUTH_DIR / 'workbuddy-concurrent-no-overwrite.json').exists())

    def test_batch_quota_is_atomic_and_info_reports_remaining(self) -> None:
        created = self._mint(max_batches=1)
        info = self.client.get('/api/public/restock/info',
                               headers=self._headers(created['token']))
        self.assertEqual(info.json()['remaining_batches'], 1)
        first = self._upload(created['token'], [_file('a.json', _account('batch-one'))])
        self.assertEqual(first.status_code, 200, first.text)
        second = self._upload(created['token'], [_file('b.json', _account('batch-two'))])
        self.assertEqual(second.status_code, 403)
        row = db.query_one('SELECT used_batches FROM restock_links WHERE id = ?', (created['id'],))
        self.assertEqual(row['used_batches'], 1)

    def test_rate_limit_applies_per_token(self) -> None:
        created = self._mint()
        old = restocksvc.TOKEN_HITS_PER_MINUTE
        restocksvc.TOKEN_HITS_PER_MINUTE = 1
        try:
            first = self._upload(created['token'], [_file('a.json', _account('rate-one'))])
            self.assertEqual(first.status_code, 200, first.text)
            second = self._upload(created['token'], [_file('b.json', _account('rate-two'))])
            self.assertEqual(second.status_code, 429)
        finally:
            restocksvc.TOKEN_HITS_PER_MINUTE = old

    def test_audit_log_never_contains_credentials(self) -> None:
        created = self._mint()
        uid = 'audit-secret-uid'
        secrets = ['ACCESS-SHOULD-NOT-LOG', 'REFRESH-SHOULD-NOT-LOG', 'DEVICE-SHOULD-NOT-LOG']
        payload = _account(uid, access=secrets[0], refresh=secrets[1])
        payload['device_token'] = secrets[2]
        response = self._upload(created['token'], [_file('secret.json', payload)])
        self.assertEqual(response.status_code, 200, response.text)
        logs = json.dumps(db.list_audit_logs(20), ensure_ascii=False)
        for value in secrets:
            self.assertNotIn(value, logs)
        self.assertNotIn(uid, logs)
        self.assertIn(uid[:8], logs)

    def test_admin_crud_requires_session_and_token_is_only_returned_once(self) -> None:
        self.assertEqual(self.client.get('/api/restock-links').status_code, 401)
        self._login()
        response = self.client.post('/api/restock-links', json={
            'name': '临时补货', 'max_batches': 3, 'allow_overwrite': False,
        })
        self.assertEqual(response.status_code, 200, response.text)
        created = response.json()
        self.assertTrue(created['token'].startswith('wbr_'))

        listed = self.client.get('/api/restock-links')
        self.assertEqual(listed.status_code, 200, listed.text)
        serialized = json.dumps(listed.json())
        self.assertNotIn(created['token'], serialized)
        self.assertNotIn('token_hash', serialized)

        patched = self.client.patch(f"/api/restock-links/{created['id']}", json={
            'enabled': False, 'name': '已停用补货',
        })
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertFalse(patched.json()['enabled'])
        deleted = self.client.delete(f"/api/restock-links/{created['id']}")
        self.assertEqual(deleted.status_code, 200, deleted.text)


if __name__ == '__main__':
    unittest.main()
