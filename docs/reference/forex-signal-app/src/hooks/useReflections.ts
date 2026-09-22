import { useCallback, useEffect, useState } from 'react';
import { listReflections } from '@/api/client';
import type { Reflection } from '@/api/types';

export function useReflections(symbol?: string) {
  const [reflections, setReflections] = useState<Reflection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const result = await listReflections({ symbol, limit: 20 });
      setReflections(result.reflections);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load reflections');
    } finally {
      setLoading(false);
    }
  }, [symbol]);

  useEffect(() => {
    load();
  }, [load]);

  return { reflections, loading, error, refresh: load };
}
