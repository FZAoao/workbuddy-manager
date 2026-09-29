# 公开账号补货页

公开补货页用于把“提供账号 JSON 的人”和“拥有管理后台权限的人”分开：管理员先创建一条只绑定到某个账号池的补货链接，拿到链接的人无需登录后台，只能向该账号池批量上传账号授权 JSON，不能读取账号列表、查看已有凭据或改变目标账号池。

## 管理员怎么创建

1. 登录管理后台，打开 **设置 → 补货链接**。
2. 填写链接名称并选择目标账号池。
3. 可选设置过期时间、最大可提交批次数，以及是否允许覆盖已有 UID。
4. 创建后立即复制公开 URL。明文 Token 只在创建响应中展示一次，刷新页面后无法找回；需要重新分享时请删除旧链接并新建。

目标账号池必须已启用，并配置了本地账号目录。多上游部署可以为不同账号池分别创建链接，访客不能通过请求参数把账号写到别的池。

## 补货人员怎么使用

打开管理员发来的 `/restock/?token=...` 链接后，直接拖入或选择多个 `.json` 文件并提交即可。页面会逐文件显示成功或失败原因；一个坏文件不会中断同批其它文件。

支持与后台“在线导入”相同的常见格式：

- workbuddy2api 的嵌套账号文件；
- 裸账号 JSON；
- 单元素账号数组；
- 带 `data` 或 `result` 包壳的登录响应。

成功提示“账号已写入，账号池正在加载”表示账号文件已经安全落盘，并已请求对应上游热加载；它不承诺账号会立即在线。账号最终是否可用仍取决于授权有效性、上游加载状态和账号自身限制。

## 默认安全边界

- Token 形如 `wbr_...`，数据库只保存 SHA-256 哈希和短前缀，不保存明文。
- 公开上传请求通过 `X-Restock-Token` 请求头鉴权，也兼容 `Authorization: Bearer wbr_...`。
- 公开接口不接受可信的 `upstream_id`；写入目标始终取自管理员创建链接时绑定的账号池。
- 默认不覆盖已有 UID；正常文件与 `.disabled` 文件都视为“账号已存在”。管理员可以按链接显式开启覆盖。
- 单批最多 200 个文件、单文件最多 512 KB、所有文件净大小合计最多 16 MB。
- 默认每个补货 Token 每分钟最多 10 批、每个来源 IP 每分钟最多 20 批。进程外还应在反向代理或 WAF 配置总速率限制。
- 审计日志只记录文件数、成功/失败数和 UID 前 8 位，不记录账号 JSON、access token、refresh token、device token 或补货明文 Token。
- 创建、修改、停用和删除补货链接只接受管理员浏览器会话，管理 API Token 不能操作这些接口。

## URL Token 的注意事项

页面首次读取 `?token=...` 后，会把 Token 暂存在当前标签页的 `sessionStorage`，并立即从地址栏移除；后续 API 请求不会再把 Token 放在 query 中。

但是，最初打开的完整 URL 仍可能已经进入：

- 浏览器历史或同步记录；
- 反向代理、CDN、WAF 的访问日志；
- 分享工具或聊天软件的链接预览记录。

因此补货链接应使用 HTTPS，通过可信渠道发送，并尽量设置过期时间或最大批次数。一旦怀疑泄露，立即在 **设置 → 补货链接** 中停用或删除；删除后原链接立即失效且无法恢复。

## 接口概览

管理接口（管理员会话）：

```text
GET    /api/restock-links
POST   /api/restock-links
PATCH  /api/restock-links/{id}
DELETE /api/restock-links/{id}
```

公开接口（补货 Token）：

```text
GET  /api/public/restock/info
POST /api/public/restock/import
```

上传示例：

```bash
curl https://wb.example.com/api/public/restock/import \
  -H 'X-Restock-Token: wbr_xxxxx' \
  -F 'files=@account-1.json;type=application/json' \
  -F 'files=@account-2.json;type=application/json'
```
