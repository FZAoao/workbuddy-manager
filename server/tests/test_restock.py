"""账号补货页的回归测试（restocksvc + /api/restock* 端点）。

锁定的性质：

  * 明文 token 只在创建响应里出现一次；列表永不回传明文；
  * 「复制链接」入口按页现取明文（GET /{id}/link），**要会话管理员**；
  * 公开端点的鉴权 = URL 里的 token：不存在 / 已停用 / 已过期 都是同一个
    404，**不区分原因**（不给探测者信号）；
  * 导入只落在补货页**绑定**的分组：调用方传 upstream_id 会被忽略——
    「拿到链接的人不该能挑池」；
  * 成功导入后 imported_count 增加；再创建同 uid 仍计一次（覆盖更新）；
  * 管理端点（建 / 改 / 删）一律 `require_session_admin`：API Token 不放行。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, security  # noqa: E402

UID = 'aaaa9999-0000-0000-0000-00000000000a'


def _auth_json(uid: str) -> bytes:
    """一份「裸账号」形态的授权 JSON（parse_auth_json 认得的最简形态）。"""
    return json.dumps({
        'uid': uid,
        'access_token': 'AT-' + uid,
        'refresh_token': 'RT-' + uid,
        'nickname': '13800000000',
        'domain': 'copilot.tencent.com',
        'realm': 'cn',
    }, ensure_ascii=False).encode('utf-8')


class RestockTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls._orig = (config.AUTH_DIR, config.DB_PATH, config.USERS_FILE,
                     config.STATIC_DIR)
        config.AUTH_DIR = d / 'auths'
        config.AUTH_DIR.mkdir()
        config.DB_PATH = d / 'restock.db'
        config.USERS_FILE = d / 'users.json'
        config.STATIC_DIR = d / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('admin-pw')}],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()
        from server.main import app
        cls.c = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.AUTH_DIR, config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def setUp(self) -> None:
        security._fail.clear()
        security._user_fail.clear()
        self.c.cookies.clear()

    @contextmanager
    def _as_session(self):
        r = self.c.post('/api/login', json={'username': 'admin', 'password': 'admin-pw'})
        self.assertEqual(r.status_code, 200, r.text)
        try:
            yield self.c
        finally:
            self.c.cookies.clear()

    def _create(self, name: str = '测试补货', expires_at: int | None = None) -> dict:
        with self._as_session():
            r = self.c.post('/api/restock-pages',
                            json={'name': name, 'upstream_id': None,
                                  'expires_at': expires_at})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # ── 管理端 ────────────────────────────────────────────
    def test_create_returns_token_once_and_list_has_no_token(self) -> None:
        created = self._create('只此一次')
        self.assertTrue(created['token'].startswith('wbr_'))
        with self._as_session():
            lst = self.c.get('/api/restock-pages').json()
        self.assertTrue(lst)
        row = next(x for x in lst if x['id'] == created['id'])
        self.assertNotIn('token', row)
        self.assertEqual(row['token_prefix'], created['token'][:12])

    def test_admin_endpoints_reject_anonymous_and_token(self) -> None:
        # 未登录：401
        self.assertEqual(self.c.post('/api/restock-pages', json={}).status_code, 401)
        self.assertEqual(self.c.get('/api/restock-pages').status_code, 401)
        self.assertEqual(self.c.get('/api/restock-pages/1/link').status_code, 401)
        # API Token（admin scope）也不放行：发放凭据的动作只对会话开放
        from server import tokensvc
        minted = tokensvc.create_token('t', 'admin', None, 'admin')
        r = self.c.post('/api/restock-pages', json={},
                        headers={'Authorization': f'Bearer {minted["token"]}'})
        self.assertEqual(r.status_code, 403, r.text)
        r = self.c.get('/api/restock-pages/1/link',
                       headers={'Authorization': f'Bearer {minted["token"]}'})
        self.assertEqual(r.status_code, 403)

    def test_link_entry_returns_token(self) -> None:
        created = self._create('取回链接')
        with self._as_session():
            r = self.c.get(f"/api/restock-pages/{created['id']}/link")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['token'], created['token'])
        # 不存在的页：404
        with self._as_session():
            self.assertEqual(self.c.get('/api/restock-pages/999999/link').status_code, 404)

    def test_update_enable_and_delete(self) -> None:
        created = self._create('启停删')
        pid = created['id']
        with self._as_session():
            r = self.c.patch(f'/api/restock-pages/{pid}', json={'enabled': False})
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.json()['enabled'])
            r = self.c.patch(f'/api/restock-pages/{pid}', json={'expires_at': 0})
            self.assertEqual(r.status_code, 200)
            self.assertIsNone(r.json()['expires_at'])
            # 改成空名：400
            self.assertEqual(
                self.c.patch(f'/api/restock-pages/{pid}', json={'name': '  '}).status_code,
                400)
            r = self.c.delete(f'/api/restock-pages/{pid}')
            self.assertEqual(r.status_code, 200)
            self.assertEqual(self.c.delete(f'/api/restock-pages/{pid}').status_code, 404)

    def test_create_rejects_unknown_group(self) -> None:
        with self._as_session():
            r = self.c.post('/api/restock-pages',
                            json={'name': 'x', 'upstream_id': 424242})
        self.assertEqual(r.status_code, 404)

    # ── 公开端点 ──────────────────────────────────────────
    def test_public_info_requires_valid_token(self) -> None:
        self.assertEqual(self.c.get('/api/restock/wbr_nope').status_code, 404)
        # 别的形态的 token 一律 404（不 422 / 不 500）
        self.assertEqual(self.c.get('/api/restock/hello').status_code, 404)

    def test_disabled_and_expired_pages_are_the_same_404(self) -> None:
        created = self._create('停用页')
        pid, token = created['id'], created['token']
        with self._as_session():
            self.c.patch(f'/api/restock-pages/{pid}', json={'enabled': False})
        self.assertEqual(self.c.get(f'/api/restock/{token}').status_code, 404)
        # 过期同理
        created = self._create('过期页', expires_at=int(time.time()) - 10)
        self.assertEqual(
            self.c.get(f"/api/restock/{created['token']}").status_code, 404)
        r = self.c.post(f"/api/restock/{created['token']}/import",
                        files={'files': ('a.json', _auth_json(UID), 'application/json')})
        self.assertEqual(r.status_code, 404)

    def test_info_shows_group_and_zero_count(self) -> None:
        created = self._create('页面信息')
        r = self.c.get(f"/api/restock/{created['token']}")
        self.assertEqual(r.status_code, 200)
        info = r.json()
        self.assertEqual(info['imported_count'], 0)
        self.assertEqual(info['upstream']['id'], None)
        self.assertEqual(info['upstream']['name'], '默认上游')

    def test_import_lands_in_bound_group_and_counts(self) -> None:
        created = self._create('导入计数')
        token = created['token']
        r = self.c.post(
            f'/api/restock/{token}/import',
            files={'files': ('acc.json', _auth_json(UID), 'application/json')},
            # 调用方想指定分组：**必须被忽略**
            data={'upstream_id': '424242'},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['succeeded'], 1)
        self.assertEqual(body['imported_count'], 1)
        # 文件落在默认目录，且能被上游加载器读出
        target = config.AUTH_DIR / f'workbuddy-{UID}.json'
        self.assertTrue(target.exists())
        raw = json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual(raw['account']['uid'], UID)
        self.assertEqual(raw['auth']['realm'], 'cn')
        # 再导一次（同 uid）：覆盖更新，计数仍 +1
        r = self.c.post(
            f'/api/restock/{token}/import',
            files={'files': ('acc.json', _auth_json(UID), 'application/json')},
        )
        self.assertEqual(r.json()['imported_count'], 2)
        info = self.c.get(f'/api/restock/{token}').json()
        self.assertEqual(info['imported_count'], 2)

    def test_import_bad_file_does_not_count(self) -> None:
        created = self._create('坏文件')
        r = self.c.post(
            f"/api/restock/{created['token']}/import",
            files={'files': ('bad.json', b'not json', 'application/json')},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['succeeded'], 0)
        self.assertEqual(body['imported_count'], 0)
        self.assertTrue(body['results'][0]['error'])

    def test_delete_revokes_link_immediately(self) -> None:
        created = self._create('即删即失效')
        token = created['token']
        self.assertEqual(self.c.get(f'/api/restock/{token}').status_code, 200)
        with self._as_session():
            self.c.delete(f"/api/restock-pages/{created['id']}")
        self.assertEqual(self.c.get(f'/api/restock/{token}').status_code, 404)


if __name__ == '__main__':
    unittest.main()
