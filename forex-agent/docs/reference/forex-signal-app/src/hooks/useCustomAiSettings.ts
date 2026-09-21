import { useCallback, useEffect, useState } from 'react';
import {
  clearCustomAiSettings,
  getCustomAiSettings,
  saveCustomAiApiKey,
  saveCustomAiUrl,
  testCustomAiModel,
  testCustomAiVm,
} from '@/api/client';

type TestState = 'idle' | 'testing' | 'success' | 'failure';

export function useCustomAiSettings() {
  const [url, setUrl] = useState<string | null>(null);
  const [hasApiKey, setHasApiKey] = useState(false);
  const [loading, setLoading] = useState(true);

  const [vmTestState, setVmTestState] = useState<TestState>('idle');
  const [vmTestDetail, setVmTestDetail] = useState<string | null>(null);
  const [modelTestState, setModelTestState] = useState<TestState>('idle');
  const [modelTestDetail, setModelTestDetail] = useState<string | null>(null);

  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const result = await getCustomAiSettings();
      setUrl(result.url);
      setHasApiKey(result.has_api_key);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  /** Test the VM's reachability, then save the URL only if the test passed. */
  const testAndSaveUrl = useCallback(async (candidateUrl: string) => {
    setVmTestState('testing');
    setVmTestDetail(null);
    try {
      const result = await testCustomAiVm(candidateUrl);
      if (!result.ok) {
        setVmTestState('failure');
        setVmTestDetail(typeof result.detail === 'string' ? result.detail : JSON.stringify(result.detail));
        return false;
      }
      setVmTestState('success');
      setSaving(true);
      await saveCustomAiUrl(candidateUrl);
      setUrl(candidateUrl);
      setSaving(false);
      return true;
    } catch (err) {
      setVmTestState('failure');
      setVmTestDetail(err instanceof Error ? err.message : 'Unknown error');
      setSaving(false);
      return false;
    }
  }, []);

  /** Test the model/API key against the (already-saved or candidate) URL, then save the key if it passed. */
  const testAndSaveApiKey = useCallback(
    async (candidateApiKey: string, urlOverride?: string) => {
      const targetUrl = urlOverride ?? url;
      if (!targetUrl) {
        setModelTestState('failure');
        setModelTestDetail('Save the VM address first — the model test needs somewhere to connect to.');
        return false;
      }

      setModelTestState('testing');
      setModelTestDetail(null);
      try {
        const result = await testCustomAiModel(targetUrl, candidateApiKey);
        if (!result.ok) {
          setModelTestState('failure');
          setModelTestDetail(result.detail ?? 'Test failed.');
          return false;
        }
        setModelTestState('success');
        setModelTestDetail(result.sample_response ? `Model responded: "${result.sample_response}"` : null);
        setSaving(true);
        await saveCustomAiApiKey(candidateApiKey);
        setHasApiKey(true);
        setSaving(false);
        return true;
      } catch (err) {
        setModelTestState('failure');
        setModelTestDetail(err instanceof Error ? err.message : 'Unknown error');
        setSaving(false);
        return false;
      }
    },
    [url]
  );

  const disconnect = useCallback(async () => {
    await clearCustomAiSettings();
    setUrl(null);
    setHasApiKey(false);
    setVmTestState('idle');
    setModelTestState('idle');
  }, []);

  return {
    url,
    hasApiKey,
    loading,
    saving,
    vmTestState,
    vmTestDetail,
    modelTestState,
    modelTestDetail,
    testAndSaveUrl,
    testAndSaveApiKey,
    disconnect,
    refresh: load,
  };
}
