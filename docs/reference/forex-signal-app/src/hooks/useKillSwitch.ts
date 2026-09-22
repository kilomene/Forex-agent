import { useCallback, useEffect, useState } from 'react';
import { getKillSwitchState, requestCloseAll, setKillSwitchEngaged } from '@/api/client';

export function useKillSwitch() {
  const [engaged, setEngaged] = useState(false);
  const [closeAllRequestedAt, setCloseAllRequestedAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [updating, setUpdating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const result = await getKillSwitchState();
      setEngaged(result.engaged);
      setCloseAllRequestedAt(result.close_all_requested_at);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load kill switch state');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    // Poll relatively often while this screen is open — this is the one
    // piece of state where "slightly stale" actually matters.
    const interval = setInterval(load, 10_000);
    return () => clearInterval(interval);
  }, [load]);

  const toggle = useCallback(async (value: boolean) => {
    setUpdating(true);
    const previous = engaged;
    setEngaged(value); // optimistic — this control should feel instant
    try {
      await setKillSwitchEngaged(value);
      setError(null);
    } catch (err) {
      setEngaged(previous);
      setError(err instanceof Error ? err.message : 'Failed to update kill switch');
    } finally {
      setUpdating(false);
    }
  }, [engaged]);

  const closeAll = useCallback(async () => {
    setUpdating(true);
    try {
      await requestCloseAll();
      await load();
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to request close-all');
    } finally {
      setUpdating(false);
    }
  }, [load]);

  return { engaged, closeAllRequestedAt, loading, updating, error, toggle, closeAll, refresh: load };
}
