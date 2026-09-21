import { useCallback, useEffect, useRef, useState } from 'react';
import { AppState } from 'react-native';
import { listSignals, NotConfiguredError } from '@/api/client';
import type { Signal, SignalStatus } from '@/api/types';

const POLL_INTERVAL_MS = 30_000;

export function useSignals(status?: SignalStatus) {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notConfigured, setNotConfigured] = useState(false);

  const load = useCallback(async (isRefresh = false) => {
    if (isRefresh) setRefreshing(true);
    try {
      const result = await listSignals({ status, limit: 50 });
      setSignals(result.signals);
      setError(null);
      setNotConfigured(false);
    } catch (err) {
      if (err instanceof NotConfiguredError) {
        setNotConfigured(true);
      } else {
        setError(err instanceof Error ? err.message : 'Failed to load signals');
      }
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [status]);

  useEffect(() => {
    load();

    // Poll while the app is in the foreground — push notifications cover
    // the backgrounded case, this just keeps an open screen fresh.
    const interval = setInterval(() => {
      if (AppState.currentState === 'active') load();
    }, POLL_INTERVAL_MS);

    return () => clearInterval(interval);
  }, [load]);

  return {
    signals,
    loading,
    refreshing,
    error,
    notConfigured,
    refresh: () => load(true),
  };
}
