"""账号授权 JSON 的公共导入流程。

管理员后台导入与公开补货入口只在「谁有权写哪个池」上不同；文件限制、JSON
兼容、原子落盘与上游热加载必须共用这一份实现，避免两条路径长期漂移。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from fastapi import UploadFile

from . import reload, tencent

MAX_FILES = 200
MAX_FILE_BYTES = 512 * 1024
MAX_REQUEST_BYTES = 16 * 1024 * 1024


class ImportRequestError(ValueError):
    """整批请求本身不合法（与单个文件失败区分）。"""


def _reload_target(group: dict) -> dict | None:
    return None if group.get('is_default') else group


async def import_uploads(
    files: Iterable[UploadFile],
    *,
    group: dict,
    auth_dir: Path,
    allow_overwrite: bool = True,
    expose_saved_file: bool = True,
    expose_storage_errors: bool = True,
) -> dict:
    """逐文件导入账号，并返回与现有后台接口兼容的汇总结构。

    一个文件失败不会中断其它文件。权限、目标分组选择和速率限制由路由层负责；
    这里仅处理已经被授权写入的目标目录。
    """
    uploads = list(files)
    if not uploads:
        raise ImportRequestError('没有收到任何文件')
    if len(uploads) > MAX_FILES:
        raise ImportRequestError(
            f'一次最多导入 {MAX_FILES} 个文件（收到 {len(uploads)} 个）'
        )

    results: list[dict] = []
    ok_uids: list[str] = []
    total_bytes = 0

    for index, uf in enumerate(uploads):
        name = str(uf.filename or 'account.json')[:255]
        try:
            raw_bytes = await uf.read()
        except Exception as exc:  # noqa: BLE001
            results.append({'file': name, 'ok': False, 'error': f'读取失败：{exc}'})
            continue

        total_bytes += len(raw_bytes)
        if total_bytes > MAX_REQUEST_BYTES:
            results.append({
                'file': name,
                'ok': False,
                'error': f'本批文件总大小超过 {MAX_REQUEST_BYTES // 1024 // 1024} MB 上限',
            })
            # 请求体中后续文件已经由 multipart 解析，但不再做 JSON/磁盘处理。
            for pending in uploads[index + 1:]:
                results.append({
                    'file': str(pending.filename or 'account.json')[:255],
                    'ok': False,
                    'error': f'本批文件总大小超过 {MAX_REQUEST_BYTES // 1024 // 1024} MB 上限',
                })
            break
        if len(raw_bytes) > MAX_FILE_BYTES:
            results.append({
                'file': name,
                'ok': False,
                'error': f'文件过大（上限 {MAX_FILE_BYTES // 1024} KB）',
            })
            continue

        try:
            parsed_json = json.loads(raw_bytes.decode('utf-8-sig'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            results.append({'file': name, 'ok': False, 'error': f'不是有效的 JSON：{exc}'})
            continue
        try:
            account = tencent.parse_auth_json(parsed_json)
            filename, existed = tencent.write_imported_auth(
                account,
                auth_dir,
                allow_overwrite=allow_overwrite,
            )
        except ValueError as exc:
            results.append({'file': name, 'ok': False, 'error': str(exc)})
            continue
        except OSError as exc:
            detail = (
                f'写入失败：{exc}。请检查 {auth_dir} 的目录权限'
                '（容器部署见 compose 里 chown 10001:10001 的说明）'
                if expose_storage_errors
                else '写入失败，请联系管理员检查账号目录权限'
            )
            results.append({'file': name, 'ok': False, 'error': detail})
            continue

        uid = str(account['uid'])
        ok_uids.append(uid)
        result = {
            'file': name,
            'ok': True,
            'uid': uid,
            'nickname': account.get('nickname') or '',
            'realm': account.get('realm') or 'cn',
            'updated': existed,
        }
        if expose_saved_file:
            result['saved_file'] = filename
        results.append(result)

    # 一次把整批成功 UID 纳入对应上游的热加载等待；调用立即返回。
    target = _reload_target(group)
    for uid in ok_uids:
        reload.request_reload_or_restart(uid, upstream=target)

    succeeded = sum(1 for item in results if item.get('ok'))
    return {
        'total': len(results),
        'succeeded': succeeded,
        'failed': len(results) - succeeded,
        'results': results,
        'upstream': {'id': group.get('id'), 'name': group.get('name') or '默认上游'},
    }
