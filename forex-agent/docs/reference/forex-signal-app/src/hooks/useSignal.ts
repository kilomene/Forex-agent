import { useCallback, useEffect, useState } from 'react';
import { approveSignal, getSignal, rejectSignal } from '@/api/client';
import type { Signal } from '@/api/types';

export function useSignal(id: string) {
  const [signal, setSignal] = useState<Signal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState<'approve' | 'reject' | null>(null);

  const load = useCallback(async () => {
    try {
      const result = await getSignal(id);
      setSignal(result.signal);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load signal');
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  const approve = useCallback(async () => {
    setActionLoading('approve');
    try {
      await approveSignal(id);
      await load();
    } finally {
      setActionLoading(null);
    }
  }, [id, load]);

  const reject = useCallback(async () => {
    setActionLoading('reject');
    try {
      await rejectSignal(id);
      await load();
    } finally {
      setActionLoading(null);
    }
  }, [id, load]);

  return { signal, loading, error, actionLoading, approve, reject, refresh: load };
}
