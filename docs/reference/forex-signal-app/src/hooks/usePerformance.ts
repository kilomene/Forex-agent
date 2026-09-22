import { useCallback, useEffect, useState } from 'react';
import { getLatestPerformance } from '@/api/client';
import type { PerformanceSnapshot } from '@/api/types';

export function usePerformance() {
  const [snapshot, setSnapshot] = useState<PerformanceSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const result = await getLatestPerformance();
      setSnapshot(result.available ? result.snapshot : null);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load performance');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return { snapshot, loading, error, refresh: load };
}
