"""公开账号补货页 API 与补货链接管理 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from typing import Literal

from .. import db, restocksvc, security
from ..iputil import client_ip
from ..services import accountimport, reload, tencent

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


class OAuthStartIn(BaseModel):
    realm: Literal['cn', 'global'] = 'cn'
    region: str | None = None


class OAuthPollIn(BaseModel):
    state: str = Field(min_length=1, max_length=256)


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


@public_router.post('/auth/start')
async def public_restock_auth_start(body: OAuthStartIn, request: Request) -> dict:
    link = _authorized_link(request)
    try:
        _, auth_dir = restocksvc.target_for_upstream(link.get('upstream_id'))
        restocksvc.check_rate_limit(int(link['id']), client_ip(request))
    except restocksvc.RestockError as exc:
        _raise(exc)

    region = str(body.region or '').strip().upper() or None
    valid_regions = {code for code, _ in tencent.INTERNATIONAL_REGIONS}
    if body.realm == 'global' and region not in valid_regions:
        raise HTTPException(status_code=400, detail='国际版登录请先选择有效地区')
    try:
        started = await tencent.start_login(body.realm)
        restocksvc.register_oauth_state(
            str(started.get('state') or ''), link, auth_dir, body.realm, region,
        )
    except restocksvc.RestockError as exc:
        _raise(exc)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail='获取授权链接失败，请稍后重试') from exc
    return {'state': started['state'], 'authUrl': started['authUrl'], 'realm': body.realm}


@public_router.post('/auth/poll')
async def public_restock_auth_poll(body: OAuthPollIn, request: Request) -> dict:
    link = _authorized_link(request)
    try:
        group, auth_dir = restocksvc.target_for_upstream(link.get('upstream_id'))
        session = restocksvc.begin_oauth_poll(body.state, link, auth_dir)
    except restocksvc.RestockError as exc:
        _raise(exc)
    if session is None:
        return {'status': 'waiting'}

    done = False
    try:
        try:
            result = await tencent.poll_login(body.state, session['realm'])
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail='查询授权状态失败，请稍后重试') from exc
        status = result.get('status')
        if status != 'ready':
            done = status in ('expired', 'invalid')
            return {'status': status if status in ('waiting', 'expired', 'invalid') else 'waiting'}

        # 腾讯返回的 access/refresh token 只能在服务端使用，公开响应只含结果摘要。
        # 国际版在落盘前完成地区注册；失败可重试同一 state。
        if session['realm'] == 'global':
            auth_probe = {
                'access_token': result['access_token'],
                'uid': result['uid'],
                'enterprise_id': result.get('enterprise_id', ''),
                'domain': result.get('domain', ''),
                'realm': 'global',
            }
            registered, _ = await tencent.registration_status(auth_probe)
            if not registered:
                registered, _ = await tencent.submit_region(auth_probe, session['region'])
                if not registered:
                    raise HTTPException(status_code=502, detail='国际版地区注册失败，请稍后重试')
            # 与后台登录一致：注册后尝试领取一次性 trial（接口幂等，失败不阻断落盘）。
            await tencent.claim_trial(auth_probe)

        ip = client_ip(request)
        try:
            # OAuth/地区注册期间会发生网络等待；落盘前重新检查链接与目标目录。
            link = restocksvc.resolve(_request_token(request))
            group, auth_dir = restocksvc.target_for_upstream(link.get('upstream_id'))
            if (link['upstream_id'] != session['upstream_id']
                    or str(auth_dir.resolve()) != session['auth_dir']):
                raise restocksvc.RestockError(409, '补货目标在授权期间发生变化，请重新登录')
            reserved = restocksvc.reserve_batch(
                int(link['id']), ip,
                expected_upstream_id=session['upstream_id'], check_target=True,
            )
        except restocksvc.RestockError as exc:
            _raise(exc)
        try:
            # 与 JSON 导入共用覆盖策略和原子落盘，含 disabled 文件的重复 UID 保护。
            _, existed = tencent.write_imported_auth(
                result, auth_dir, allow_overwrite=bool(reserved['allow_overwrite']),
            )
        except ValueError as exc:
            restocksvc.refund_batch(int(link['id']))
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OSError as exc:
            restocksvc.refund_batch(int(link['id']))
            raise HTTPException(status_code=500, detail='账号写入失败，请联系管理员检查账号目录权限') from exc

        done = True
        tencent.drop_state(body.state)
        reload.request_reload_or_restart(
            str(result['uid']), upstream=None if group.get('is_default') else group,
        )
        db.add_audit_log(
            f"restock:{int(link['id'])}", 'restock_oauth',
            str(group.get('name') or '默认上游'),
            f"uid={str(result['uid'])[:8]} realm={session['realm']} updated={existed}", ip,
        )
        return {
            'status': 'success',
            'uid': str(result['uid']),
            'nickname': str(result.get('nickname') or ''),
            'realm': session['realm'],
            'updated': existed,
        }
    finally:
        restocksvc.finish_oauth_poll(body.state, done=done)


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
