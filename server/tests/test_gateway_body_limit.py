"""请求体上限的回归（网关独立配置 + 读取时强制截断）。

旧版网关把上游遗留的 `server.max_body_mb` 当成本端上限；上游移除该限制后，
这个旧配置会意外拦截图片等较大的 JSON 请求。现在只使用网关自己的配置。

另一个漏洞：原先只看 `Content-Length` 判断大小。分块传输时该头缺失、也
可以伪造，因此超大请求体仍会被整段读进内存（正是当初要防的内存耗尽）。
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile
import unittest

from starlette.requests import Request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.routers import gateway  # noqa: E402


def _request(body: bytes, headers: dict | None = None) -> Request:
    scope = {
        'type': 'http',
        'method': 'POST',
        'path': '/v1/chat/completions',
        'headers': [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    sent = {'done': False}

    async def receive():
        if sent['done']:
            return {'type': 'http.disconnect'}
        sent['done'] = True
        return {'type': 'http.request', 'body': body, 'more_body': False}

    return Request(scope, receive)


class BodyLimitResolution(unittest.TestCase):
    """上限的取值来源。

    上游已移除 `server.max_body_mb` 的请求限制；即使遗留配置文件里还有该键，
    网关也只使用自己的 `WB_GATEWAY_MAX_BODY_MB`，避免旧值成为图片请求的隐形瓶颈。

    为什么本端不能也取消上限：上游是 Go，读的是流、能背压；本端要把完整 body
    读进内存再 json.loads（见 _read_json_body），几个并发大请求就能把内存吃光。
    保留上限是必要的，只是默认值要够大（32 MB），免得变成比上游更紧的隐性瓶颈。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = pathlib.Path(self._tmp.name) / 'config.json'
        self._orig = config.UPSTREAM_CONFIG
        config.UPSTREAM_CONFIG = self.cfg

    def tearDown(self) -> None:
        config.UPSTREAM_CONFIG = self._orig
        self._tmp.cleanup()

    def _mb(self, value) -> int:
        if value == 'missing':
            self.cfg.unlink(missing_ok=True)
        else:
            self.cfg.write_text(json.dumps(value), encoding='utf-8')
        return gateway.max_body_bytes() // 1024 // 1024

    def test_ignores_legacy_upstream_body_limit(self) -> None:
        self.assertEqual(self._mb({'server': {'max_body_mb': 1}}), gateway.DEFAULT_MAX_BODY_MB)
        self.assertEqual(self._mb({'server': {'max_body_mb': 256}}), gateway.DEFAULT_MAX_BODY_MB)

    def test_large_json_image_is_not_rejected_by_legacy_limit(self) -> None:
        self.cfg.write_text(json.dumps({'server': {'max_body_mb': 1}}), encoding='utf-8')
        body = json.dumps({'model': 'm', 'messages': [{'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {
                'url': 'data:image/png;base64,' + ('A' * (1024 * 1024 + 1))}},
        ]}]}).encode()
        parsed, error = asyncio.run(gateway._read_json_body(_request(body)))
        self.assertIsNone(error)
        self.assertEqual(parsed['messages'][0]['content'][0]['image_url']['url'],
                         'data:image/png;base64,' + ('A' * (1024 * 1024 + 1)))

    def test_missing_config_falls_back(self) -> None:
        """读不到配置不能导致拒绝服务，退回本端默认值。"""
        self.assertEqual(self._mb('missing'), gateway.DEFAULT_MAX_BODY_MB)

    def test_default_is_generous_after_upstream_removed_the_key(self) -> None:
        """上游移除该键后，本端默认值必须足够大，否则会成为隐性瓶颈。

        上游现在不限制请求体；本端若还按老默认 8 MB 拦，用户发个正常的大上下文
        请求就会被 413，而上游其实能处理 —— 且报错指的配置项在上游已不存在。
        """
        self.assertGreaterEqual(gateway.DEFAULT_MAX_BODY_MB, 16,
                                '默认值太小会让本端比上游更先拒绝')

    def test_new_upstream_without_the_key_uses_our_default(self) -> None:
        """新版上游的配置里没有 server 段 → 用本端默认值，不是「无限制」。"""
        self.assertEqual(self._mb({}), gateway.DEFAULT_MAX_BODY_MB)
        self.assertGreater(gateway.max_body_bytes(), 0, '上限不能变成 0（等于全拒）')

    def test_invalid_values_fall_back(self) -> None:
        for bad in ({'server': {}}, {'server': {'max_body_mb': 0}},
                    {'server': {'max_body_mb': -5}}, {'server': {'max_body_mb': 'x'}}, {}):
            self.assertEqual(self._mb(bad), gateway.DEFAULT_MAX_BODY_MB, repr(bad))

    def test_limit_comes_from_gateway_setting(self) -> None:
        original = gateway.DEFAULT_MAX_BODY_MB
        try:
            gateway.DEFAULT_MAX_BODY_MB = 7
            self.assertEqual(gateway.max_body_bytes(), 7 * 1024 * 1024)
        finally:
            gateway.DEFAULT_MAX_BODY_MB = original


class BodyLimitEnforcement(unittest.TestCase):
    """大小校验必须落在实际读取上，而不只是信任 Content-Length。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = pathlib.Path(self._tmp.name) / 'config.json'
        self._orig = config.UPSTREAM_CONFIG
        self._orig_default = gateway.DEFAULT_MAX_BODY_MB
        config.UPSTREAM_CONFIG = self.cfg
        gateway.DEFAULT_MAX_BODY_MB = 1
        # 设为 1 MiB，便于构造超限体
        self.cfg.write_text(json.dumps({'server': {'max_body_mb': 1}}), encoding='utf-8')
        self.big = json.dumps(
            {'model': 'x', 'messages': [{'role': 'user', 'content': 'a' * (2 * 1024 * 1024)}]}
        ).encode()
        self.small = json.dumps({'model': 'x', 'messages': []}).encode()

    def tearDown(self) -> None:
        config.UPSTREAM_CONFIG = self._orig
        gateway.DEFAULT_MAX_BODY_MB = self._orig_default
        self._tmp.cleanup()

    def _read(self, body: bytes, headers: dict | None = None):
        return asyncio.run(gateway._read_json_body(_request(body, headers)))

    def test_too_large_with_content_length(self) -> None:
        _, err = self._read(self.big, {'content-length': str(len(self.big))})
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 413)

    def test_too_large_without_content_length(self) -> None:
        """分块/缺头时原先可绕过，现在必须也拦。"""
        _, err = self._read(self.big, {})
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 413)

    def test_too_large_with_forged_content_length(self) -> None:
        """伪造偏小的 Content-Length 不能绕过。"""
        _, err = self._read(self.big, {'content-length': '100'})
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 413)

    def test_small_body_passes(self) -> None:
        body, err = self._read(self.small, {'content-length': str(len(self.small))})
        self.assertIsNone(err)
        self.assertIsInstance(body, dict)

    def test_non_json_is_400(self) -> None:
        _, err = self._read(b'not json', {'content-length': '8'})
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 400)

    def test_non_object_is_400(self) -> None:
        raw = json.dumps([1, 2, 3]).encode()
        _, err = self._read(raw, {'content-length': str(len(raw))})
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 400)

    def test_error_message_has_guidance(self) -> None:
        """报错要说清怎么改，而不是只丢一个 413。

        指导必须指向**真正能改的地方**：上游 9d1a21b 起移除了
        `server.max_body_mb`，再让用户去调那个键等于指错路（按上游文档怎么找
        都找不到）。现在指本网关自己的环境变量。
        """
        _, err = self._read(self.big, {})
        msg = json.loads(err.body)['error']['message']
        self.assertIn('上限', msg)
        self.assertIn('WB_GATEWAY_MAX_BODY_MB', msg,
                      '要指出本网关自己的环境变量（上游那个键已经不存在了）')


if __name__ == '__main__':
    unittest.main()
