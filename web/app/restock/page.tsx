'use client';

/**
 * 公开补货页。
 *
 * 页面不放进 `(main)`：访客不需要管理端会话，也不应该看到后台导航。明文 Token
 * 只从首次分享 URL 读取一次，随后写入 sessionStorage 并立即从地址栏清掉；后续
 * 请求统一走 X-Restock-Token，避免凭据继续出现在浏览器历史和代理 access log。
 */
import {useCallback, useEffect, useRef, useState} from 'react';
import {
  CheckCircle2,
  CircleAlert,
  FileJson2,
  Loader2,
  PackagePlus,
  ShieldCheck,
  Trash2,
  UploadCloud,
  XCircle,
} from 'lucide-react';
import {restockApi, errText} from '@/lib/api';
import type {ImportResult, RestockInfo} from '@/lib/types';
import {useI18n} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';

const TOKEN_STORE = 'wb-restock-token';
const FALLBACK_MAX_FILES = 200;
const FALLBACK_MAX_FILE_BYTES = 512 * 1024;
const FALLBACK_MAX_REQUEST_BYTES = 16 * 1024 * 1024;

function errorStatus(error: unknown): number | undefined {
  return (error as {response?: {status?: number}})?.response?.status;
}

function bytesText(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${Math.round(bytes / 1024 / 1024)} MB`;
  return `${Math.round(bytes / 1024)} KB`;
}

export default function RestockPage() {
  const {t, tp} = useI18n();
  const inputRef = useRef<HTMLInputElement>(null);
  const [token, setToken] = useState('');
  const [info, setInfo] = useState<RestockInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [linkError, setLinkError] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  const [results, setResults] = useState<ImportResult[] | null>(null);
  const [importing, setImporting] = useState(false);
  const [dragging, setDragging] = useState(false);

  useEffect(() => {
    const url = new URL(window.location.href);
    const fromUrl = (url.searchParams.get('token') || '').trim();
    let next = fromUrl;
    try {
      if (fromUrl) window.sessionStorage.setItem(TOKEN_STORE, fromUrl);
      else next = window.sessionStorage.getItem(TOKEN_STORE) || '';
    } catch {
      // 禁用存储时本次页面仍可用，只是不支持刷新后继续上传。
    }

    if (url.searchParams.has('token')) {
      url.searchParams.delete('token');
      const clean = `${url.pathname}${url.search}${url.hash}`;
      window.history.replaceState(window.history.state, '', clean);
    }

    setToken(next);
    if (!next) {
      setLinkError(t('restock.missingToken'));
      setLoading(false);
      return;
    }

    restockApi.info(next)
      .then((value) => {
        setInfo(value);
        setLinkError('');
      })
      .catch((error) => {
        const status = errorStatus(error);
        if (status === 401) {
          setLinkError(t('restock.invalidLink'));
          try { window.sessionStorage.removeItem(TOKEN_STORE); } catch { /* ignore */ }
        } else if (status === 410) {
          setLinkError(t('restock.expiredLink'));
        } else if (status === 403) {
          setLinkError(t('restock.disabledOrExhausted'));
        } else if (status === 409) {
          setLinkError(t('restock.targetUnavailable'));
        } else {
          setLinkError(errText(error));
        }
      })
      .finally(() => setLoading(false));
  }, [t]);

  const limits = info?.limits ?? {
    max_files: FALLBACK_MAX_FILES,
    max_file_bytes: FALLBACK_MAX_FILE_BYTES,
    max_request_bytes: FALLBACK_MAX_REQUEST_BYTES,
  };

  const importErrorText = useCallback((message: string) => {
    // 导入 Service 的错误以中文源文返回；这里按稳定前缀翻译动态错误，避免英文、
    // 日文等公开页面在逐文件结果里突然混入中文或把解析器细节直接暴露给访客。
    if (message.startsWith('本批文件总大小超过')) {
      return t('restock.batchTooLarge', {max: bytesText(limits.max_request_bytes)});
    }
    if (message.startsWith('文件过大')) {
      return t('restock.fileTooLarge', {max: bytesText(limits.max_file_bytes)});
    }
    if (message.startsWith('不是有效的 JSON') || message.startsWith('文件里是空数组')) {
      return t('restock.invalidJsonFile');
    }
    if (message.startsWith('缺少 uid')) return t('restock.missingUid');
    if (message.startsWith('缺少 accessToken')) return t('restock.missingAccessToken');
    if (message.startsWith('账号已存在')) return t('restock.duplicateNotAllowed');
    if (message.startsWith('账号 uid 形态异常')) return t('restock.invalidUid');
    if (message.startsWith('读取失败')) return t('restock.readFailed');
    if (message.startsWith('写入失败')) return t('restock.writeFailed');
    return tp(message);
  }, [limits.max_file_bytes, limits.max_request_bytes, t, tp]);

  const addFiles = useCallback((picked: FileList | File[]) => {
    const all = Array.from(picked);
    const jsonFiles = all.filter((file) => file.name.toLowerCase().endsWith('.json'));
    if (jsonFiles.length !== all.length) notify.warn(t('restock.jsonOnly'));
    if (!jsonFiles.length) return;

    const oversized = jsonFiles.filter((file) => file.size > limits.max_file_bytes);
    if (oversized.length) {
      notify.err(t('restock.fileTooLarge', {max: bytesText(limits.max_file_bytes)}));
      return;
    }

    setFiles((current) => {
      const merged = [...current, ...jsonFiles];
      if (merged.length > limits.max_files) {
        notify.err(t('restock.tooManyFiles', {max: limits.max_files}));
        return current;
      }
      const total = merged.reduce((sum, file) => sum + file.size, 0);
      if (total > limits.max_request_bytes) {
        notify.err(t('restock.batchTooLarge', {max: bytesText(limits.max_request_bytes)}));
        return current;
      }
      return merged;
    });
    setResults(null);
  }, [limits.max_file_bytes, limits.max_files, limits.max_request_bytes, t]);

  const importFiles = useCallback(async () => {
    if (!token || !files.length) return;
    setImporting(true);
    setResults(null);
    try {
      const response = await restockApi.importAccounts(token, files);
      setResults(response.results);
      // 后端按“被接受的上传请求”占用一次批次，即使其中所有 JSON 都失败也会计数。
      // 前端必须同步扣减，否则会暂时显示一个实际上已经不存在的剩余批次。
      setInfo((current) => current && current.remaining_batches !== null
        ? {...current, remaining_batches: Math.max(0, current.remaining_batches - 1)}
        : current);
      if (response.succeeded) {
        notify.ok(
          t('restock.importSucceeded', {ok: response.succeeded, n: response.succeeded}),
          response.failed ? t('restock.importPartial', {failed: response.failed}) : undefined,
        );
      } else {
        notify.err(t('restock.importAllFailed'));
      }
    } catch (error) {
      const status = errorStatus(error);
      if (status === 410) setLinkError(t('restock.expiredLink'));
      else if (status === 401) setLinkError(t('restock.invalidLink'));
      else if (status === 403) setLinkError(t('restock.disabledOrExhausted'));
      else if (status === 409) setLinkError(t('restock.targetUnavailable'));
      else notify.err(errText(error));
    } finally {
      setImporting(false);
    }
  }, [files, t, token]);

  const succeeded = results?.filter((item) => item.ok).length ?? 0;
  const totalBytes = files.reduce((sum, file) => sum + file.size, 0);

  return (
    <main className="min-h-screen bg-background px-4 py-8 text-foreground sm:px-6 sm:py-12">
      <div className="mx-auto w-full max-w-3xl">
        <div className="mb-6 flex items-center gap-3">
          <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-primary/10">
            <PackagePlus className="h-5 w-5 text-primary" />
          </div>
          <div>
            <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">
              {info?.name || t('restock.title')}
            </h1>
            <p className="mt-0.5 text-xs text-muted-foreground">{t('restock.subtitle')}</p>
          </div>
        </div>

        {loading ? (
          <div className="flex min-h-72 items-center justify-center gap-2 rounded-[24px] bg-muted text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            {t('restock.validating')}
          </div>
        ) : linkError || !info ? (
          <div className="rounded-[24px] border border-red-500/25 bg-red-500/5 px-5 py-12 text-center">
            <XCircle className="mx-auto h-9 w-9 text-red-500" />
            <div className="mt-4 text-base font-medium">{t('restock.unavailable')}</div>
            <div className="mx-auto mt-2 max-w-lg text-sm leading-6 text-muted-foreground">
              {linkError || t('restock.invalidLink')}
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            <section className="grid gap-3 rounded-[24px] bg-muted p-4 sm:grid-cols-3">
              <div className="rounded-2xl bg-background p-3.5">
                <div className="text-[11px] text-muted-foreground">{t('restock.targetPool')}</div>
                <div className="mt-1 truncate text-sm font-medium">{info.upstream.name}</div>
              </div>
              <div className="rounded-2xl bg-background p-3.5">
                <div className="text-[11px] text-muted-foreground">{t('restock.limits')}</div>
                <div className="mt-1 text-sm font-medium">
                  {t('restock.limitSummary', {
                    count: limits.max_files,
                    size: bytesText(limits.max_file_bytes),
                  })}
                </div>
              </div>
              <div className="rounded-2xl bg-background p-3.5">
                <div className="text-[11px] text-muted-foreground">{t('restock.overwritePolicy')}</div>
                <div className="mt-1 text-sm font-medium">
                  {info.allow_overwrite ? t('restock.overwriteAllowed') : t('restock.noOverwrite')}
                </div>
              </div>
              <div className="flex items-center gap-2 sm:col-span-3">
                <ShieldCheck className="h-4 w-4 shrink-0 text-emerald-500" />
                <p className="text-[11px] leading-5 text-muted-foreground">
                  {t('restock.privacyHint')}
                  {info.remaining_batches !== null
                    ? ` · ${t('restock.remainingBatches', {count: info.remaining_batches})}`
                    : ''}
                </p>
              </div>
            </section>

            <section className="rounded-[24px] bg-muted p-4">
              <div
                role="button"
                tabIndex={0}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') inputRef.current?.click();
                }}
                onClick={() => inputRef.current?.click()}
                onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
                onDragLeave={() => setDragging(false)}
                onDrop={(event) => {
                  event.preventDefault();
                  setDragging(false);
                  if (event.dataTransfer.files.length) addFiles(event.dataTransfer.files);
                }}
                className={
                  'flex cursor-pointer flex-col items-center justify-center rounded-[20px] border-2 border-dashed px-5 py-10 text-center outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring ' +
                  (dragging
                    ? 'border-primary bg-primary/5'
                    : 'border-border bg-background hover:border-muted-foreground/40')
                }
              >
                <UploadCloud className="h-8 w-8 text-muted-foreground" />
                <div className="mt-3 text-sm font-medium">{t('restock.dropTitle')}</div>
                <div className="mt-1 max-w-md text-[11px] leading-5 text-muted-foreground">
                  {t('restock.dropDesc', {
                    size: bytesText(limits.max_file_bytes),
                    total: bytesText(limits.max_request_bytes),
                  })}
                </div>
                <input
                  ref={inputRef}
                  className="hidden"
                  type="file"
                  accept=".json,application/json"
                  multiple
                  onChange={(event) => {
                    if (event.target.files?.length) addFiles(event.target.files);
                    event.target.value = '';
                  }}
                />
              </div>

              {files.length > 0 && (
                <div className="mt-4 space-y-2">
                  <div className="flex flex-wrap items-center justify-between gap-2 px-1">
                    <div className="text-xs font-medium">
                      {t('restock.selectedFiles', {count: files.length})}
                    </div>
                    <div className="text-[11px] text-muted-foreground">
                      {bytesText(totalBytes)} / {bytesText(limits.max_request_bytes)}
                    </div>
                  </div>
                  <div className="max-h-80 space-y-1.5 overflow-y-auto pr-1">
                    {files.map((file, index) => {
                      // 后端逐文件保持输入顺序；按索引关联可正确处理来自不同目录的
                      // 同名 account.json，而不是把多个结果错误地指向第一项。
                      const result = results?.[index];
                      return (
                        <div key={`${file.name}-${file.size}-${file.lastModified}-${index}`} className="flex items-start gap-2 rounded-2xl bg-background px-3 py-2.5">
                          {result?.ok ? (
                            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" />
                          ) : result ? (
                            <CircleAlert className="mt-0.5 h-4 w-4 shrink-0 text-red-500" />
                          ) : (
                            <FileJson2 className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                          )}
                          <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-center gap-1.5">
                              <span className="truncate text-xs font-medium">{file.name}</span>
                              <span className="text-[10px] text-muted-foreground">{bytesText(file.size)}</span>
                              {result?.ok && (
                                <Badge variant="secondary" className="h-5 rounded-full px-1.5 text-[10px]">
                                  {t('restock.success')}
                                </Badge>
                              )}
                            </div>
                            {result && (
                              <div className={
                                'mt-1 break-words text-[11px] leading-4 ' +
                                (result.ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-500')
                              }>
                                {result.ok
                                  ? `${result.nickname || result.uid || file.name} · ${t('restock.poolLoading')}`
                                  : importErrorText(result.error || t('restock.failed'))}
                              </div>
                            )}
                          </div>
                          {!importing && (
                            <Button
                              type="button"
                              variant="ghost"
                              size="icon"
                              className="h-7 w-7 shrink-0 rounded-full text-muted-foreground"
                              aria-label={t('restock.removeFile')}
                              onClick={(event) => {
                                event.stopPropagation();
                                setFiles((current) => current.filter((_, itemIndex) => itemIndex !== index));
                                setResults(null);
                              }}
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </Button>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              {results && succeeded > 0 && (
                <div className="mt-4 flex items-start gap-2 rounded-2xl border border-emerald-500/25 bg-emerald-500/10 p-3 text-xs text-emerald-700 dark:text-emerald-300">
                  <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
                  <div>
                    <div className="font-medium">{t('restock.written', {count: succeeded})}</div>
                    <div className="mt-0.5 opacity-80">{t('restock.poolLoading')}</div>
                  </div>
                </div>
              )}

              <Button
                className="mt-4 w-full rounded-full"
                size="lg"
                disabled={!files.length || importing || info.remaining_batches === 0}
                onClick={() => void importFiles()}
              >
                {importing ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <UploadCloud className="mr-2 h-4 w-4" />}
                {importing ? t('restock.importing') : t('restock.importButton', {count: files.length})}
              </Button>
            </section>
          </div>
        )}
      </div>
    </main>
  );
}
