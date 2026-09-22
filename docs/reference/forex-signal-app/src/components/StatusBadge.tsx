import { StyleSheet, Text, View } from 'react-native';
import { colors, radius, spacing, typography } from '@/theme/tokens';
import { statusLabel } from '@/utils/formatting';
import type { SignalStatus } from '@/api/types';

const STATUS_COLOR: Record<SignalStatus, string> = {
  pending: colors.statusPending,
  approved: colors.statusApproved,
  executed: colors.statusExecuted,
  closed: colors.statusClosed,
  rejected_by_user: colors.statusRejected,
  rejected_by_risk: colors.statusRejected,
  skipped_by_agent: colors.statusSkipped,
  failed: colors.statusRejected,
};

export function StatusBadge({ status }: { status: SignalStatus }) {
  const color = STATUS_COLOR[status];
  return (
    <View style={[styles.badge, { backgroundColor: `${color}22`, borderColor: `${color}55` }]}>
      <View style={[styles.dot, { backgroundColor: color }]} />
      <Text style={[styles.label, { color }]}>{statusLabel(status)}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  badge: {
    flexDirection: 'row',
    alignItems: 'center',
    alignSelf: 'flex-start',
    paddingHorizontal: spacing.sm,
    paddingVertical: 4,
    borderRadius: radius.pill,
    borderWidth: 1,
    gap: 6,
  },
  dot: {
    width: 6,
    height: 6,
    borderRadius: 3,
  },
  label: {
    ...typography.label,
  },
});
