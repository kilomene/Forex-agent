import { useState } from 'react';
import { useRouter } from 'expo-router';
import { Alert, ScrollView, StyleSheet, Switch, Text, TextInput, View } from 'react-native';
import { useTradingMode } from '@/hooks/useTradingMode';
import { useKillSwitch } from '@/hooks/useKillSwitch';
import { useCustomAiSettings } from '@/hooks/useCustomAiSettings';
import { useAiProviderInfo } from '@/hooks/useAiProviderInfo';
import { clearApiConfig, getApiConfig } from '@/api/config';
import { sendTestPush } from '@/api/client';
import {
  isBiometricHardwareAvailable,
  isBiometricLockEnabled,
  setBiometricLockEnabled,
} from '@/hooks/useBiometricLock';
import { registerForPushNotifications } from '@/notifications/register';
import { Button } from '@/components/Button';
import { colors, radius, spacing, typography } from '@/theme/tokens';
import { useEffect } from 'react';

export default function SettingsScreen() {
  const router = useRouter();
  const { mode, loading, updating, error, updateMode } = useTradingMode();
  const killSwitch = useKillSwitch();
  const customAi = useCustomAiSettings();
  const aiProviderInfo = useAiProviderInfo();
  const [vmUrlInput, setVmUrlInput] = useState('');
  const [apiKeyInput, setApiKeyInput] = useState('');
  const [baseUrl, setBaseUrl] = useState<string | null>(null);
  const [notifStatus, setNotifStatus] = useState<'idle' | 'requesting' | 'granted' | 'denied'>('idle');
  const [testPushStatus, setTestPushStatus] = useState<'idle' | 'sending' | 'sent'>('idle');
  const [biometricEnabled, setBiometricEnabled] = useState(false);
  const [biometricAvailable, setBiometricAvailable] = useState(true);

  useEffect(() => {
    isBiometricLockEnabled().then(setBiometricEnabled);
    isBiometricHardwareAvailable().then(setBiometricAvailable);
  }, []);

  useEffect(() => {
    if (customAi.url) setVmUrlInput(customAi.url);
  }, [customAi.url]);

  const handleToggleBiometric = async (value: boolean) => {
    if (value && !biometricAvailable) {
      Alert.alert('Not available', 'No Face ID/fingerprint is set up on this device.');
      return;
    }
    await setBiometricLockEnabled(value);
    setBiometricEnabled(value);
  };

  const handleToggleKillSwitch = (value: boolean) => {
    if (value) {
      Alert.alert(
        'Halt all new entries?',
        'No new trade can open — manual approval will be rejected and autonomous mode will not auto-approve anything, until this is switched off again. Positions already open are NOT affected by this alone.',
        [
          { text: 'Cancel', style: 'cancel' },
          { text: 'Halt entries', style: 'destructive', onPress: () => killSwitch.toggle(true) },
        ]
      );
    } else {
      killSwitch.toggle(false);
    }
  };

  const handleCloseAll = () => {
    // The most destructive single action in the app — force-closes every
    // real open position, bypassing every normal exit rule, regardless of
    // profit or loss. Two-step confirmation, and the second step requires
    // reading a distinct, specific warning rather than just tapping the
    // same dialog twice — a double-tap on one dialog isn't real friction.
    Alert.alert(
      'Close ALL open positions?',
      'Every position this bot manages will be force-closed immediately at current market price, regardless of profit or loss. This bypasses trailing stops, take-profit, and every other exit rule.',
      [
        { text: 'Cancel', style: 'cancel' },
        {
          text: 'Continue',
          style: 'destructive',
          onPress: () => {
            Alert.alert(
              'Are you absolutely sure?',
              'This cannot be undone. Every open position closes now at whatever price the market is at.',
              [
                { text: 'Cancel', style: 'cancel' },
                {
                  text: 'Close everything now',
                  style: 'destructive',
                  onPress: () => killSwitch.closeAll(),
                },
              ]
            );
          },
        },
      ]
    );
  };

  const handleTestAndSaveVm = async () => {
    if (!vmUrlInput.trim()) {
      Alert.alert('Enter an address', 'Enter the VM\'s URL first, e.g. http://43.111.42.21:8000');
      return;
    }
    const ok = await customAi.testAndSaveUrl(vmUrlInput.trim());
    if (!ok) {
      Alert.alert('Connection failed', customAi.vmTestDetail ?? 'Could not reach that address.');
    }
  };

  const handleTestAndSaveApiKey = async () => {
    if (!apiKeyInput.trim()) {
      Alert.alert('Enter a key', 'Enter the model\'s API key first.');
      return;
    }
    const ok = await customAi.testAndSaveApiKey(apiKeyInput.trim(), vmUrlInput.trim() || undefined);
    if (ok) {
      setApiKeyInput(''); // don't leave a saved secret sitting in a visible text field
    } else {
      Alert.alert('Model test failed', customAi.modelTestDetail ?? 'Could not verify the model connection.');
    }
  };

  const handleDisconnectCustomAi = () => {
    Alert.alert(
      'Disconnect self-hosted model?',
      'Reasoning will fall back to whatever provider the Worker\'s own deploy-time settings specify.',
      [
        { text: 'Cancel', style: 'cancel' },
        {
          text: 'Disconnect',
          style: 'destructive',
          onPress: async () => {
            await customAi.disconnect();
            setVmUrlInput('');
            setApiKeyInput('');
          },
        },
      ]
    );
  };

  useEffect(() => {
    getApiConfig().then((config) => setBaseUrl(config?.baseUrl ?? null));
  }, []);

  const handleToggleAutonomous = (value: boolean) => {
    if (value) {
      // This is the single most consequential toggle in the app — real
      // money can move without a human tap after this. A native switch
      // flick is too easy to hit by accident; force an explicit confirm.
      Alert.alert(
        'Enable autonomous trading?',
        'Zenas will decide entries on its own — its "enter" verdict will be auto-approved without you tapping anything. The risk engine still enforces hard limits, but no human reviews each trade beforehand.',
        [
          { text: 'Cancel', style: 'cancel' },
          { text: 'Enable', style: 'destructive', onPress: () => updateMode('autonomous') },
        ]
      );
    } else {
      updateMode('manual');
    }
  };

  const handleEnableNotifications = async () => {
    setNotifStatus('requesting');
    const result = await registerForPushNotifications();
    setNotifStatus(result.ok ? 'granted' : 'denied');
    if (!result.ok && result.reason === 'not-physical-device') {
      Alert.alert('Simulator detected', 'Push notifications require a physical device.');
    } else if (!result.ok && result.reason === 'registration-failed') {
      Alert.alert(
        'Registration failed',
        'This usually means the app was opened in Expo Go rather than a native build — raw FCM tokens require an EAS build with google-services.json configured.'
      );
    }
  };

  const handleTestPush = async () => {
    setTestPushStatus('sending');
    try {
      const result = await sendTestPush();
      if (result.ok) {
        setTestPushStatus('sent');
        Alert.alert('Sent', `Test push sent to ${result.sent}/${result.total} device(s). It should arrive shortly.`);
      } else {
        setTestPushStatus('idle');
        Alert.alert('Could not send', result.error ?? 'Unknown error — check the Worker logs.');
      }
    } catch (err) {
      setTestPushStatus('idle');
      Alert.alert('Could not send', err instanceof Error ? err.message : 'Unknown error');
    }
  };

  const handleDisconnect = () => {
    Alert.alert('Disconnect', 'This will remove your saved Worker connection from this device.', [
      { text: 'Cancel', style: 'cancel' },
      {
        text: 'Disconnect',
        style: 'destructive',
        onPress: async () => {
          await clearApiConfig();
          router.replace('/onboarding');
        },
      },
    ]);
  };

  return (
    <ScrollView style={styles.container} contentContainerStyle={styles.content}>
      <Text style={styles.headerTitle}>Settings</Text>

      <View style={[styles.card, styles.killSwitchCard]}>
        <Text style={styles.killSwitchTitle}>🛑 Kill switch</Text>

        <View style={styles.row}>
          <View style={styles.rowText}>
            <Text style={styles.rowLabel}>Halt all new entries</Text>
            <Text style={styles.rowDescription}>
              {killSwitch.engaged
                ? 'Active — no new trade can open until this is switched off.'
                : 'Off — trading proceeds normally.'}
            </Text>
          </View>
          <Switch
            value={killSwitch.engaged}
            onValueChange={handleToggleKillSwitch}
            disabled={killSwitch.loading || killSwitch.updating}
            trackColor={{ false: colors.border, true: colors.sellMuted }}
            thumbColor={killSwitch.engaged ? colors.sell : colors.textTertiary}
          />
        </View>

        {killSwitch.closeAllRequestedAt && (
          <Text style={styles.closeAllPending}>
            Close-all requested at {new Date(killSwitch.closeAllRequestedAt).toLocaleTimeString()} — waiting for the bridge to pick it up.
          </Text>
        )}

        {killSwitch.error && <Text style={styles.error}>{killSwitch.error}</Text>}

        <Button
          label="Close all positions now"
          onPress={handleCloseAll}
          variant="danger"
          loading={killSwitch.updating}
        />

        <Text style={styles.footnote}>
          This toggle only stops NEW trades — it does not touch positions
          already open. Use "Close all positions now" to force-close
          everything immediately, bypassing every normal exit rule.
        </Text>
      </View>

      <Section title="Trading mode">
        <View style={styles.row}>
          <View style={styles.rowText}>
            <Text style={styles.rowLabel}>Autonomous entry</Text>
            <Text style={styles.rowDescription}>
              {mode === 'autonomous'
                ? "Zenas's own decision gates entry — no manual approval needed."
                : 'Every signal waits for your approval before it can execute.'}
            </Text>
          </View>
          <Switch
            value={mode === 'autonomous'}
            onValueChange={handleToggleAutonomous}
            disabled={loading || updating}
            trackColor={{ false: colors.border, true: colors.sellMuted }}
            thumbColor={mode === 'autonomous' ? colors.sell : colors.textTertiary}
          />
        </View>
        {error && <Text style={styles.error}>{error}</Text>}
        <Text style={styles.footnote}>
          Exit management (trailing stops, breakeven, time exits) always runs regardless of this
          setting — it can only reduce risk on open positions, never open new ones.
        </Text>
      </Section>

      <Section title="Notifications">
        <View style={styles.row}>
          <View style={styles.rowText}>
            <Text style={styles.rowLabel}>Push notifications</Text>
            <Text style={styles.rowDescription}>
              {notifStatus === 'granted'
                ? 'Enabled — you\'ll be notified of new signals and outcomes.'
                : 'Get notified the moment a new signal is detected.'}
            </Text>
          </View>
        </View>
        <Button
          label={notifStatus === 'granted' ? 'Enabled' : 'Enable notifications'}
          onPress={handleEnableNotifications}
          variant="secondary"
          loading={notifStatus === 'requesting'}
          disabled={notifStatus === 'granted'}
        />
        {notifStatus === 'granted' && (
          <Button
            label="Send test notification"
            onPress={handleTestPush}
            variant="ghost"
            loading={testPushStatus === 'sending'}
          />
        )}
      </Section>

      <Section title="Reasoning engine">
        {aiProviderInfo.loading ? (
          <Text style={styles.rowDescription}>Checking…</Text>
        ) : aiProviderInfo.error ? (
          <Text style={styles.error}>{aiProviderInfo.error}</Text>
        ) : (
          <>
            <View style={styles.row}>
              <View style={styles.rowText}>
                <Text style={styles.rowLabel}>
                  Provider: {aiProviderInfo.provider}
                  {aiProviderInfo.model ? ` (${aiProviderInfo.model})` : ''}
                </Text>
                <Text style={aiProviderInfo.usesToolCalling ? styles.testSuccess : styles.rowDescription}>
                  {aiProviderInfo.usesToolCalling
                    ? '✓ Zenas is using the real multi-turn agent loop — it decides which tools to call.'
                    : 'Using the pre-fetched-context path — evidence is gathered upfront in one prompt, not via live tool decisions. This is expected for self-hosted models, which mostly can\'t do reliable multi-turn tool use.'}
                </Text>
              </View>
            </View>
            <Button label="Refresh" onPress={aiProviderInfo.refresh} variant="ghost" />
          </>
        )}
        <Text style={styles.footnote}>
          Check this after changing providers, or after a signal fires, to
          confirm the agent loop is actually running rather than assuming it.
        </Text>
      </Section>

      <Section title="AI model (self-hosted)">
        <View style={styles.rowText}>
          <Text style={styles.rowLabel}>VM address</Text>
          <Text style={styles.rowDescription}>
            {customAi.url ? `Connected: ${customAi.url}` : 'Not connected — Zenas uses the Worker\'s default provider.'}
          </Text>
        </View>
        <TextInput
          style={styles.input}
          placeholder="http://43.111.42.21:8000"
          placeholderTextColor={colors.textTertiary}
          value={vmUrlInput}
          onChangeText={setVmUrlInput}
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
        />
        {customAi.vmTestState === 'success' && (
          <Text style={styles.testSuccess}>✓ Reachable and saved.</Text>
        )}
        {customAi.vmTestState === 'failure' && customAi.vmTestDetail && (
          <Text style={styles.error}>{customAi.vmTestDetail}</Text>
        )}
        <Button
          label="Test & save VM address"
          onPress={handleTestAndSaveVm}
          variant="secondary"
          loading={customAi.vmTestState === 'testing' || customAi.saving}
        />

        <View style={[styles.rowText, { marginTop: spacing.md }]}>
          <Text style={styles.rowLabel}>Model API key</Text>
          <Text style={styles.rowDescription}>
            {customAi.hasApiKey ? 'A key is saved.' : 'No key saved yet.'}
          </Text>
        </View>
        <TextInput
          style={styles.input}
          placeholder={customAi.hasApiKey ? '••••••••  (enter a new key to replace it)' : 'Enter your model server\'s API key'}
          placeholderTextColor={colors.textTertiary}
          value={apiKeyInput}
          onChangeText={setApiKeyInput}
          autoCapitalize="none"
          autoCorrect={false}
          secureTextEntry
        />
        {customAi.modelTestState === 'success' && (
          <Text style={styles.testSuccess}>✓ Model responded correctly and key saved.</Text>
        )}
        {customAi.modelTestState === 'failure' && customAi.modelTestDetail && (
          <Text style={styles.error}>{customAi.modelTestDetail}</Text>
        )}
        <Button
          label="Test & save API key"
          onPress={handleTestAndSaveApiKey}
          variant="secondary"
          loading={customAi.modelTestState === 'testing' || customAi.saving}
        />

        {(customAi.url || customAi.hasApiKey) && (
          <Button label="Disconnect self-hosted model" onPress={handleDisconnectCustomAi} variant="ghost" />
        )}

        <Text style={styles.footnote}>
          Configuring this here overrides the Worker's default AI provider —
          Zenas will reason using this model instead, until you disconnect.
          Testing always happens before saving, so a wrong address or key
          never gets silently persisted.
        </Text>
      </Section>

      <Section title="Security">
        <View style={styles.row}>
          <View style={styles.rowText}>
            <Text style={styles.rowLabel}>Require Face ID / fingerprint</Text>
            <Text style={styles.rowDescription}>
              {biometricAvailable
                ? 'Lock the app behind biometrics — it can approve real trades.'
                : 'No Face ID/fingerprint is enrolled on this device.'}
            </Text>
          </View>
          <Switch
            value={biometricEnabled}
            onValueChange={handleToggleBiometric}
            disabled={!biometricAvailable}
            trackColor={{ false: colors.border, true: colors.buyMuted }}
            thumbColor={biometricEnabled ? colors.buy : colors.textTertiary}
          />
        </View>
      </Section>

      <Section title="Connection">
        <View style={styles.row}>
          <View style={styles.rowText}>
            <Text style={styles.rowLabel}>Worker URL</Text>
            <Text style={styles.rowDescription} numberOfLines={1}>
              {baseUrl ?? 'Not configured'}
            </Text>
          </View>
        </View>
        <Button label="Disconnect" onPress={handleDisconnect} variant="danger" />
      </Section>
    </ScrollView>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <View style={styles.section}>
      <Text style={styles.sectionTitle}>{title}</Text>
      <View style={styles.card}>{children}</View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.background },
  content: { padding: spacing.lg, gap: spacing.xl, paddingBottom: spacing.xxl },
  headerTitle: { ...typography.displayLarge, color: colors.textPrimary },
  killSwitchCard: {
    borderColor: colors.sell,
    gap: spacing.md,
  },
  killSwitchTitle: { ...typography.title, color: colors.sell },
  closeAllPending: { ...typography.caption, color: colors.amber },
  section: { gap: spacing.sm },
  sectionTitle: { ...typography.label, color: colors.textTertiary },
  card: {
    backgroundColor: colors.surface,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    padding: spacing.lg,
    gap: spacing.md,
  },
  row: { flexDirection: 'row', alignItems: 'center', gap: spacing.md },
  rowText: { flex: 1, gap: 2 },
  rowLabel: { ...typography.bodyStrong, color: colors.textPrimary },
  rowDescription: { ...typography.caption, color: colors.textSecondary },
  error: { ...typography.caption, color: colors.sell },
  testSuccess: { ...typography.caption, color: colors.buy },
  input: {
    backgroundColor: colors.background,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    padding: spacing.md,
    color: colors.textPrimary,
    ...typography.body,
  },
  footnote: { ...typography.caption, color: colors.textTertiary, lineHeight: 17 },
});
