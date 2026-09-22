import { useCallback, useEffect, useState } from 'react';
import { getAiProviderInfo } from '@/api/client';

export function useAiProviderInfo() {
  const [provider, setProvider] = useState<string | null>(null);
  const [usesToolCalling, setUsesToolCalling] = useState(false);
  const [model, setModel] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const result = await getAiProviderInfo();
      setProvider(result.provider);
      setUsesToolCalling(result.uses_tool_calling);
      setModel(result.model);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load provider info');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return { provider, usesToolCalling, model, loading, error, refresh: load };
}
