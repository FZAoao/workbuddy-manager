"""公开账号补货页 API 与补货链接管理 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from .. import db, restocksvc, security
from ..iputil import client_ip
from ..services import accountimport

public_router = APIRouter(prefix='/api/public/restock', tags=['public-restock'])
admin_router = APIRouter(prefix='/api/restock-links', tags=['restock-links'])


class RestockLinkIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    upstream_id: int | None = None
    expires_at: int | None = None
    max_batches: int = Field(default=0, ge=0, le=1_000_000)
    allow_overwrite: bool = False
    enabled: bool = True


class RestockLinkPatch(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    upstream_id: int | None = None
    expires_at: int | None = None
    max_batches: int | None = Field(default=None, ge=0, le=1_000_000)
    allow_overwrite: bool | None = None
    enabled: bool | None = None


def _raise(exc: restocksvc.RestockError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _request_token(request: Request) -> str:
    token = str(request.headers.get('x-restock-token') or '').strip()
    if token:
        return token
    authorization = str(request.headers.get('authorization') or '')
    scheme, _, value = authorization.partition(' ')
    return value.strip() if scheme.lower() == 'bearer' else ''


def _authorized_link(request: Request) -> dict:
    try:
        return restocksvc.resolve(_request_token(request))
    except restocksvc.RestockError as exc:
        _raise(exc)


@admin_router.get('')
def list_restock_links(
    user: dict = Depends(security.require_session_admin),
) -> list[dict]:
    return restocksvc.list_links()


@admin_router.post('')
def create_restock_link(
    body: RestockLinkIn,
    request: Request,
    user: dict = Depends(security.require_session_admin),
) -> dict:
    try:
        created = restocksvc.create_link(
            body.name,
            upstream_id=body.upstream_id,
            expires_at=body.expires_at,
            max_batches=body.max_batches,
            allow_overwrite=body.allow_overwrite,
            enabled=body.enabled,
            created_by=user.get('username', ''),
        )
    except restocksvc.RestockError as exc:
        _raise(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    security.audit(
        user,
        'create_restock_link',
        created['name'],
        f"账号池={created['upstream_name']}；覆盖={created['allow_overwrite']}；来源 {client_ip(request)}",
    )
    return created


@admin_router.patch('/{link_id}')
def update_restock_link(
    link_id: int,
    body: RestockLinkPatch,
    request: Request,
    user: dict = Depends(security.require_session_admin),
) -> dict:
    patch = body.model_dump(exclude_unset=True)
    try:
        updated = restocksvc.update_link(link_id, patch)
    except restocksvc.RestockError as exc:
        _raise(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail='补货链接不存在')
    security.audit(
        user,
        'update_restock_link',
        updated['name'],
        f"字段={','.join(sorted(patch)) or '无'}；来源 {client_ip(request)}",
    )
    return updated


@admin_router.delete('/{link_id}')
def delete_restock_link(
    link_id: int,
    request: Request,
    user: dict = Depends(security.require_session_admin),
) -> dict:
    current = next((item for item in restocksvc.list_links() if item['id'] == link_id), None)
    if not restocksvc.delete_link(link_id):
        raise HTTPException(status_code=404, detail='补货链接不存在')
    security.audit(
        user,
        'delete_restock_link',
        (current or {}).get('name', str(link_id)),
        f'来源 {client_ip(request)}',
    )
    return {'ok': True}


@public_router.get('/info')
def public_restock_info(request: Request) -> dict:
    link = _authorized_link(request)
    try:
        group, _ = restocksvc.target_for_upstream(link.get('upstream_id'))
    except restocksvc.RestockError as exc:
        _raise(exc)
    return {
        'valid': True,
        'name': str(link.get('name') or '账号补货'),
        'upstream': {'id': group.get('id'), 'name': group.get('name')},
        'limits': {
            'max_files': accountimport.MAX_FILES,
            'max_file_bytes': accountimport.MAX_FILE_BYTES,
            'max_request_bytes': accountimport.MAX_REQUEST_BYTES,
        },
        'expires_at': link.get('expires_at'),
        'allow_overwrite': bool(link.get('allow_overwrite')),
        'remaining_batches': (
            max(0, int(link['max_batches']) - int(link['used_batches']))
            if int(link.get('max_batches') or 0) else None
        ),
    }


@public_router.post('/import')
async def public_restock_import(
    request: Request,
    files: list[UploadFile] = File(...),
) -> dict:
    link = _authorized_link(request)
    ip = client_ip(request)
    try:
        group, auth_dir = restocksvc.target_for_upstream(link.get('upstream_id'))
        restocksvc.check_rate_limit(int(link['id']), ip)
        reserved = restocksvc.reserve_batch(int(link['id']), ip)
    except restocksvc.RestockError as exc:
        _raise(exc)

    try:
        result = await accountimport.import_uploads(
            files,
            group=group,
            auth_dir=auth_dir,
            allow_overwrite=bool(reserved.get('allow_overwrite')),
            expose_saved_file=False,
            expose_storage_errors=False,
        )
    except accountimport.ImportRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 只记录脱敏 UID 前缀与计数；严禁把原 JSON 或任何 auth token 写进日志。
    uid_prefixes = [str(item.get('uid') or '')[:8] for item in result['results'] if item.get('ok')]
    db.add_audit_log(
        f"restock:{int(link['id'])}",
        'restock_import',
        str(group.get('name') or '默认上游'),
        f"文件={result['total']} 成功={result['succeeded']} 失败={result['failed']} "
        f"uid={','.join(uid_prefixes[:20])}",
        ip,
    )
    return result
