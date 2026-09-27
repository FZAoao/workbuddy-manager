'use client';

import {useCallback, useState} from 'react';
import {
  Ticket,
  Coins,
  Gift,
  RefreshCw,
  Trash2,
  CheckCircle2,
  Undo2,
  Ban,
  Sparkles,
} from 'lucide-react';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {useAsyncAll} from '@/lib/use-async-data';
import {notify} from '@/lib/toast';
import {accountApi, errText} from '@/lib/api';
import type {
  LotteryPrize,
  LotteryPrizeResponse,
  LotteryPrizeStatus,
} from '@/lib/types';
import {fmtDateTimeMarked, fmtNumber} from '@/lib/format';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table';

/** 时间范围选项（与任务记录页保持同一套说法） */
const RANGES = [
  {value: '7', key: 'stats.last7'},
  {value: '30', key: 'stats.last30'},
  {value: '90', key: 'stats.last90'},
  {value: '0', key: 'prizes.allTime'},
];

/** 核销状态 → 配色与图标 */
const STATUS_STYLE: Record<LotteryPrizeStatus, {tone: string; Icon: typeof CheckCircle2}> = {
  pending: {tone: 'text-amber-600 dark:text-amber-400', Icon: Sparkles},
  redeemed: {tone: 'text-emerald-600 dark:text-emerald-400', Icon: CheckCircle2},
  void: {tone: 'text-muted-foreground', Icon: Ban},
};

const EMPTY: LotteryPrize[] = [];

export default function PrizesPage() {
  const t = useT();
  const {isAdmin} = useAuth();
  const [days, setDays] = useState('30');
  const [statusFilter, setStatusFilter] = useState('all');
  const [typeFilter, setTypeFilter] = useState('all');

  const {values, errors, isInitialLoading, isInitialFailed, isRefreshing, reload} =
    useAsyncAll({
      prizes: () => accountApi.lotteryPrizes({
        limit: 500,
        status: statusFilter === 'all' ? undefined : statusFilter,
        prize_type: typeFilter === 'all' ? undefined : typeFilter,
        days: Number(days) || undefined,
      }),
    }, [], [days, statusFilter, typeFilter]);

  useHeartbeat(reload, 30000);

  const data: LotteryPrizeResponse | undefined = values.prizes;
  const items: LotteryPrize[] = data?.items ?? EMPTY;
  const failed = 'prizes' in errors;

  const updateStatus = useCallback(async (p: LotteryPrize, status: LotteryPrizeStatus) => {
    try {
      await accountApi.updateLotteryPrize(p.id, {status});
      await reload();
    } catch (e) {
      notify.err(errText(e));
    }
  }, [reload]);

  /** 删除后的收尾（通知 + 刷新）。调用点内联在 ConfirmDialog 里，
   *  以便「破坏性操作必须二次确认」的源码守卫能识别到。 */
  const afterRemove = useCallback(async () => {
    notify.ok(t('prizes.deleted'));
    await reload();
  }, [reload, t]);

  const header = (
    <PageHeader
      title={t('prizes.title')}
      description={t('prizes.description')}
      actions={
        <Button variant="outline" size="sm" className="rounded-full"
                onClick={() => reload()}>
          <RefreshCw className={isRefreshing ? 'animate-spin' : ''} />
          {t('common.refresh')}
        </Button>
      }
    />
  );

  if (isInitialFailed || isInitialLoading) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        {header}
        {isInitialFailed ? <LoadError variant="page" onRetry={reload} /> : <PrizesSkeleton />}
      </div>
    );
  }

  const stats = data?.stats;
  const hasAny = (stats?.total ?? 0) > 0 || items.length > 0;

  return (
    <div className="flex flex-col gap-4 md:gap-6" aria-busy={isRefreshing}>
      {header}

      {/* 概览：中奖总数 / 积分合计 / 券核销进度 */}
      {stats && stats.total > 0 && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard icon={Gift} label={t('prizes.statTotal')}
                    value={fmtNumber(stats.total)} />
          <StatCard icon={Coins} label={t('prizes.statCredit')}
                    value={fmtNumber(stats.total_credit)} />
          <StatCard icon={Ticket} label={t('prizes.statVoucher')}
                    value={fmtNumber(stats.voucher_total)} />
          <StatCard icon={CheckCircle2} label={t('prizes.statRedeemed')}
                    value={`${fmtNumber(stats.voucher_redeemed)}/${fmtNumber(stats.voucher_total)}`} />
        </div>
      )}

      <section className="flex flex-col overflow-hidden rounded-[20px] bg-muted">
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 px-4 py-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Ticket className="h-4 w-4" />
            {t('prizes.listTitle')}
            <span className="hidden text-[11px] font-normal text-muted-foreground sm:inline">
              {t('prizes.listSubtitle')}
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Select value={typeFilter} onValueChange={setTypeFilter}>
              <SelectTrigger className="h-7 w-[110px] rounded-full text-[11px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all" className="text-xs">{t('prizes.typeAll')}</SelectItem>
                <SelectItem value="voucher" className="text-xs">{t('prizes.typeVoucher')}</SelectItem>
                <SelectItem value="credit" className="text-xs">{t('prizes.typeCredit')}</SelectItem>
              </SelectContent>
            </Select>
            <Select value={statusFilter} onValueChange={setStatusFilter}>
              <SelectTrigger className="h-7 w-[110px] rounded-full text-[11px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all" className="text-xs">{t('prizes.statusAll')}</SelectItem>
                <SelectItem value="pending" className="text-xs">{t('prizes.statusPending')}</SelectItem>
                <SelectItem value="redeemed" className="text-xs">{t('prizes.statusRedeemed')}</SelectItem>
                <SelectItem value="void" className="text-xs">{t('prizes.statusVoid')}</SelectItem>
              </SelectContent>
            </Select>
            <Select value={days} onValueChange={setDays}>
              <SelectTrigger className="h-7 w-[110px] rounded-full text-[11px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {RANGES.map((r) => (
                  <SelectItem key={r.value} value={r.value} className="text-xs">
                    {t(r.key)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        {failed && (
          <div className="px-4 pb-3">
            <LoadError message={t('prizes.loadFailed')} onRetry={reload} />
          </div>
        )}

        {items.length ? (
          <>
            {/* 手机端：卡片 */}
            <div className="space-y-1.5 px-3.5 pb-4 md:hidden">
              {items.map((p) => {
                const st = STATUS_STYLE[p.status] ?? STATUS_STYLE.pending;
                return (
                  <div key={p.id} className="rounded-xl bg-background/60 px-3 py-2">
                    <div className="flex items-center justify-between gap-2">
                      <span className="flex min-w-0 items-center gap-1.5 text-xs font-medium">
                        {p.prize_type === 'voucher'
                          ? <Ticket className="h-3.5 w-3.5 shrink-0 text-amber-500" />
                          : <Coins className="h-3.5 w-3.5 shrink-0 text-emerald-500" />}
                        <span className="truncate">{p.label}</span>
                      </span>
                      <span className={'flex shrink-0 items-center gap-1 text-[11px] font-medium ' + st.tone}>
                        <st.Icon className="h-3 w-3" />
                        {t(`prizes.status_${p.status}`)}
                      </span>
                    </div>
                    <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
                      <span className="truncate" title={p.uid}>{p.nickname || p.uid || '—'}</span>
                      <span className="shrink-0 tabular-nums">{fmtDateTimeMarked(p.ts)}</span>
                    </div>
                    {isAdmin && (
                      <div className="mt-1.5 flex gap-1.5">
                        {p.status !== 'redeemed' && (
                          <Button variant="ghost" size="sm" className="h-6 rounded-full px-2 text-[10px]"
                                  onClick={() => updateStatus(p, 'redeemed')}>
                            {t('prizes.markRedeemed')}
                          </Button>
                        )}
                        {p.status === 'redeemed' && (
                          <Button variant="ghost" size="sm" className="h-6 rounded-full px-2 text-[10px]"
                                  onClick={() => updateStatus(p, 'pending')}>
                            {t('prizes.markPending')}
                          </Button>
                        )}
                        <ConfirmDialog
                          title={t('prizes.deleteTitle')}
                          description={t('prizes.deleteDesc', {label: p.label})}
                          confirmText={t('common.clear')}
                          destructive
                          onConfirm={async () => {
                            await accountApi.removeLotteryPrize(p.id);
                            await afterRemove();
                          }}
                          trigger={
                            <Button variant="ghost" size="sm"
                                    className="h-6 rounded-full px-2 text-[10px] text-red-500">
                              <Trash2 className="h-3 w-3" />
                            </Button>
                          }
                        />
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

            <div className="hidden md:block">
              <Table>
                <TableHeader>
                  <TableRow className="border-b border-border/60 hover:bg-transparent">
                    <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('logs.colTime')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('prizes.colPrize')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('prizes.colType')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('tasks.colAccount')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('prizes.colStatus')}</TableHead>
                    <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">{t('prizes.colActions')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {items.map((p) => {
                    const st = STATUS_STYLE[p.status] ?? STATUS_STYLE.pending;
                    return (
                      <TableRow key={p.id} className="border-b border-border/40">
                        <TableCell className="pl-4 text-xs tabular-nums text-muted-foreground">
                          {fmtDateTimeMarked(p.ts)}
                        </TableCell>
                        <TableCell className="max-w-[260px] truncate text-xs" title={p.label}>
                          {p.label}
                          {p.credit > 0 && (
                            <span className="ml-1 text-emerald-600 dark:text-emerald-400">
                              +{p.credit}
                            </span>
                          )}
                        </TableCell>
                        <TableCell>
                          <Badge variant="secondary" className="rounded-full text-[10px]">
                            {p.prize_type === 'voucher'
                              ? t('prizes.typeVoucher')
                              : p.prize_type === 'credit'
                                ? t('prizes.typeCredit')
                                : t('prizes.typeOther')}
                          </Badge>
                        </TableCell>
                        <TableCell className="max-w-[180px] truncate text-xs" title={p.uid}>
                          {p.nickname || p.uid || '—'}
                        </TableCell>
                        <TableCell>
                          <span className={'flex items-center gap-1 text-[11px] font-medium ' + st.tone}>
                            <st.Icon className="h-3 w-3" />
                            {t(`prizes.status_${p.status}`)}
                          </span>
                        </TableCell>
                        <TableCell className="pr-4 text-right">
                          {isAdmin && (
                            <div className="flex items-center justify-end gap-1">
                              {p.status !== 'redeemed' ? (
                                <Button variant="ghost" size="sm"
                                        className="h-7 rounded-full px-2 text-[11px]"
                                        onClick={() => updateStatus(p, 'redeemed')}>
                                  {t('prizes.markRedeemed')}
                                </Button>
                              ) : (
                                <Button variant="ghost" size="sm"
                                        className="h-7 rounded-full px-2 text-[11px]"
                                        onClick={() => updateStatus(p, 'pending')}>
                                  <Undo2 className="h-3.5 w-3.5" />
                                </Button>
                              )}
                              <ConfirmDialog
                                title={t('prizes.deleteTitle')}
                                description={t('prizes.deleteDesc', {label: p.label})}
                                confirmText={t('common.clear')}
                                destructive
                                onConfirm={async () => {
                                  await accountApi.removeLotteryPrize(p.id);
                                  await afterRemove();
                                }}
                                trigger={
                                  <Button variant="ghost" size="sm"
                                          className="h-7 rounded-full px-2 text-red-500">
                                    <Trash2 className="h-3.5 w-3.5" />
                                  </Button>
                                }
                              />
                            </div>
                          )}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          </>
        ) : failed ? null : hasAny ? (
          <div className="px-4 py-10 text-center text-xs text-muted-foreground">
            {t('prizes.filterEmpty')}
          </div>
        ) : (
          <EmptyState
            icon={Gift}
            title={t('prizes.emptyTitle')}
            description={t('prizes.emptyDesc')}
          />
        )}

        {stats && stats.total > 0 && (
          <div className="flex shrink-0 flex-wrap items-center gap-x-2 border-t border-border/40 px-4 py-2 text-[11px] text-muted-foreground">
            <span className="tabular-nums">{t('prizes.footerTotal', {n: fmtNumber(stats.total)})}</span>
          </div>
        )}
      </section>
    </div>
  );
}

function StatCard({
  icon: Icon, label, value,
}: {
  icon: typeof Gift;
  label: string;
  value: string;
}) {
  return (
    <div className="flex items-center gap-3 rounded-2xl bg-muted px-4 py-3">
      <div className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-background">
        <Icon className="h-4 w-4 text-muted-foreground" />
      </div>
      <div className="min-w-0">
        <div className="truncate text-[11px] text-muted-foreground">{label}</div>
        <div className="truncate text-sm font-semibold tabular-nums">{value}</div>
      </div>
    </div>
  );
}

function PrizesSkeleton() {
  return (
    <>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {Array.from({length: 4}, (_, i) => (
          <div key={i} className="flex items-center gap-3 rounded-2xl bg-muted px-4 py-3">
            <SkeletonBar className="h-9 w-9 rounded-full" />
            <div className="flex-1 space-y-1.5">
              <SkeletonBar className="h-2.5 w-16" />
              <SkeletonBar className="h-3 w-10" />
            </div>
          </div>
        ))}
      </div>
      <section className="flex flex-col rounded-[20px] bg-muted p-4">
        <SkeletonBar className="mb-4 h-3.5 w-28" />
        <div className="space-y-2.5">
          {Array.from({length: 8}, (_, i) => (
            <SkeletonBar key={i} className="h-3 w-full" />
          ))}
        </div>
      </section>
    </>
  );
}
