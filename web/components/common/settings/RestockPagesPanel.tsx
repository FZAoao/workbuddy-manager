'use client';

import {useCallback, useEffect, useState} from 'react';
import {PackagePlus, Plus, Trash2, Copy, ExternalLink} from 'lucide-react';
import {errText, restockApi, upstreamsApi} from '@/lib/api';
import {useI18n} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import {BASE_PATH} from '@/lib/base-path';
import type {RestockPage, UpstreamEndpoint} from '@/lib/types';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {CopyButton} from '@/components/ui/copy-button';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

/** 有效期选项（与 TokensPanel 同一套档位；never 传 null）。 */
type Expires = 'never' | '7' | '30' | '90';

/**
 * 「设置 → 账号补货页」面板（仅管理员）。
 *
 * 生成一个独立导入链接：拿到链接的人打开 `/restock?token=…`，只能往该页
 * 绑定的分组导入账号 JSON，看不到管理后台的其它任何功能。token 存明文
 * （链接要能随时回来复制），列表里只显示前缀；「复制链接」按页现取明文。
 */
export function RestockPagesPanel() {
  const {t} = useI18n();
  const [pages, setPages] = useState<RestockPage[]>([]);
  const [groups, setGroups] = useState<UpstreamEndpoint[]>([]);
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState<RestockPage | null>(null);
  const [form, setForm] = useState<{name: string; group: string; expires: Expires}>(
    {name: '', group: 'default', expires: 'never'},
  );

  const load = useCallback(async () => {
    try {
      const [pagesRes, upstreamsRes] = await Promise.all([
        restockApi.list(),
        upstreamsApi.list(),
      ]);
      // 只有**配置了账号目录**的分组才有补货的意义：没有目录的分组连面板
      // 自己都不能添号（manageable 的语义），给它生成导入链接只会收到 409。
      setPages(pagesRes);
      setGroups(upstreamsRes.items.filter((g) => g.auth_dir));
    } catch (e) {
      notify.err(errText(e));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  /** 补货页的完整链接（浏览器侧拼 origin + basePath）。 */
  const linkOf = (token: string) =>
    `${window.location.origin}${BASE_PATH}/restock?token=${encodeURIComponent(token)}`;

  async function copyLink(id: number) {
    try {
      const {token} = await restockApi.link(id);
      await navigator.clipboard.writeText(linkOf(token));
      notify.ok(t('restock.admin.linkCopied'));
    } catch (e) {
      notify.err(errText(e));
    }
  }

  function openLink(id: number) {
    // 打开前现取明文：不在列表里长期持有完整 token（与「明文按页现取」一致）。
    restockApi.link(id)
      .then(({token}) => window.open(linkOf(token), '_blank', 'noopener'))
      .catch((e) => notify.err(errText(e)));
  }

  async function create() {
    if (!form.name.trim()) {
      notify.err(t('restock.admin.nameRequired'));
      return;
    }
    setBusy(true);
    try {
      const r = await restockApi.create({
        name: form.name.trim(),
        upstream_id: form.group === 'default' ? null : Number(form.group),
        // 「永不」传 null；其余按天数换算成绝对到期时刻
        expires_at: form.expires === 'never'
          ? null
          : Math.floor(Date.now() / 1000) + Number(form.expires) * 86400,
      });
      setCreated(r);
      notify.ok(t('restock.admin.created'));
      setForm({name: '', group: 'default', expires: 'never'});
      void load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      {/* ── 新建 ── */}
      <div className="rounded-[20px] bg-muted p-4">
        <div className="mb-1 text-sm font-medium">{t('restock.admin.new')}</div>
        <div className="mb-3 text-[11px] text-muted-foreground">
          {t('restock.admin.newDesc')}
        </div>
        <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-4">
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">
              {t('restock.admin.name')}
            </Label>
            <Input
              value={form.name}
              onChange={(e) => setForm({...form, name: e.target.value})}
              placeholder={t('restock.admin.namePlaceholder')}
              className="bg-background"
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">
              {t('restock.admin.group')}
            </Label>
            <Select value={form.group} onValueChange={(v) => setForm({...form, group: v})}>
              <SelectTrigger className="bg-background">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {groups.map((g) => (
                  <SelectItem key={g.id ?? 'default'} value={g.id == null ? 'default' : String(g.id)}>
                    {g.is_default ? t('accounts.groupDefault') : g.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">
              {t('restock.admin.expires')}
            </Label>
            <Select value={form.expires} onValueChange={(v) => setForm({...form, expires: v as Expires})}>
              <SelectTrigger className="bg-background">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="never">{t('restock.admin.expiresNever')}</SelectItem>
                <SelectItem value="7">{t('restock.admin.expires7')}</SelectItem>
                <SelectItem value="30">{t('restock.admin.expires30')}</SelectItem>
                <SelectItem value="90">{t('restock.admin.expires90')}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <Button className="rounded-full" disabled={busy} onClick={create}>
            <Plus />
            {t('restock.admin.create')}
          </Button>
        </div>
      </div>

      {/* ── 新建结果：明文 token（仅此一次，可随时用「复制链接」找回） ── */}
      {created && (
        <div className="rounded-[20px] border border-emerald-500/40 bg-emerald-500/5 p-4">
          <div className="mb-1 text-sm font-medium">{t('restock.admin.created')}</div>
          <div className="mb-2 text-[11px] text-muted-foreground">
            {t('restock.admin.createdDesc')}
          </div>
          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 truncate rounded-lg bg-background px-3 py-2 font-mono text-xs">
              {linkOf(created.token || '')}
            </code>
            <CopyButton value={linkOf(created.token || '')}
                        label={t('restock.admin.copyLink')} />
            <Button
              variant="ghost"
              size="sm"
              className="rounded-full text-xs"
              onClick={() => setCreated(null)}
            >
              {t('common.close')}
            </Button>
          </div>
        </div>
      )}

      <div className="rounded-[20px] bg-muted p-4 text-[11px] text-muted-foreground">
        {t('restock.admin.hint')}
      </div>

      {/* ── 列表 ── */}
      <div className="overflow-hidden rounded-[20px] bg-muted">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] text-muted-foreground">
                {t('restock.admin.colName')}
              </TableHead>
              <TableHead className="text-[11px] text-muted-foreground">
                {t('restock.admin.colGroup')}
              </TableHead>
              <TableHead className="text-[11px] text-muted-foreground">
                {t('restock.admin.colImported')}
              </TableHead>
              <TableHead className="text-[11px] text-muted-foreground">
                {t('restock.admin.colExpires')}
              </TableHead>
              <TableHead className="text-[11px] text-muted-foreground">
                {t('restock.admin.colLastUsed')}
              </TableHead>
              <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">
                {t('accounts.colActions')}
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {pages.map((p) => (
              <TableRow key={p.id} className="border-b border-border/40">
                <TableCell className="pl-4 text-sm font-medium">
                  {p.name || <span className="text-muted-foreground">#{p.id}</span>}
                  <div className="font-mono text-[10px] text-muted-foreground">
                    {p.token_prefix}…
                  </div>
                </TableCell>
                <TableCell>
                  <Badge variant="secondary" className="rounded-full">
                    {p.upstream_id == null
                      ? t('accounts.groupDefault')
                      : (groups.find((g) => g.id === p.upstream_id)?.name || `#${p.upstream_id}`)}
                  </Badge>
                  {!p.enabled && (
                    <Badge variant="secondary" className="ml-1 rounded-full">
                      {t('restock.admin.disabled')}
                    </Badge>
                  )}
                </TableCell>
                <TableCell className="text-sm tabular-nums">{p.imported_count}</TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {p.expires_at
                    ? new Date(p.expires_at * 1000).toLocaleString()
                    : t('restock.admin.expiresNever')}
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {p.last_used_at
                    ? new Date(p.last_used_at * 1000).toLocaleString()
                    : t('settings.tokensNeverUsed')}
                </TableCell>
                <TableCell className="pr-4 text-right">
                  <div className="flex justify-end gap-1">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 rounded-full text-xs"
                      title={t('restock.admin.copyLink')}
                      onClick={() => void copyLink(p.id)}
                    >
                      <Copy className="h-3.5 w-3.5" />
                      {t('restock.admin.copyLink')}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 rounded-full text-xs"
                      title={t('restock.admin.openLink')}
                      onClick={() => openLink(p.id)}
                    >
                      <ExternalLink className="h-3.5 w-3.5" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 rounded-full text-xs"
                      onClick={async () => {
                        try {
                          await restockApi.update(p.id, {enabled: !p.enabled});
                          void load();
                        } catch (e) {
                          notify.err(errText(e));
                        }
                      }}
                    >
                      {p.enabled
                        ? t('settings.tokensDisable')
                        : t('settings.tokensEnable')}
                    </Button>
                    <ConfirmDialog
                      title={t('restock.admin.deleteTitle', {name: p.name || `#${p.id}`})}
                      description={t('restock.admin.deleteDesc')}
                      confirmText={t('keys.delete')}
                      destructive
                      onConfirm={async () => {
                        try {
                          await restockApi.remove(p.id);
                          notify.ok(t('keys.deleted'));
                          void load();
                        } catch (e) {
                          notify.err(errText(e));
                        }
                      }}
                      trigger={
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7 rounded-md text-red-500 hover:text-red-600"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      }
                    />
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {!pages.length && (
          <EmptyState
            icon={PackagePlus}
            title={t('restock.admin.empty')}
            description={t('restock.admin.emptyDesc')}
            className="flex flex-col items-center justify-center py-12 text-center"
          />
        )}
      </div>
    </div>
  );
}
