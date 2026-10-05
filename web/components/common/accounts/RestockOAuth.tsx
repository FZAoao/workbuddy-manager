'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {QRCodeSVG} from 'qrcode.react';
import {CheckCircle2, ExternalLink, Loader2, ScanLine, TriangleAlert} from 'lucide-react';
import {restockApi, errText} from '@/lib/api';
import {useT} from '@/lib/i18n/provider';
import {Button} from '@/components/ui/button';
import {CopyButton} from '@/components/ui/copy-button';

const REGIONS = ['HK', 'MO', 'SG', 'TH', 'PH', 'MY', 'ID'] as const;
type Phase = 'idle' | 'starting' | 'waiting' | 'success' | 'error';

export function RestockOAuth({token, disabled, onSuccess, onLinkError}: {
  token: string;
  disabled: boolean;
  onSuccess: () => void;
  onLinkError: (status: number) => void;
}) {
  const t = useT();
  const [realm, setRealm] = useState<'cn' | 'global'>('cn');
  const [region, setRegion] = useState('');
  const [phase, setPhase] = useState<Phase>('idle');
  const [state, setState] = useState('');
  const [authUrl, setAuthUrl] = useState('');
  const [message, setMessage] = useState('');
  const polling = useRef(false);
  const generation = useRef(0);

  useEffect(() => () => { generation.current += 1; }, []);

  const reset = useCallback(() => {
    generation.current += 1;
    setState('');
    setAuthUrl('');
    setMessage('');
    setPhase('idle');
  }, []);

  const start = useCallback(async () => {
    if (disabled || (realm === 'global' && !region)) return;
    generation.current += 1;
    const current = generation.current;
    setState('');
    setAuthUrl('');
    setPhase('starting');
    setMessage(t('restock.oauthRequesting'));
    try {
      const started = await restockApi.startOAuth(token, realm, region || undefined);
      if (generation.current !== current) return;
      setState(started.state);
      setAuthUrl(started.authUrl);
      setMessage(t('restock.oauthWaiting'));
      setPhase('waiting');
    } catch (error) {
      if (generation.current !== current) return;
      const status = (error as {response?: {status?: number}})?.response?.status;
      if (status && [401, 403, 410].includes(status)) onLinkError(status);
      setMessage(errText(error));
      setPhase('error');
    }
  }, [disabled, onLinkError, realm, region, t, token]);

  useEffect(() => {
    if (phase !== 'waiting' || !state) return;
    const current = generation.current;
    let failures = 0;
    const tick = async () => {
      if (document.hidden || polling.current) return;
      polling.current = true;
      try {
        const result = await restockApi.pollOAuth(token, state);
        if (generation.current !== current) return;
        failures = 0;
        if (result.status === 'success') {
          setPhase('success');
          setMessage(t('restock.oauthSuccess', {name: result.nickname || result.uid || ''}));
          onSuccess();
        } else if (result.status === 'expired' || result.status === 'invalid') {
          setState('');
          setPhase('error');
          setMessage(t(result.status === 'expired'
            ? 'restock.oauthExpired' : 'restock.oauthStateLost'));
        }
      } catch (error) {
        if (generation.current !== current) return;
        const status = (error as {response?: {status?: number}})?.response?.status;
        if (status && [401, 403, 410].includes(status)) {
          onLinkError(status);
          return;
        }
        if (status === 400 || status === 409) {
          setState('');
          setPhase('error');
          setMessage(errText(error));
          return;
        }
        failures += 1;
        if (failures >= 3) {
          setPhase('error');
          setMessage(errText(error));
        }
      } finally {
        polling.current = false;
      }
    };
    // 授权页可能盖住当前窗口，回来时立即补查，避免后台定时器节流造成延迟。
    const wake = () => { void tick(); };
    const timer = window.setInterval(wake, 2000);
    window.addEventListener('focus', wake);
    document.addEventListener('visibilitychange', wake);
    void tick();
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('focus', wake);
      document.removeEventListener('visibilitychange', wake);
    };
  }, [onLinkError, onSuccess, phase, state, t, token]);

  return (
    <section className="space-y-4 rounded-[24px] bg-muted p-4">
      <div className="text-sm font-medium">{t('restock.oauthTitle')}</div>
      <p className="text-xs leading-5 text-muted-foreground">{t('restock.oauthDescription')}</p>
      <div className="flex gap-2">
        {(['cn', 'global'] as const).map((value) => (
          <Button key={value} type="button" size="sm"
            variant={realm === value ? 'default' : 'outline'}
            disabled={phase === 'starting' || phase === 'waiting'}
            onClick={() => { reset(); setRealm(value); }}>
            {t(value === 'cn' ? 'restock.oauthCn' : 'restock.oauthGlobal')}
          </Button>
        ))}
      </div>
      {realm === 'global' && (
        <label className="block space-y-1.5 text-xs">
          <span>{t('restock.oauthRegion')}</span>
          <select value={region} disabled={phase === 'starting' || phase === 'waiting'}
            onChange={(event) => { reset(); setRegion(event.target.value); }}
            className="h-9 w-full rounded-full border border-input bg-background px-3 text-xs">
            <option value="">{t('restock.oauthChooseRegion')}</option>
            {REGIONS.map((code) => (
              <option key={code} value={code}>{t(`region.${code}`)} ({code})</option>
            ))}
          </select>
        </label>
      )}
      {(phase === 'waiting' || phase === 'error') && authUrl && (
        <div className="flex flex-col items-center gap-3">
          <div className="rounded-2xl bg-white p-3"><QRCodeSVG value={authUrl} size={188} level="M" /></div>
          <div className="flex w-full min-w-0 items-center gap-2">
            <ExternalLink className="h-4 w-4 shrink-0 text-muted-foreground" />
            <a href={authUrl} target="_blank" rel="noopener noreferrer"
              className="min-w-0 flex-1 truncate text-xs text-blue-500 hover:underline">
              {t('restock.oauthOpenLink')}
            </a>
            <CopyButton value={authUrl} size="sm" variant="outline"
              label={t('restock.oauthCopyLink')} />
          </div>
        </div>
      )}
      {message && (
        <div className={'flex items-start gap-2 text-xs leading-5 ' +
          (phase === 'success' ? 'text-emerald-600' : phase === 'error' ? 'text-red-500' : 'text-muted-foreground')}>
          {phase === 'success' ? <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
            : phase === 'error' ? <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
              : <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin" />}
          <span>{message}</span>
        </div>
      )}
      {phase !== 'success' && (
        <Button className="w-full rounded-full" disabled={disabled || phase === 'starting' || phase === 'waiting' || (realm === 'global' && !region)}
          onClick={() => state ? setPhase('waiting') : void start()}>
          {phase === 'starting' ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <ScanLine className="mr-2 h-4 w-4" />}
          {t(phase === 'error' ? 'restock.oauthRetry' : 'restock.oauthStart')}
        </Button>
      )}
      {(phase === 'waiting' || phase === 'success') && (
        <Button variant="outline" className="w-full rounded-full" onClick={reset}>
          {t(phase === 'success' ? 'restock.oauthStart' : 'common.cancel')}
        </Button>
      )}
    </section>
  );
}
