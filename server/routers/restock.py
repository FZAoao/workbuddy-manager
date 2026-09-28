"""账号补货页接口：管理端 CRUD（管理员）+ 公开导入端点（凭 URL 里的 token）。

**两套 router 分开挂**（与 redpackets 的 admin / claim 同一形态）：上面一组走
`require_session_admin`（生成 / 停用 / 删除补货页是发放凭据的动作）；下面一组
**公开**、鉴权完全靠路径里的 token —— 它就是「拿到链接就能补货」这个承诺的
落地，不该要求登录。

公开端的防滥用：
  * token 128 位熵（token_urlsafe(32)），猜不出来；
  * 文件数与单文件大小沿用管理端导入的上限（`_IMPORT_MAX_FILES` /
    `_IMPORT_MAX_BYTES`，直接复用常量，两边口径不会漂移）；
  * 解析与落盘走 `tencent.parse_auth_json` / `tencent.write_imported_auth`，
    与扫码 / 面板导入是同一套校验（uid 字符集、realm 解析、原子写）。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from .. import db, restocksvc, security
from ..iputil import client_ip
from ..services import reload, tencent

# 文件数与单文件大小上限**直接复用**管理端「在线导入」的常量（`accounts.py`
# 的模块级定义）：两边口径必须一致，抄一份迟早漂移。
from .accounts import _IMPORT_MAX_BYTES, _IMPORT_MAX_FILES  # noqa: F401

router = APIRouter(prefix='/api/restock-pages', tags=['restock'])

# 公开端点：鉴权 = URL 路径里的 token（wbr_…），没有它什么都做不了。
public_router = APIRouter(prefix='/api/restock', tags=['restock'])


class PageIn(BaseModel):
    name: str = Field(default='', max_length=64)
    # 绑定的分组；null = 默认分组（与 upstreamsvc.DEFAULT_ID 同约定）
    upstream_id: int | None = None
    # epoch 秒；null / 0 = 永不过期
    expires_at: int | None = None


class PagePatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    expires_at: int | None = None


# ── 管理端（会话管理员）──────────────────────────────────

@router.get('')
def list_pages(user: dict = Depends(security.require_session_admin)) -> list[dict]:
    """全部补货页。**不含明文 token**——链接从「复制链接」入口按页取。"""
    return restocksvc.list_pages()


@router.get('/{page_id}/link')
def get_link(page_id: int,
             user: dict = Depends(security.require_session_admin)) -> dict:
    """取某个补货页的完整链接（含明文 token）。需要管理员：token 就是凭据。"""
    row = db.query_one('SELECT * FROM restock_pages WHERE id = ?', (page_id,))
    if not row:
        raise HTTPException(status_code=404, detail='补货页不存在')
    return {'token': row['token']}


@router.post('')
def create_page(body: PageIn, request: Request,
                user: dict = Depends(security.require_session_admin)) -> dict:
    """创建补货页。返回体里的 `token` 是**明文，仅此一次**（拼链接用）。"""
    if body.upstream_id is not None:
        from .. import upstreamsvc
        if upstreamsvc.get_upstream(body.upstream_id) is None:
            raise HTTPException(status_code=404, detail='分组不存在（可能已被删除）')
    try:
        created = restocksvc.create_page(
            body.name, body.upstream_id, body.expires_at,
            created_by=str(user.get('username') or ''),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    security.audit(user, 'create_restock_page', created['name'] or str(created['id']),
                   f"id={created['id']} 分组={created['upstream_id']}；"
                   f'来源 {client_ip(request)}')
    return created


@router.patch('/{page_id}')
def update_page(page_id: int, body: PagePatch, request: Request,
                user: dict = Depends(security.require_session_admin)) -> dict:
    patch = body.model_dump(exclude_unset=True)
    if 'name' in patch and patch['name'] is not None and not str(patch['name']).strip():
        raise HTTPException(status_code=400, detail='名称不能只有空格')
    updated = restocksvc.update_page(page_id, patch)
    if not updated:
        raise HTTPException(status_code=404, detail='补货页不存在')
    security.audit(user, 'update_restock_page', updated['name'] or str(page_id),
                   f"字段={','.join(sorted(patch)) or '无'}；来源 {client_ip(request)}")
    return updated


@router.delete('/{page_id}')
def delete_page(page_id: int, request: Request,
                user: dict = Depends(security.require_session_admin)) -> dict:
    row = db.query_one('SELECT name FROM restock_pages WHERE id = ?', (page_id,))
    if not restocksvc.delete_page(page_id):
        raise HTTPException(status_code=404, detail='补货页不存在')
    security.audit(user, 'delete_restock_page',
                   str((row or {})['name'] if row else page_id),
                   f'来源 {client_ip(request)}')
    return {'ok': True}


# ── 公开端点（凭 URL 里的 token）────────────────────────

def _authed_page(token: str) -> dict:
    """解析并校验 URL 里的 token。失败一律 404（不区分原因，不给探测者信号）。"""
    row = restocksvc.resolve(token)
    if not row or not restocksvc.usable(row):
        raise HTTPException(status_code=404, detail='链接无效或已过期')
    return row


@public_router.get('/{token}')
def restock_info(token: str) -> dict:
    """补货页要显示的信息：分组名、已导入数量、有效期。**不含任何账号内容**。"""
    _authed_page(token)
    info = restocksvc.page_info(token)
    assert info is not None  # 上面已保证可用
    return info


@public_router.post('/{token}/import')
async def restock_import(token: str, request: Request,
                         files: list[UploadFile] = File(...),
                         upstream_id: int | None = Body(None)) -> dict:
    """往补货页绑定的分组导入账号 JSON。与面板「在线导入」同一套解析与落盘。

    upstream_id **不接受调用方指定**（签名里保留该形参只为吞掉误传、避免
    FastAPI 报 422）：目标分组一律以补货页自己的绑定为准——链接拿到的是
    「往某个池补货」这个能力，不该能挑池。
    """
    del upstream_id  # 刻意忽略：见 docstring
    row = _authed_page(token)
    from ..routers.accounts import _group, _require_dir
    group = _group(row['upstream_id'])
    base_dir = _require_dir(group)

    if not files:
        raise HTTPException(status_code=400, detail='没有收到任何文件')
    if len(files) > _IMPORT_MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f'一次最多导入 {_IMPORT_MAX_FILES} 个文件（收到 {len(files)} 个）',
        )

    results: list[dict] = []
    ok_uids: list[str] = []
    for uf in files:
        name = str(uf.filename or 'account.json')
        try:
            raw_bytes = await uf.read()
        except Exception as exc:  # noqa: BLE001
            results.append({'file': name, 'ok': False, 'error': f'读取失败：{exc}'})
            continue
        if len(raw_bytes) > _IMPORT_MAX_BYTES:
            results.append({
                'file': name, 'ok': False,
                'error': f'文件过大（上限 {_IMPORT_MAX_BYTES // 1024} KB）',
            })
            continue
        try:
            parsed_json = json.loads(raw_bytes.decode('utf-8-sig'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            results.append({'file': name, 'ok': False, 'error': f'不是有效的 JSON：{exc}'})
            continue
        try:
            account = tencent.parse_auth_json(parsed_json)
        except ValueError as exc:
            results.append({'file': name, 'ok': False, 'error': str(exc)})
            continue
        try:
            filename, existed = tencent.write_imported_auth(account, base_dir)
        except ValueError as exc:
            results.append({'file': name, 'ok': False, 'error': str(exc)})
            continue
        except OSError as exc:
            results.append({
                'file': name, 'ok': False,
                'error': (f'写入失败：{exc}。请检查 {base_dir} 的目录权限'
                          '（容器部署见 compose 里 chown 10001:10001 的说明）'),
            })
            continue
        ok_uids.append(account['uid'])
        results.append({
            'file': name, 'ok': True,
            'uid': account['uid'],
            'nickname': account.get('nickname') or '',
            'realm': account.get('realm') or 'cn',
            'saved_file': filename,
            'updated': existed,
        })

    # 收尾与面板导入一致：新 uid 交给热加载 / 重启（按分组定向）。
    from ..routers.accounts import _reload_target
    for uid in ok_uids:
        reload.request_reload_or_restart(uid, upstream=_reload_target(group))

    succeeded = sum(1 for r in results if r.get('ok'))
    if succeeded:
        restocksvc.touch(int(row['id']))
    return {
        'total': len(results),
        'succeeded': succeeded,
        'failed': len(results) - succeeded,
        'results': results,
        # 页面据此刷新「已导入数量」
        'imported_count': restocksvc.page_info(token)['imported_count'],
    }
