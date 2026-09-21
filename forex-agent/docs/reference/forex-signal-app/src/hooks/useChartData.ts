import { useCallback, useEffect, useState } from 'react';
import { getChartData, listAvailableCharts } from '@/api/client';
import type { AvailableChart, ChartData } from '@/api/types';

export function useAvailableCharts() {
  const [charts, setCharts] = useState<AvailableChart[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const result = await listAvailableCharts();
      setCharts(result.charts);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load available charts');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return { charts, loading, error, refresh: load };
}

const REFRESH_INTERVAL_MS = 60_000; // matches the bridge's own reporting cadence order of magnitude — no point polling faster than the data actually changes

export function useChartData(symbol: string | null, timeframe: string | null) {
  const [data, setData] = useState<ChartData | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (isRefresh = false) => {
      if (!symbol || !timeframe) {
        setLoading(false);
        return;
      }
      if (isRefresh) setRefreshing(true);
      try {
        const result = await getChartData(symbol, timeframe);
        setData(result);
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load chart data');
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [symbol, timeframe]
  );

  useEffect(() => {
    setLoading(true);
    load();
    const interval = setInterval(() => load(), REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [load]);

  return { data, loading, refreshing, error, refresh: () => load(true) };
}
