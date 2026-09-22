import { StyleSheet, Text, View } from 'react-native';
import { colors, radius, spacing, typography } from '@/theme/tokens';
import { directionLabel } from '@/utils/formatting';
import type { Direction } from '@/api/types';

export function DirectionBadge({ direction, size = 'md' }: { direction: Direction; size?: 'sm' | 'md' }) {
  const isBuy = direction === 'BUY';
  const bg = isBuy ? colors.buyMuted : colors.sellMuted;
  const fg = isBuy ? colors.buy : colors.sell;

  return (
    <View style={[styles.badge, size === 'sm' && styles.badgeSm, { backgroundColor: bg }]}>
      <Text style={[styles.text, size === 'sm' && styles.textSm, { color: fg }]}>
        {isBuy ? '↑' : '↓'} {directionLabel(direction)}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  badge: {
    alignSelf: 'flex-start',
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.xs,
    borderRadius: radius.sm,
  },
  badgeSm: {
    paddingHorizontal: spacing.sm,
    paddingVertical: 3,
  },
  text: {
    ...typography.bodyStrong,
  },
  textSm: {
    fontSize: 12,
    fontWeight: '700',
  },
});
