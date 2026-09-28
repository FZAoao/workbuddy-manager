'use client';

/**
 * 账号补货页（公开，凭 URL 里的 token 鉴权）。
 *
 * 为什么不放在 `(main)/` 下：那一组 layout 会检查登录态、渲染管理端导航栏。
 * 而这里是**给拿到链接的人看的**（帮管理员补账号的同事 / 渠道，多半没有
 * 账号）——同 claim 抽奖页一样的公开页形态。
 *
 * token 从 URL 的 `?token=` 取。用 `window.location` 而不是 `useSearchParams`：
 * 静态导出下后者要求外面包一层 Suspense，而这个页面没有任何服务端内容，
 * 直接读 location 更省事（claim 页同款取舍）。
 *
 * 页面只做一件事：把账号授权 JSON 拖进来导入（可选国内 / 国际版提示语）。
 * 显示的是「这个页面已导入多少个账号」，不展示任何账号内容——导入接口与
 * 面板「在线导入」同一套解析落盘，格式自动识别，无需用户选版本。
 */
import {useCallback, useEffect, useRef, useState} from 'react';
import {
  PackagePlus, UploadCloud, FileJson, X, CheckCircle2, Loader2, ShieldCheck,
} from 'lucide-react';
import {notify} from '@/lib/toast';
import {restockPublicApi, errText} from '@/lib/api';
import type {ImportResult} from '@/lib/types';
import {Button} from '@/components/ui/button';
import {useT} from '@/lib/i18n/provider';
import {fmtDateTime} from '@/lib/format';

/** 单文件上限（与后端 _IMPORT_MAX_BYTES 一致），前端先拦一次。 */
const IMPORT_MAX_BYTES = 512 * 1024;
const IMPORT_ACCEPT = '.json,application/json';

export default function RestockPage() {
  const t = useT();
  const [token, setToken] = useState('');
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');
  const [info, setInfo] = useState<Awaited<ReturnType<typeof restockPublicApi.info>> | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [importing, setImporting] = useState(false);
  const [results, setResults] = useState<ImportResult[] | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    const tk = new URLSearchParams(window.location.search).get('token') || '';
    setToken(tk);
    if (!tk) {
      setErr(t('restock.noToken'));
      setLoading(false);
      return;
    }
    restockPublicApi.info(tk)
      .then(setInfo)
      .catch((e) => setErr(errText(e)))
      .finally(() => setLoading(false));
  }, [t]);

  const addFiles = useCallback((picked: FileList | File[]) => {
    const incoming = Array.from(picked).filter((f) => f.name.toLowerCase().endsWith('.json'));
    if (!incoming.length) {
      notify.warn(t('restock.noJson'));
      return;
    }
    // 同名去重（拖两次同一批很常见）
    setFiles((prev) => {
      const names = new Set(prev.map((f) => f.name));
      const merged = [...prev];
      for (const f of incoming) {
        if (names.has(f.name)) continue;
        names.add(f.name);
        merged.push(f);
      }
      return merged;
    });
    setResults(null);
  }, [t]);

  const doImport = useCallback(async () => {
    const tooBig = files.filter((f) => f.size > IMPORT_MAX_BYTES);
    if (tooBig.length) {
      notify.err(t('restock.tooBig', {max: Math.floor(IMPORT_MAX_BYTES / 1024)}));
      return;
    }
    setImporting(true);
    try {
      const res = await restockPublicApi.import(token, files);
      setResults(res.results);
      if (res.succeeded > 0) {
        notify.ok(t('restock.importDone', {ok: res.succeeded}));
        setInfo((prev) => (prev ? {...prev, imported_count: res.imported_count} : prev));
        setFiles((prev) => prev.filter((f) => !res.results.some(
          (r) => r.ok && r.file === f.name,
        )));
      } else {
        notify.err(t('restock.importAllFailed'));
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setImporting(false);
    }
  }, [files, token, t]);

  const importFailed = (results ?? []).filter((r) => !r.ok).length;
  const importOk = (results ?? []).filter((r) => r.ok).length;

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4 py-10">
      <div className="w-full max-w-md animate-in fade-in-0 zoom-in-95 rounded-[24px] bg-muted px-6 py-8 duration-300">
        {loading ? (
          <div className="flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />{t('common.loading')}
          </div>
        ) : err || !info ? (
          <div className="py-6 text-center">
            <PackagePlus className="mx-auto mb-4 h-12 w-12 text-muted-foreground" />
            <div className="text-sm font-medium">{t('restock.failed')}</div>
            <p className="mt-2 text-xs leading-5 text-muted-foreground">{err}</p>
          </div>
        ) : (
          <>
            <div className="text-center">
              <PackagePlus className="mx-auto mb-3 h-10 w-10 text-emerald-500" />
              <div className="text-base font-medium">
                {info.name || t('restock.untitled')}
              </div>
              <p className="mt-1 text-xs text-muted-foreground">{t('restock.subtitle')}</p>
              <div className="mt-3 flex items-center justify-center gap-4">
                <div className="rounded-2xl bg-background px-5 py-2.5 text-center">
                  <div className="text-2xl font-semibold tabular-nums text-emerald-600 dark:text-emerald-400">
                    {info.imported_count}
                  </div>
                  <div className="mt-0.5 text-[10px] text-muted-foreground">
                    {t('restock.importedCount')}
                  </div>
                </div>
                <div className="rounded-2xl bg-background px-5 py-2.5 text-center">
                  <div className="text-sm font-medium">{info.upstream.name}</div>
                  <div className="mt-1 text-[10px] text-muted-foreground">
                    {t('restock.groupLabel')}
                  </div>
                </div>
              </div>
              {info.expires_at && (
                <p className="mt-2 text-[10px] text-muted-foreground/70">
                  {t('restock.expiresAt', {at: fmtDateTime(info.expires_at)})}
                </p>
              )}
            </div>

            {/* 拖拽区 / 点选文件 */}
            <div
              onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragOver(false);
                if (e.dataTransfer?.files?.length) addFiles(e.dataTransfer.files);
              }}
              onClick={() => fileInputRef.current?.click()}
              className={
                'mt-5 flex w-full cursor-pointer flex-col items-center justify-center gap-2 rounded-2xl border-2 border-dashed px-4 py-8 text-center transition-colors ' +
                (dragOver
                  ? 'border-primary bg-primary/5'
                  : 'border-border bg-background/60 hover:border-muted-foreground/40')
              }
            >
              <UploadCloud className="h-7 w-7 text-muted-foreground" />
              <div className="text-xs font-medium">{t('restock.dropHint')}</div>
              <div className="text-[10px] leading-4 text-muted-foreground">
                {t('restock.dropSub')}
              </div>
              <input
                ref={fileInputRef}
                type="file"
                accept={IMPORT_ACCEPT}
                multiple
                className="hidden"
                onChange={(e) => {
                  if (e.target.files?.length) addFiles(e.target.files);
                  e.target.value = '';   // 允许重复选同一个文件
                }}
              />
            </div>

            {/* 已选文件列表 */}
            {files.length > 0 && (
              <div className="mt-3 space-y-1">
                {files.map((f) => {
                  const r = results?.find((x) => x.file === f.name);
                  return (
                    <div key={f.name}
                         className="flex items-center gap-2 rounded-xl bg-background px-3 py-1.5">
                      <FileJson className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                      <span className="min-w-0 flex-1 truncate text-[11px]" title={f.name}>
                        {f.name}
                      </span>
                      {r && (
                        r.ok ? (
                          <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-500" />
                        ) : (
                          <span className="shrink-0 text-[10px] text-red-500" title={r.error}>
                            {t('restock.itemFailed')}
                          </span>
                        )
                      )}
                      {!importing && (
                        <button
                          type="button"
                          onClick={() => {
                            setFiles((prev) => prev.filter((x) => x.name !== f.name));
                            setResults(null);
                          }}
                          className="shrink-0 text-muted-foreground hover:text-foreground"
                          aria-label={t('common.clear')}
                        >
                          <X className="h-3.5 w-3.5" />
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            )}

            {/* 失败原因（逐个列出，便于修文件后重试） */}
            {results && importFailed > 0 && (
              <div className="mt-3 space-y-1 rounded-xl bg-red-500/5 px-3 py-2">
                {results.filter((r) => !r.ok).map((r) => (
                  <div key={r.file} className="text-[10px] leading-4 text-red-600 dark:text-red-400">
                    <span className="font-medium">{r.file}</span>：{r.error}
                  </div>
                ))}
              </div>
            )}

            {results && importOk > 0 && (
              <div className="mt-3 flex items-center justify-center gap-1.5 text-xs text-emerald-500">
                <CheckCircle2 className="h-3.5 w-3.5" />
                {t('restock.successNote', {ok: importOk})}
              </div>
            )}

            <Button
              className="mt-5 w-full rounded-full transition-transform active:scale-[0.98]"
              disabled={importing || files.length === 0}
              onClick={doImport}
            >
              {importing
                ? <><Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />{t('restock.importing')}</>
                : t('restock.importButton', {n: files.length})}
            </Button>

            <p className="mt-4 flex items-center justify-center gap-1 text-center text-[10px] text-muted-foreground/70">
              <ShieldCheck className="h-3 w-3 shrink-0" />
              {t('restock.footer')}
            </p>
          </>
        )}
      </div>
    </div>
  );
}
