'use client';

import {useCallback, useEffect, useMemo, useState} from 'react';
import {
  ExternalLink,
  Link2,
  Loader2,
  PackagePlus,
  Save,
  ShieldAlert,
  Trash2,
} from 'lucide-react';
import {
  errText,
  restockLinksApi,
  upstreamsApi,
  type RestockLinkWrite,
} from '@/lib/api';
import {BASE_PATH} from '@/lib/base-path';
import {fmtDateTime} from '@/lib/format';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {CreatedRestockLink, RestockLink, UpstreamEndpoint} from '@/lib/types';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {CopyButton} from '@/components/ui/copy-button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {Switch} from '@/components/ui/switch';

type Draft = {
  name: string;
  upstreamId: string;
  expires: string;
  maxBatches: string;
  allowOverwrite: boolean;
};

const EMPTY: Draft = {
  name: '',
  upstreamId: 'default',
  expires: '',
  maxBatches: '0',
  allowOverwrite: false,
};

function toLocalInput(ts: number | null): string {
  if (!ts) return '';
  const date = new Date(ts * 1000);
  const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return shifted.toISOString().slice(0, 16);
}

function draftOf(link: RestockLink): Draft {
  return {
    name: link.name,
    upstreamId: link.upstream_id == null ? 'default' : String(link.upstream_id),
    expires: toLocalInput(link.expires_at),
    maxBatches: String(link.max_batches),
    allowOverwrite: link.allow_overwrite,
  };
}

function writeOf(draft: Draft): RestockLinkWrite {
  const parsedExpiry = draft.expires ? Math.floor(new Date(draft.expires).getTime() / 1000) : null;
  return {
    name: draft.name.trim(),
    upstream_id: draft.upstreamId === 'default' ? null : Number(draft.upstreamId),
    expires_at: Number.isFinite(parsedExpiry) ? parsedExpiry : null,
    max_batches: Math.max(0, Number.parseInt(draft.maxBatches || '0', 10) || 0),
    allow_overwrite: draft.allowOverwrite,
  };
}

function TargetSelect({
  value,
  onChange,
  upstreams,
}: {
  value: string;
  onChange: (value: string) => void;
  upstreams: UpstreamEndpoint[];
}) {
  const t = useT();
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger className="bg-background"><SelectValue /></SelectTrigger>
      <SelectContent>
        {upstreams.map((upstream) => (
          <SelectItem
            key={upstream.id ?? 'default'}
            value={upstream.id == null ? 'default' : String(upstream.id)}
            disabled={!upstream.enabled || !upstream.auth_dir}
          >
            {upstream.name}
            {(!upstream.enabled || !upstream.auth_dir) ? ` · ${t('restock.adminTargetUnavailable')}` : ''}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export function RestockLinksPanel() {
  const t = useT();
  const {isAdmin} = useAuth();
  const [links, setLinks] = useState<RestockLink[]>([]);
  const [upstreams, setUpstreams] = useState<UpstreamEndpoint[]>([]);
  const [drafts, setDrafts] = useState<Record<number, Draft>>({});
  const [form, setForm] = useState<Draft>(EMPTY);
  const [created, setCreated] = useState<CreatedRestockLink | null>(null);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [savingId, setSavingId] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [linkRows, upstreamRows] = await Promise.all([
        restockLinksApi.list(),
        upstreamsApi.list(),
      ]);
      setLinks(linkRows);
      setUpstreams(upstreamRows.items);
      setDrafts(Object.fromEntries(linkRows.map((link) => [link.id, draftOf(link)])));
    } catch (error) {
      notify.err(errText(error));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isAdmin) void load();
  }, [isAdmin, load]);

  const publicUrl = useMemo(() => {
    if (!created || typeof window === 'undefined') return '';
    return `${window.location.origin}${BASE_PATH}/restock/?token=${encodeURIComponent(created.token)}`;
  }, [created]);

  if (!isAdmin) {
    return (
      <div className="rounded-[20px] bg-muted p-8 text-center text-sm">
        <div className="font-medium">{t('restock.adminRequired')}</div>
        <div className="mx-auto mt-2 max-w-[38rem] text-xs text-muted-foreground">
          {t('restock.adminRequiredDesc')}
        </div>
      </div>
    );
  }

  const create = async () => {
    const body = writeOf(form);
    if (!body.name) {
      notify.warn(t('restock.adminNameRequired'));
      return;
    }
    setCreating(true);
    try {
      const result = await restockLinksApi.create(body);
      setCreated(result);
      setForm(EMPTY);
      notify.ok(t('restock.adminCreated'));
      await load();
    } catch (error) {
      notify.err(errText(error));
    } finally {
      setCreating(false);
    }
  };

  const updateDraft = (id: number, patch: Partial<Draft>) => {
    setDrafts((current) => ({...current, [id]: {...current[id], ...patch}}));
  };

  const save = async (id: number) => {
    const draft = drafts[id];
    if (!draft) return;
    const body = writeOf(draft);
    if (!body.name) {
      notify.warn(t('restock.adminNameRequired'));
      return;
    }
    setSavingId(id);
    try {
      const updated = await restockLinksApi.update(id, body);
      setLinks((current) => current.map((item) => item.id === id ? updated : item));
      setDrafts((current) => ({...current, [id]: draftOf(updated)}));
      notify.ok(t('settings.saved'));
    } catch (error) {
      notify.err(errText(error));
    } finally {
      setSavingId(null);
    }
  };

  const toggle = async (link: RestockLink) => {
    setSavingId(link.id);
    try {
      const updated = await restockLinksApi.update(link.id, {enabled: !link.enabled});
      setLinks((current) => current.map((item) => item.id === link.id ? updated : item));
      setDrafts((current) => ({...current, [link.id]: draftOf(updated)}));
    } catch (error) {
      notify.err(errText(error));
    } finally {
      setSavingId(null);
    }
  };

  return (
    <div className="space-y-4">
      <div className="rounded-[20px] bg-muted p-4">
        <div className="mb-1 text-sm font-medium">{t('restock.adminNew')}</div>
        <div className="mb-4 text-[11px] leading-5 text-muted-foreground">
          {t('restock.adminNewDesc')}
        </div>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <div className="space-y-1.5 xl:col-span-1">
            <Label className="text-[11px] text-muted-foreground">{t('restock.adminName')}</Label>
            <Input
              value={form.name}
              maxLength={64}
              onChange={(event) => setForm({...form, name: event.target.value})}
              className="bg-background"
              placeholder={t('restock.adminNamePlaceholder')}
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('restock.targetPool')}</Label>
            <TargetSelect
              value={form.upstreamId}
              onChange={(upstreamId) => setForm({...form, upstreamId})}
              upstreams={upstreams}
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('restock.adminExpires')}</Label>
            <Input
              type="datetime-local"
              value={form.expires}
              onChange={(event) => setForm({...form, expires: event.target.value})}
              className="bg-background"
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('restock.adminMaxBatches')}</Label>
            <Input
              type="number"
              min={0}
              max={1000000}
              value={form.maxBatches}
              onChange={(event) => setForm({...form, maxBatches: event.target.value})}
              className="bg-background"
            />
          </div>
          <div className="flex items-end gap-3">
            <label className="flex min-h-9 flex-1 items-center gap-2 rounded-xl bg-background px-3 text-xs">
              <Switch
                checked={form.allowOverwrite}
                onCheckedChange={(allowOverwrite) => setForm({...form, allowOverwrite})}
              />
              {t('restock.adminAllowOverwrite')}
            </label>
            <Button className="rounded-full" disabled={creating || !upstreams.length} onClick={() => void create()}>
              {creating ? <Loader2 className="mr-1.5 h-4 w-4 animate-spin" /> : <PackagePlus className="mr-1.5 h-4 w-4" />}
              {t('restock.adminCreate')}
            </Button>
          </div>
        </div>
        <div className="mt-2 text-[10px] leading-4 text-muted-foreground">
          {t('restock.adminZeroUnlimited')}
        </div>
      </div>

      {created && (
        <div className="rounded-[20px] border border-amber-500/30 bg-amber-500/10 p-4">
          <div className="flex items-start gap-2.5">
            <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
            <div className="min-w-0 flex-1">
              <div className="text-sm font-medium">{t('restock.adminTokenOnce')}</div>
              <div className="mt-1 text-[11px] leading-5 text-muted-foreground">
                {t('restock.adminTokenOnceDesc')}
              </div>
              <div className="mt-3 flex items-center gap-2 rounded-xl bg-background p-2">
                <code className="min-w-0 flex-1 break-all font-mono text-[11px]">{publicUrl}</code>
                <CopyButton value={publicUrl} showLabel size="sm" variant="outline" label={t('restock.adminCopyUrl')} />
              </div>
              <a
                href={publicUrl}
                target="_blank"
                rel="noreferrer"
                className="mt-2 inline-flex items-center gap-1 text-[11px] text-primary hover:underline"
              >
                <ExternalLink className="h-3 w-3" />{t('restock.adminOpenPage')}
              </a>
            </div>
          </div>
        </div>
      )}

      {loading && !links.length ? (
        <div className="flex items-center justify-center gap-2 rounded-[20px] bg-muted py-14 text-xs text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />{t('common.loading')}
        </div>
      ) : links.length ? (
        <div className="space-y-3">
          {links.map((link) => {
            const draft = drafts[link.id] ?? draftOf(link);
            return (
              <div key={link.id} className="rounded-[20px] bg-muted p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{link.name}</span>
                      <Badge variant={link.enabled ? 'default' : 'secondary'} className="rounded-full">
                        {link.enabled ? t('restock.adminEnabled') : t('restock.adminDisabled')}
                      </Badge>
                      {!link.target_available && (
                        <Badge variant="outline" className="rounded-full border-amber-500/40 text-amber-600 dark:text-amber-400">
                          {t('restock.adminTargetUnavailable')}
                        </Badge>
                      )}
                    </div>
                    <div className="mt-1 text-[11px] text-muted-foreground">
                      <code>{link.prefix}…</code>
                      {' · '}{t('restock.adminUsedBatches', {
                        used: link.used_batches,
                        max: link.max_batches || '∞',
                      })}
                      {' · '}{t('restock.adminCreatedAt', {time: fmtDateTime(link.created_at)})}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      className="rounded-full"
                      disabled={savingId === link.id}
                      onClick={() => void toggle(link)}
                    >
                      {link.enabled ? t('restock.adminDisable') : t('restock.adminEnable')}
                    </Button>
                    <ConfirmDialog
                      title={t('restock.adminDeleteTitle', {name: link.name})}
                      description={t('restock.adminDeleteDesc')}
                      confirmText={t('keys.delete')}
                      destructive
                      onConfirm={async () => {
                        try {
                          await restockLinksApi.remove(link.id);
                          setLinks((current) => current.filter((item) => item.id !== link.id));
                          notify.ok(t('keys.deleted'));
                        } catch (error) {
                          notify.err(errText(error));
                          throw error;
                        }
                      }}
                      trigger={
                        <Button variant="ghost" size="icon" className="h-8 w-8 rounded-full text-red-500">
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      }
                    />
                  </div>
                </div>

                <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
                  <div className="space-y-1.5">
                    <Label className="text-[11px] text-muted-foreground">{t('restock.adminName')}</Label>
                    <Input
                      value={draft.name}
                      onChange={(event) => updateDraft(link.id, {name: event.target.value})}
                      className="bg-background"
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-[11px] text-muted-foreground">{t('restock.targetPool')}</Label>
                    <TargetSelect
                      value={draft.upstreamId}
                      onChange={(upstreamId) => updateDraft(link.id, {upstreamId})}
                      upstreams={upstreams}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-[11px] text-muted-foreground">{t('restock.adminExpires')}</Label>
                    <Input
                      type="datetime-local"
                      value={draft.expires}
                      onChange={(event) => updateDraft(link.id, {expires: event.target.value})}
                      className="bg-background"
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-[11px] text-muted-foreground">{t('restock.adminMaxBatches')}</Label>
                    <Input
                      type="number"
                      min={0}
                      max={1000000}
                      value={draft.maxBatches}
                      onChange={(event) => updateDraft(link.id, {maxBatches: event.target.value})}
                      className="bg-background"
                    />
                  </div>
                  <div className="flex items-end gap-2">
                    <label className="flex min-h-9 flex-1 items-center gap-2 rounded-xl bg-background px-3 text-xs">
                      <Switch
                        checked={draft.allowOverwrite}
                        onCheckedChange={(allowOverwrite) => updateDraft(link.id, {allowOverwrite})}
                      />
                      {t('restock.adminAllowOverwrite')}
                    </label>
                    <Button
                      size="icon"
                      className="h-9 w-9 shrink-0 rounded-full"
                      title={t('common.save')}
                      disabled={savingId === link.id}
                      onClick={() => void save(link.id)}
                    >
                      {savingId === link.id
                        ? <Loader2 className="h-4 w-4 animate-spin" />
                        : <Save className="h-4 w-4" />}
                    </Button>
                  </div>
                </div>

                <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-muted-foreground">
                  <span>{t('restock.adminTarget')}: {link.upstream_name}</span>
                  <span>{t('restock.adminExpires')}: {link.expires_at ? fmtDateTime(link.expires_at) : t('restock.adminNever')}</span>
                  <span>{t('restock.adminLastUsed')}: {link.last_used_at ? fmtDateTime(link.last_used_at) : t('restock.adminNeverUsed')}</span>
                  {link.last_used_ip && <span>IP: {link.last_used_ip}</span>}
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <EmptyState
          icon={Link2}
          title={t('restock.adminEmpty')}
          description={t('restock.adminEmptyDesc')}
          className="flex flex-col items-center justify-center rounded-[20px] bg-muted py-14 text-center"
        />
      )}
    </div>
  );
}
