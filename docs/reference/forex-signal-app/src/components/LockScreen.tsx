import { StyleSheet, Text, View } from 'react-native';
import { Button } from './Button';
import { colors, spacing, typography } from '@/theme/tokens';

export function LockScreen({ onRetry }: { onRetry: () => void }) {
  return (
    <View style={styles.container}>
      <Text style={styles.icon}>🔒</Text>
      <Text style={styles.title}>Locked</Text>
      <Text style={styles.message}>This app can approve real trades — unlock to continue.</Text>
      <View style={styles.button}>
        <Button label="Unlock" onPress={onRetry} variant="secondary" />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    ...StyleSheet.absoluteFillObject,
    backgroundColor: colors.background,
    alignItems: 'center',
    justifyContent: 'center',
    gap: spacing.sm,
    zIndex: 999,
  },
  icon: { fontSize: 40, marginBottom: spacing.sm },
  title: { ...typography.headline, color: colors.textPrimary },
  message: { ...typography.body, color: colors.textSecondary, textAlign: 'center', paddingHorizontal: spacing.xxl },
  button: { marginTop: spacing.lg, minWidth: 160 },
});
