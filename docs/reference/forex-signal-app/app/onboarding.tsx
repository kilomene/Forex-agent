import { useState } from 'react';
import { useRouter } from 'expo-router';
import {
  KeyboardAvoidingView,
  Platform,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { setApiConfig } from '@/api/config';
import { testConnection } from '@/api/client';
import { registerForPushNotifications } from '@/notifications/register';
import { Button } from '@/components/Button';
import { colors, radius, spacing, typography } from '@/theme/tokens';

export default function Onboarding() {
  const router = useRouter();
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = baseUrl.trim().length > 0 && apiKey.trim().length > 0;

  const handleConnect = async () => {
    setError(null);
    setTesting(true);

    const cleanUrl = baseUrl.trim().replace(/\/$/, '');
    try {
      const ok = await testConnection(cleanUrl, apiKey.trim());
      if (!ok) {
        setError('Could not authenticate with that URL and key. Double-check both against your Worker deployment.');
        setTesting(false);
        return;
      }

      await setApiConfig({ baseUrl: cleanUrl, apiKey: apiKey.trim() });

      // Best-effort — push notifications aren't required to use the app,
      // and this silently no-ops in Expo Go (see notifications/register.ts).
      registerForPushNotifications().catch(() => {});

      router.replace('/(tabs)/signals');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not reach that URL — check it\'s correct and reachable.');
      setTesting(false);
    }
  };

  return (
    <KeyboardAvoidingView
      style={styles.flex}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <ScrollView contentContainerStyle={styles.container}>
        <View style={styles.header}>
          <Text style={styles.brandLabel}>ALPHA</Text>
          <Text style={styles.title}>Connect Zenas</Text>
          <Text style={styles.subtitle}>
            Enter your Worker deployment URL and the shared API key from your Worker's secrets to connect to Zenas, your trading agent.
          </Text>
        </View>

        <View style={styles.field}>
          <Text style={styles.label}>WORKER URL</Text>
          <TextInput
            style={styles.input}
            placeholder="https://forex-signal-worker.your-subdomain.workers.dev"
            placeholderTextColor={colors.textTertiary}
            value={baseUrl}
            onChangeText={setBaseUrl}
            autoCapitalize="none"
            autoCorrect={false}
            keyboardType="url"
          />
        </View>

        <View style={styles.field}>
          <Text style={styles.label}>API KEY</Text>
          <TextInput
            style={styles.input}
            placeholder="Your WORKER_API_KEY secret"
            placeholderTextColor={colors.textTertiary}
            value={apiKey}
            onChangeText={setApiKey}
            autoCapitalize="none"
            autoCorrect={false}
            secureTextEntry
          />
        </View>

        {error && <Text style={styles.error}>{error}</Text>}

        <Button label="Connect" onPress={handleConnect} loading={testing} disabled={!canSubmit} />

        <Text style={styles.footnote}>
          Your key is stored securely on this device only and sent solely to the URL above.
        </Text>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1, backgroundColor: colors.background },
  container: {
    flexGrow: 1,
    justifyContent: 'center',
    padding: spacing.xl,
    gap: spacing.lg,
  },
  header: {
    gap: spacing.sm,
    marginBottom: spacing.md,
  },
  title: {
    ...typography.displayLarge,
    color: colors.textPrimary,
  },
  brandLabel: {
    ...typography.label,
    color: colors.buy,
    marginBottom: spacing.xs,
  },
  subtitle: {
    ...typography.body,
    color: colors.textSecondary,
  },
  field: {
    gap: spacing.xs,
  },
  label: {
    ...typography.label,
    color: colors.textTertiary,
  },
  input: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    padding: spacing.md,
    color: colors.textPrimary,
    ...typography.body,
  },
  error: {
    ...typography.caption,
    color: colors.sell,
  },
  footnote: {
    ...typography.caption,
    color: colors.textTertiary,
    textAlign: 'center',
    marginTop: spacing.md,
  },
});
