"""公开补货链接的生成、校验与最小权限边界。

补货 Token 只授权一件事：向它绑定的账号池提交账号 JSON。它不是管理 API Token，
不能读取账号列表、切换目标池或调用任何其它管理接口。
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import deque
from pathlib import Path

from . import db, upstreamsvc

TOKEN_PREFIX = 'wbr_'
TOKEN_HITS_PER_MINUTE = 10
IP_HITS_PER_MINUTE = 20

_rate_lock = threading.Lock()
_token_hits: dict[int, deque[float]] = {}
_ip_hits: dict[str, deque[float]] = {}


class RestockError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _normalize_upstream_id(value: object) -> int | None:
    if value in (None, '', 0, '0'):
        return None
    try:
        uid = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError('目标账号池 id 不合法') from exc
    if uid <= 0:
        raise ValueError('目标账号池 id 不合法')
    return uid


def target_for_upstream(upstream_id: object) -> tuple[dict, Path]:
    """解析并验证目标池，绝不在目标缺失时回落默认池。"""
    uid = _normalize_upstream_id(upstream_id)
    group = upstreamsvc.default_upstream() if uid is None else upstreamsvc.get_upstream(uid)
    if group is None:
        raise RestockError(409, '补货链接绑定的账号池不存在，请联系管理员')
    if not group.get('enabled', True):
        raise RestockError(409, '补货链接绑定的账号池已停用，请联系管理员')
    raw_dir = str(group.get('auth_dir') or '').strip()
    if not raw_dir:
        raise RestockError(409, '目标账号池没有配置本地账号目录，请联系管理员')
    return group, Path(raw_dir)


def _parse(row) -> dict:
    upstream_id = row['upstream_id']
    group = upstreamsvc.default_upstream() if upstream_id is None else upstreamsvc.get_upstream(upstream_id)
    return {
        'id': int(row['id']),
        'name': str(row['name']),
        'prefix': str(row['prefix']),
        'enabled': bool(row['enabled']),
        'upstream_id': int(upstream_id) if upstream_id is not None else None,
        'upstream_name': str(group.get('name')) if group else f'已删除的账号池 #{upstream_id}',
        'target_available': bool(
            group and group.get('enabled', True)
            and str(group.get('auth_dir') or '').strip()
        ),
        'expires_at': row['expires_at'],
        'max_batches': int(row['max_batches'] or 0),
        'used_batches': int(row['used_batches'] or 0),
        'allow_overwrite': bool(row['allow_overwrite']),
        'created_at': int(row['created_at']),
        'created_by': str(row['created_by'] or ''),
        'last_used_at': row['last_used_at'],
        'last_used_ip': row['last_used_ip'],
    }


def list_links() -> list[dict]:
    return [_parse(row) for row in db.query('SELECT * FROM restock_links ORDER BY id DESC')]


def create_link(
    name: str,
    *,
    upstream_id: object = None,
    expires_at: int | None = None,
    max_batches: int = 0,
    allow_overwrite: bool = False,
    enabled: bool = True,
    created_by: str = '',
) -> dict:
    cleaned_name = str(name or '').strip()
    if not cleaned_name:
        raise ValueError('补货链接名称不能为空')
    uid = _normalize_upstream_id(upstream_id)
    # 创建时就验证目标池，避免生成一个注定不可用且容易被误发出去的链接。
    target_for_upstream(uid)
    exp = int(expires_at) if expires_at else None
    if exp is not None and exp <= int(time.time()):
        raise ValueError('过期时间必须晚于当前时间')
    batches = int(max_batches or 0)
    if batches < 0 or batches > 1_000_000:
        raise ValueError('最大批次数必须在 0 到 1000000 之间（0 表示不限）')

    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    link_id = db.execute(
        'INSERT INTO restock_links('
        'name, token_hash, prefix, enabled, upstream_id, expires_at, max_batches, '
        'used_batches, allow_overwrite, created_at, created_by'
        ') VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)',
        (cleaned_name[:64], _hash(token), token[:12], 1 if enabled else 0, uid,
         exp, batches, 1 if allow_overwrite else 0, int(time.time()),
         str(created_by or '')[:64]),
    )
    out = _parse(db.query_one('SELECT * FROM restock_links WHERE id = ?', (link_id,)))
    out['token'] = token  # 明文仅创建时返回一次
    return out


def update_link(link_id: int, patch: dict) -> dict | None:
    row = db.query_one('SELECT * FROM restock_links WHERE id = ?', (link_id,))
    if row is None:
        return None
    fields: dict[str, object] = {}
    if 'name' in patch:
        name = str(patch.get('name') or '').strip()
        if not name:
            raise ValueError('补货链接名称不能为空')
        fields['name'] = name[:64]
    if 'enabled' in patch:
        fields['enabled'] = 1 if patch['enabled'] else 0
    if 'upstream_id' in patch:
        uid = _normalize_upstream_id(patch['upstream_id'])
        target_for_upstream(uid)
        fields['upstream_id'] = uid
    if 'expires_at' in patch:
        value = patch['expires_at']
        exp = int(value) if value else None
        if exp is not None and exp <= int(time.time()):
            raise ValueError('过期时间必须晚于当前时间')
        fields['expires_at'] = exp
    if 'max_batches' in patch:
        batches = int(patch['max_batches'] or 0)
        if batches < 0 or batches > 1_000_000:
            raise ValueError('最大批次数必须在 0 到 1000000 之间（0 表示不限）')
        if batches and batches < int(row['used_batches'] or 0):
            raise ValueError('最大批次数不能小于已经使用的批次数')
        fields['max_batches'] = batches
    if 'allow_overwrite' in patch:
        fields['allow_overwrite'] = 1 if patch['allow_overwrite'] else 0
    if fields:
        assignments = ', '.join(f'{key} = ?' for key in fields)
        db.execute(f'UPDATE restock_links SET {assignments} WHERE id = ?',
                   (*fields.values(), link_id))
    return _parse(db.query_one('SELECT * FROM restock_links WHERE id = ?', (link_id,)))


def delete_link(link_id: int) -> bool:
    if db.query_one('SELECT id FROM restock_links WHERE id = ?', (link_id,)) is None:
        return False
    db.execute('DELETE FROM restock_links WHERE id = ?', (link_id,))
    with _rate_lock:
        _token_hits.pop(link_id, None)
    return True


def resolve(token: object) -> dict:
    """校验明文补货 Token；失败不会返回数据库哈希或其它候选信息。"""
    if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
        raise RestockError(401, '补货链接无效')
    digest = _hash(token)
    matched = None
    for row in db.query('SELECT * FROM restock_links WHERE prefix = ?', (token[:12],)):
        if secrets.compare_digest(str(row['token_hash']), digest):
            matched = row
            break
    if matched is None:
        raise RestockError(401, '补货链接无效')
    now = int(time.time())
    if not matched['enabled']:
        raise RestockError(403, '补货链接已停用')
    if matched['expires_at'] and int(matched['expires_at']) <= now:
        raise RestockError(410, '补货链接已过期')
    max_batches = int(matched['max_batches'] or 0)
    if max_batches and int(matched['used_batches'] or 0) >= max_batches:
        raise RestockError(403, '补货链接的可用批次已用完')
    return dict(matched)


def reserve_batch(link_id: int, ip: str) -> dict:
    """原子占用一次批次，避免并发请求越过 max_batches。"""
    conn = db.connect()
    now = int(time.time())
    with db._lock:
        try:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM restock_links WHERE id = ?', (link_id,)).fetchone()
            if row is None:
                raise RestockError(401, '补货链接无效')
            if not row['enabled']:
                raise RestockError(403, '补货链接已停用')
            if row['expires_at'] and int(row['expires_at']) <= now:
                raise RestockError(410, '补货链接已过期')
            maximum = int(row['max_batches'] or 0)
            used = int(row['used_batches'] or 0)
            if maximum and used >= maximum:
                raise RestockError(403, '补货链接的可用批次已用完')
            conn.execute(
                'UPDATE restock_links SET used_batches = used_batches + 1, '
                'last_used_at = ?, last_used_ip = ? WHERE id = ?',
                (now, str(ip or '')[:64], link_id),
            )
            fresh = conn.execute('SELECT * FROM restock_links WHERE id = ?', (link_id,)).fetchone()
            conn.commit()
            return dict(fresh)
        except Exception:
            conn.rollback()
            raise


def check_rate_limit(link_id: int, ip: str) -> None:
    """进程内一分钟滑窗限流；反代层仍可叠加更严格的全局限制。"""
    now = time.monotonic()
    cutoff = now - 60.0
    ip_key = str(ip or 'unknown')[:64]
    with _rate_lock:
        token_q = _token_hits.setdefault(link_id, deque())
        ip_q = _ip_hits.setdefault(ip_key, deque())
        while token_q and token_q[0] <= cutoff:
            token_q.popleft()
        while ip_q and ip_q[0] <= cutoff:
            ip_q.popleft()
        if len(token_q) >= TOKEN_HITS_PER_MINUTE or len(ip_q) >= IP_HITS_PER_MINUTE:
            raise RestockError(429, '上传过于频繁，请稍后再试')
        token_q.append(now)
        ip_q.append(now)
