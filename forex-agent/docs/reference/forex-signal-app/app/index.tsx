import { useEffect, useState } from 'react';
import { Redirect } from 'expo-router';
import { getApiConfig } from '@/api/config';
import { LoadingState } from '@/components/States';

export default function Index() {
  const [checking, setChecking] = useState(true);
  const [configured, setConfigured] = useState(false);

  useEffect(() => {
    getApiConfig().then((config) => {
      setConfigured(config !== null);
      setChecking(false);
    });
  }, []);

  if (checking) return <LoadingState />;

  return <Redirect href={configured ? '/(tabs)/signals' : '/onboarding'} />;
}
