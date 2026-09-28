"""账号补货页：管理员生成独立导入链接，凭 URL 里的 token 导入账号。

使用场景：账号授权 JSON 由别人保管（同事、上游渠道），管理员不想把管理后台
的账号密码发出去，也不想自己当中转。给这件事生成一个**只做导入**的链接：
拿到链接的人打开补货页、拖入 JSON 即可，见不到其它任何管理功能。

与既有凭据的关系（刻意为之的形态）：

  * 与 `tokensvc`（管理面 API Token）不同源、不同前缀（`wbr_` ≠ `wbt_`）：
    它授权的不是管理接口，而是一个**能力单一**的页面——「往某个分组导入
    账号」。塞进 api_tokens 会让它平白获得一个 scope 档位（以及被当成
    管理凭据误用的可能），单独一张表反而让边界一眼可见。
  * token **存明文**（与红包抽奖码 / red_packet_shares.token 同一取舍）：
    链接要能随时回来复制、再发给下一个补货的人；只存哈希的话弄丢就只能
    删了重建。而它允许的动作并不高于库里已有的明文凭据。
  * 失败**不区分原因**（不存在 / 已停用 / 已过期都是同一个 404）——不给
    探测者「这个码存在但停用了」之类的信号，与 tokensvc.resolve 的口径一致。

每页绑定一个**分组**（upstream_id，NULL = 默认分组）：导入的账号落到该分组
的账号目录，与面板里的多账号池语义一致。
"""
from __future__ import annotations

import hashlib
import secrets
import time

from . import db

# 与网关密钥 `wbk_`、管理面令牌 `wbt_` 刻意区分：看到前缀就知道凭据能干什么。
TOKEN_PREFIX = 'wbr_'
MAX_NAME = 64
MAX_TOKENS = 200  # 最多同时存在多少个补货页（防把表灌满，与 _IMPORT_MAX_FILES 同思路）


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _row(r) -> dict:
    return {
        'id': r['id'],
        'name': r['name'],
        # 列表给**前缀**，不给明文 —— 明文只在创建时返回一次，
        # 之后从「补货页链接」入口随时可取（这正是存明文的意义）。
        'token_prefix': r['token_prefix'],
        'upstream_id': r['upstream_id'],
        'enabled': bool(r['enabled']),
        'expires_at': r['expires_at'],
        'imported_count': int(r['imported_count']),
        'created_by': r['created_by'],
        'created_at': r['created_at'],
        'last_used_at': r['last_used_at'],
    }


def _clean(value: object, limit: int) -> str:
    return str(value or '').strip()[:limit]


def list_pages() -> list[dict]:
    """全部补货页（不含明文 token）。"""
    return [_row(r) for r in db.query('SELECT * FROM restock_pages ORDER BY id DESC')]


def create_page(name: str, upstream_id: int | None, expires_at: int | None,
                created_by: str = '') -> dict:
    """创建一个补货页。返回体里的 `token` 是**明文，仅此一次**（拼链接用）。"""
    if len(list_pages()) >= MAX_TOKENS:
        raise ValueError(f'补货页数量已达上限（{MAX_TOKENS} 个），请先删除不用的')
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    pid = db.execute(
        'INSERT INTO restock_pages(name, token, token_prefix, upstream_id, enabled, '
        'expires_at, created_by, created_at) VALUES(?, ?, ?, ?, 1, ?, ?, ?)',
        (_clean(name, MAX_NAME), token, token[:12], upstream_id,
         int(expires_at) if expires_at else None,
         _clean(created_by, 64), int(time.time())),
    )
    out = _row(db.query_one('SELECT * FROM restock_pages WHERE id = ?', (pid,)))
    out['token'] = token  # 仅此一次
    return out


def update_page(page_id: int, patch: dict) -> dict | None:
    """部分更新（PATCH 语义：只改真的提交了的字段）。不存在返回 None。"""
    if not db.query_one('SELECT id FROM restock_pages WHERE id = ?', (page_id,)):
        return None
    fields: dict[str, object] = {}
    if 'name' in patch and patch['name']:
        fields['name'] = _clean(patch['name'], MAX_NAME)
    if 'enabled' in patch:
        fields['enabled'] = 1 if patch['enabled'] else 0
    if 'expires_at' in patch:
        # 显式传 null/0 表示「改为永不过期」——不能用真值判断（与 tokensvc 同口径）
        v = patch['expires_at']
        fields['expires_at'] = int(v) if v else None
    if fields:
        assignments = ', '.join(f'{k} = ?' for k in fields)
        db.execute(f'UPDATE restock_pages SET {assignments} WHERE id = ?',
                   (*fields.values(), page_id))
    return _row(db.query_one('SELECT * FROM restock_pages WHERE id = ?', (page_id,)))


def delete_page(page_id: int) -> bool:
    if not db.query_one('SELECT id FROM restock_pages WHERE id = ?', (page_id,)):
        return False
    db.execute('DELETE FROM restock_pages WHERE id = ?', (page_id,))
    return True


def resolve(token: object) -> dict | None:
    """校验明文 token：先按前缀定位候选行，再常量时间比较哈希。

    返回行字典（含 token 明文，仅内部使用）。**不存在 / 已停用 / 已过期**
    一律返回 None —— 调用方回同一个 404，不给探测者信号。
    """
    if not token or not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
        return None
    digest = _hash(token)
    for row in db.query('SELECT * FROM restock_pages WHERE token_prefix = ?',
                        (token[:12],)):
        if secrets.compare_digest(_hash(str(row['token'])), digest):
            return dict(row)
    return None


def usable(row: dict) -> bool:
    """补货页是否可用：已启用，且未过期（无 expires_at = 永不过期）。"""
    if not row.get('enabled'):
        return False
    exp = row.get('expires_at')
    return not exp or int(exp) > int(time.time())


def touch(page_id: int) -> None:
    """成功导入后记账：次数 +1、最近使用时刻。旁路，失败不影响请求。"""
    try:
        db.execute(
            'UPDATE restock_pages SET imported_count = imported_count + 1, '
            'last_used_at = ? WHERE id = ?',
            (int(time.time()), page_id),
        )
    except Exception:  # noqa: BLE001
        pass


def page_info(token: str) -> dict | None:
    """补货页要展示的元信息（分组名等）。不可用返回 None。"""
    row = resolve(token)
    if not row or not usable(row):
        return None
    from . import upstreamsvc
    group = (upstreamsvc.get_upstream(row['upstream_id'])
             if row['upstream_id'] is not None else upstreamsvc.default_upstream())
    return {
        'name': row['name'],
        'upstream': {'id': group['id'], 'name': group['name']},
        'expires_at': row['expires_at'],
        'imported_count': int(row['imported_count']),
    }
