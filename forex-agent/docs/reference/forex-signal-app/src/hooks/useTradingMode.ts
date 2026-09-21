import { useCallback, useEffect, useState } from 'react';
import { getTradingMode, setTradingMode } from '@/api/client';
import type { TradingMode } from '@/api/types';

export function useTradingMode() {
  const [mode, setModeState] = useState<TradingMode | null>(null);
  const [loading, setLoading] = useState(true);
  const [updating, setUpdating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const result = await getTradingMode();
      setModeState(result.trading_mode);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load trading mode');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const updateMode = useCallback(async (newMode: TradingMode) => {
    setUpdating(true);
    const previous = mode;
    setModeState(newMode); // optimistic — autonomous trading is the kind of
    // setting where instant UI feedback matters, but we roll back on failure
    try {
      await setTradingMode(newMode);
      setError(null);
    } catch (err) {
      setModeState(previous);
      setError(err instanceof Error ? err.message : 'Failed to update trading mode');
    } finally {
      setUpdating(false);
    }
  }, [mode]);

  return { mode, loading, updating, error, updateMode };
}
