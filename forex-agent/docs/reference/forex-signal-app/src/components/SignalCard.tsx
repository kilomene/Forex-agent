import { Pressable, StyleSheet, Text, View } from 'react-native';
import { DirectionBadge } from './DirectionBadge';
import { StatusBadge } from './StatusBadge';
import { PriceSparkline } from './PriceSparkline';
import { colors, radius, spacing, typography } from '@/theme/tokens';
import { formatPips, formatPrice, formatRelativeTime, riskRewardRatio } from '@/utils/formatting';
import { parseRecentCandles, type Signal } from '@/api/types';

export function SignalCard({ signal, onPress }: { signal: Signal; onPress: () => void }) {
  const rr = riskRewardRatio(signal.entry_price, signal.stop_loss, signal.take_profit);
  const candles = parseRecentCandles(signal);

  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [styles.card, pressed && styles.cardPressed]}
    >
      <View style={styles.topRow}>
        <View style={styles.symbolRow}>
          <Text style={styles.symbol}>{signal.symbol}</Text>
          <Text style={styles.timeframe}>{signal.timeframe}</Text>
        </View>
        <Text style={styles.time}>{formatRelativeTime(signal.created_at)}</Text>
      </View>

      <View style={styles.badgeRow}>
        <DirectionBadge direction={signal.direction} size="sm" />
        <StatusBadge status={signal.status} />
        {signal.ai_confidence != null && (
          <View style={styles.confidencePill}>
            <Text style={styles.confidenceText}>{signal.ai_confidence}% confidence</Text>
          </View>
        )}
      </View>

      {candles.length > 1 && (
        <View style={styles.sparklineRow}>
          <PriceSparkline candles={candles} direction={signal.direction} width={260} height={44} />
        </View>
      )}

      <View style={styles.statsRow}>
        <Stat label="ENTRY" value={formatPrice(signal.entry_price, signal.symbol)} />
        <Stat label="STOP" value={formatPips(signal.entry_price, signal.stop_loss, signal.symbol)} muted />
        <Stat label="TARGET" value={formatPips(signal.entry_price, signal.take_profit, signal.symbol)} />
        <Stat label="R:R" value={`1:${rr.toFixed(1)}`} />
      </View>

      {signal.ai_reasoning && (
        <Text style={styles.reasoning} numberOfLines={2}>
          {signal.ai_reasoning}
        </Text>
      )}
    </Pressable>
  );
}

function Stat({ label, value, muted }: { label: string; value: string; muted?: boolean }) {
  return (
    <View style={styles.stat}>
      <Text style={styles.statLabel}>{label}</Text>
      <Text style={[styles.statValue, muted && { color: colors.textSecondary }]}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.surface,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
    padding: spacing.lg,
    marginBottom: spacing.md,
    gap: spacing.md,
  },
  cardPressed: {
    backgroundColor: colors.surfaceRaised,
    borderColor: colors.textTertiary,
  },
  topRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
  },
  symbolRow: {
    flexDirection: 'row',
    alignItems: 'baseline',
    gap: spacing.sm,
  },
  symbol: {
    ...typography.headline,
    color: colors.textPrimary,
  },
  timeframe: {
    ...typography.caption,
    color: colors.textTertiary,
  },
  time: {
    ...typography.caption,
    color: colors.textTertiary,
  },
  badgeRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.sm,
    flexWrap: 'wrap',
  },
  confidencePill: {
    paddingHorizontal: spacing.sm,
    paddingVertical: 4,
    borderRadius: radius.pill,
    backgroundColor: colors.amberMuted,
  },
  confidenceText: {
    ...typography.label,
    color: colors.amber,
  },
  sparklineRow: {
    alignItems: 'center',
  },
  statsRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
  },
  stat: {
    gap: 2,
  },
  statLabel: {
    ...typography.label,
    color: colors.textTertiary,
  },
  statValue: {
    ...typography.mono,
    color: colors.textPrimary,
  },
  reasoning: {
    ...typography.caption,
    color: colors.textSecondary,
    lineHeight: 18,
  },
});
