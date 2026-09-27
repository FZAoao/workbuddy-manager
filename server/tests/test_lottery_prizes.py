"""开学季抽奖中奖记录：脚本 stdout 抓取 + 落库 + 接口。

背景：上游调度器 `runScript` **不转发**子进程 stdout（容器日志里只有一行
`school: ok (...)`），所以抽奖中奖信息在容器日志里根本不存在，采集器无从解析。
面板触发开学季脚本时必须在 `taskrun._run` 里把脚本 stdout 接住、解析后落库，
否则用户永远看不到「我中了哪些券」。

本文件钉住三件事：
  1. `_capture_lottery` 只认真实中奖行（`draw -> <label>（balance <n>）`），
     不把「无一抽出奖」「未中奖」等行误记成中奖；
  2. `_record_lottery` 幂等（dedup_key）且只在开学季模式下写；
  3. `/api/lottery-prizes` 的列表 / 统计 / 改状态 / 删除。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault('WB_DATA_DIR', tempfile.mkdtemp())

from server import config, db, security  # noqa: E402
from server.services import taskrun, tencent  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class CaptureLotteryTest(unittest.TestCase):
    """从脚本输出行里抓中奖记录。"""

    def setUp(self) -> None:
        self._saved = list(taskrun._state['prizes'])
        taskrun._state['prizes'] = []

    def tearDown(self) -> None:
        taskrun._state['prizes'] = self._saved

    def test_voucher_line_captured(self) -> None:
        taskrun._capture_lottery(
            '[school2026] 2fb53fe4 draw -> 瑞幸咖啡15元券（balance 0）')
        self.assertEqual(len(taskrun._state['prizes']), 1)
        p = taskrun._state['prizes'][0]
        self.assertEqual(p['uid'], '2fb53fe4')
        self.assertEqual(p['label'], '瑞幸咖啡15元券')
        self.assertEqual(p['prize_code'], 'school_voucher_luckin')
        self.assertEqual(p['prize_type'], 'voucher')
        self.assertEqual(p['credit'], 0)

    def test_credit_line_captured(self) -> None:
        taskrun._capture_lottery(
            '[school2026] ab12cd34 draw -> 6积分（+6 Credit）（balance 3）')
        p = taskrun._state['prizes'][0]
        self.assertEqual(p['prize_type'], 'credit')
        self.assertEqual(p['credit'], 6)
        self.assertEqual(p['prize_code'], '')

    def test_unknown_prize_kept_without_fabricating_code(self) -> None:
        taskrun._capture_lottery(
            '[school2026] ab12cd34 draw -> 神秘大奖（balance 1）')
        p = taskrun._state['prizes'][0]
        self.assertEqual(p['label'], '神秘大奖')
        self.assertEqual(p['prize_code'], '')
        self.assertEqual(p['prize_type'], 'other')

    def test_non_winning_lines_are_ignored(self) -> None:
        for line in (
            '[school2026] ab12cd34 lottery 无一抽出奖（上次余额 0）',
            '[school2026] ab12cd34 draw http=409 code=40900（次数耗尽：no chance），按边界正常结束',
            '[school2026] ab12cd34 draw http=200 code=0 msg=x（未中奖，停）',
            '[school2026] ab12cd34 lottery balance=0，无需抽奖',
            'school2026 done: accounts=1 ok=1 already=0 skipped=0 pending=0 fail=0',
        ):
            taskrun._capture_lottery(line)
        self.assertEqual(taskrun._state['prizes'], [],
                         '非中奖行被误记成中奖了')


class RecordLotteryTest(unittest.TestCase):
    """中奖记录落库：幂等 + 模式限定。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 's.db'
        db._conn = None
        db.connect()
        self._saved_state = dict(taskrun._state)
        taskrun._state.update({'prizes': [], 'finished_at': 1700000000})

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        taskrun._state.clear()
        taskrun._state.update(self._saved_state)
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_records_only_in_school_modes(self) -> None:
        taskrun._state['prizes'] = [
            {'uid': 'u1', 'label': '瑞幸咖啡15元券',
             'prize_code': 'school_voucher_luckin', 'prize_type': 'voucher', 'credit': 0},
        ]
        taskrun._record_lottery('claim', 'ALL')   # 非开学季：不写
        self.assertEqual(db.count_lottery_prizes(), 0)
        taskrun._record_lottery('school', 'ALL')
        self.assertEqual(db.count_lottery_prizes(), 1)

    def test_dedup_same_run(self) -> None:
        taskrun._state['prizes'] = [
            {'uid': 'u1', 'label': '肯德基OK餐券',
             'prize_code': 'school_voucher_kfc_ok', 'prize_type': 'voucher', 'credit': 0},
        ]
        taskrun._record_lottery('school-lottery', 'ALL')
        taskrun._record_lottery('school-lottery', 'ALL')   # 重复解析同一轮
        self.assertEqual(db.count_lottery_prizes(), 1, '同一轮被重复入库')

    def test_two_same_prizes_same_run_both_recorded(self) -> None:
        taskrun._state['prizes'] = [
            {'uid': 'u1', 'label': '6积分', 'prize_code': '', 'prize_type': 'credit', 'credit': 6},
            {'uid': 'u1', 'label': '6积分', 'prize_code': '', 'prize_type': 'credit', 'credit': 6},
        ]
        taskrun._record_lottery('school-lottery', 'ALL')
        self.assertEqual(db.count_lottery_prizes(), 2,
                         '同一轮中了两个同名奖，应各记一条')

    def test_stats_and_update(self) -> None:
        taskrun._state['prizes'] = [
            {'uid': 'u1', 'label': '瑞幸咖啡15元券',
             'prize_code': 'school_voucher_luckin', 'prize_type': 'voucher', 'credit': 0},
            {'uid': 'u2', 'label': '66积分', 'prize_code': '', 'prize_type': 'credit', 'credit': 66},
        ]
        taskrun._record_lottery('school', 'ALL')
        stats = db.lottery_prize_stats()
        self.assertEqual(stats['total'], 2)
        self.assertEqual(stats['total_credit'], 66)
        self.assertEqual(stats['voucher_total'], 1)
        self.assertEqual(stats['voucher_redeemed'], 0)

        pid = next(p['id'] for p in db.list_lottery_prizes()
                   if p['prize_type'] == 'voucher')
        self.assertTrue(db.update_lottery_prize(pid, status='redeemed'))
        self.assertEqual(db.lottery_prize_stats()['voucher_redeemed'], 1)
        self.assertFalse(db.update_lottery_prize(999999, status='redeemed'))
        with self.assertRaises(ValueError):
            db.update_lottery_prize(pid, status='bogus')


class ParseAuthJsonTest(unittest.TestCase):
    """在线导入：把各种形态的授权 JSON 归一化。"""

    def test_workbuddy_format(self) -> None:
        out = tencent.parse_auth_json({
            'account': {'uid': 'abc', 'enterpriseId': 'e1', 'nickname': '13800138000'},
            'auth': {'accessToken': 'tok', 'refreshToken': 'r',
                     'expiresAt': 123, 'domain': 'copilot.tencent.com', 'realm': 'cn'},
        })
        self.assertEqual(out['uid'], 'abc')
        self.assertEqual(out['access_token'], 'tok')
        self.assertEqual(out['realm'], 'cn')
        self.assertEqual(out['expires_at'], 123)

    def test_flat_format_guesses_realm(self) -> None:
        cn = tencent.parse_auth_json({'uid': 'u1', 'access_token': 't', 'nickname': '13800138000'})
        self.assertEqual(cn['realm'], 'cn')
        gl = tencent.parse_auth_json({'uid': 'u2', 'access_token': 't', 'nickname': 'olga32663859'})
        self.assertEqual(gl['realm'], 'global')

    def test_array_and_wrapped(self) -> None:
        arr = tencent.parse_auth_json([{'uid': 'u3', 'access_token': 't'}])
        self.assertEqual(arr['uid'], 'u3')
        wrapped = tencent.parse_auth_json(
            {'uid': 'u4', 'data': {'accessToken': 't', 'domain': 'www.workbuddy.ai'}})
        self.assertEqual(wrapped['uid'], 'u4')
        self.assertEqual(wrapped['realm'], 'global')

    def test_missing_fields_rejected(self) -> None:
        with self.assertRaises(ValueError):
            tencent.parse_auth_json({'uid': 'u1'})           # 缺 token
        with self.assertRaises(ValueError):
            tencent.parse_auth_json({'access_token': 't'})   # 缺 uid
        with self.assertRaises(ValueError):
            tencent.parse_auth_json([])                      # 空数组


class LotteryEndpointTest(unittest.TestCase):
    """接口层：列表只读开放、改状态要 admin、删除 / 清空要会话管理员。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 'e.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S',
            'users': [
                {'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('p')},
                {'username': 'viewer', 'role': 'viewer', 'pwd_hash': security.make_hash('p')},
            ],
            'api_keys': [],
        })
        db.add_lottery_prizes([{
            'ts': 1700000000, 'uid': 'u1', 'label': '瑞幸咖啡15元券',
            'prize_code': 'school_voucher_luckin', 'prize_type': 'voucher',
            'credit': 0, 'source': 'school', 'status': 'pending',
            'dedup_key': 'k1',
        }])
        from server.main import app
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _login(self, username: str) -> None:
        r = self.client.post('/api/login', json={'username': username, 'password': 'p'})
        self.assertEqual(r.status_code, 200, r.text)

    def test_list_readable_by_viewer(self) -> None:
        self._login('viewer')
        r = self.client.get('/api/lottery-prizes')
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['total'], 1)
        self.assertEqual(body['items'][0]['label'], '瑞幸咖啡15元券')
        self.assertEqual(body['stats']['voucher_total'], 1)

    def test_update_requires_admin(self) -> None:
        pid = db.list_lottery_prizes()[0]['id']
        self._login('viewer')
        self.assertEqual(self.client.patch(
            f'/api/lottery-prizes/{pid}', json={'status': 'redeemed'}).status_code, 403)
        self._login('admin')
        r = self.client.patch(f'/api/lottery-prizes/{pid}', json={'status': 'redeemed'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(db.list_lottery_prizes()[0]['status'], 'redeemed')

    def test_delete_and_clear(self) -> None:
        pid = db.list_lottery_prizes()[0]['id']
        self._login('admin')
        self.assertEqual(self.client.delete(f'/api/lottery-prizes/{pid}').status_code, 200)
        self.assertEqual(db.count_lottery_prizes(), 0)
        self.assertEqual(self.client.delete('/api/lottery-prizes/999').status_code, 404)


class AccountImportEndpointTest(unittest.TestCase):
    """在线导入接口：multipart 上传、逐个处理、坏文件不中断整批。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        self._orig_auth = config.AUTH_DIR
        config.DB_PATH = Path(self._tmp.name) / 'e.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        config.AUTH_DIR = Path(self._tmp.name) / 'auths'
        config.AUTH_DIR.mkdir(parents=True, exist_ok=True)
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S',
            'users': [
                {'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('p')},
                {'username': 'viewer', 'role': 'viewer', 'pwd_hash': security.make_hash('p')},
            ],
            'api_keys': [],
        })
        from server.main import app
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        config.AUTH_DIR = self._orig_auth
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _login(self, username: str) -> None:
        r = self.client.post('/api/login', json={'username': username, 'password': 'p'})
        self.assertEqual(r.status_code, 200, r.text)

    def test_requires_admin(self) -> None:
        self._login('viewer')
        r = self.client.post('/api/accounts/import', files=[
            ('files', ('a.json', b'{"uid":"u1","access_token":"t"}', 'application/json'))])
        self.assertEqual(r.status_code, 403, r.text)

    def test_import_good_and_bad_mixed(self) -> None:
        self._login('admin')
        with mock.patch.object(taskrun, 'start_async', mock.AsyncMock()), \
             mock.patch('server.services.reload.request_reload_or_restart',
                        lambda *a, **k: True):
            r = self.client.post('/api/accounts/import', files=[
                ('files', ('good.json',
                           b'{"uid":"uid-1111","access_token":"tok",'
                           b'"nickname":"13800138000"}', 'application/json')),
                ('files', ('bad.json', b'not json', 'application/json')),
            ])
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['total'], 2)
        self.assertEqual(body['succeeded'], 1)
        self.assertEqual(body['failed'], 1)
        self.assertTrue((config.AUTH_DIR / 'workbuddy-uid-1111.json').is_file())
        # realm 按昵称（11 位手机号）判为 cn
        import json as _json
        saved = _json.loads((config.AUTH_DIR / 'workbuddy-uid-1111.json').read_text())
        self.assertEqual(saved['auth']['realm'], 'cn')

    def test_import_overwrites_same_uid(self) -> None:
        self._login('admin')
        with mock.patch('server.services.reload.request_reload_or_restart',
                        lambda *a, **k: True):
            first = self.client.post('/api/accounts/import', files=[
                ('files', ('a.json', b'{"uid":"dup","access_token":"t1"}', 'application/json'))])
            second = self.client.post('/api/accounts/import', files=[
                ('files', ('b.json', b'{"uid":"dup","access_token":"t2"}', 'application/json'))])
        self.assertEqual(first.json()['results'][0]['updated'], False)
        self.assertEqual(second.json()['results'][0]['updated'], True)


if __name__ == '__main__':
    unittest.main()
