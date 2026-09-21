import { useLocalSearchParams } from 'expo-router';
import { Alert, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useSignal } from '@/hooks/useSignal';
import { DirectionBadge } from '@/components/DirectionBadge';
import { StatusBadge } from '@/components/StatusBadge';
import { ConfidenceRing } from '@/components/ConfidenceRing';
import { CandlestickChart } from '@/components/CandlestickChart';
import { Button } from '@/components/Button';
import { ErrorState, LoadingState } from '@/components/States';
import { colors, radius, spacing, typography } from '@/theme/tokens';
import { formatCurrency, formatPrice, formatRelativeTime, formatSpread, riskRewardRatio } from '@/utils/formatting';
import { parseToolCalls, parseRecentCandles, parseSmcSummary } from '@/api/types';

export default function SignalDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const { signal, loading, error, actionLoading, approve, reject, refresh } = useSignal(id);

  if (loading) return <LoadingState message="Loading signal…" />;
  if (error || !signal) return <ErrorState message={error ?? 'Signal not found'} onRetry={refresh} />;

  const rr = riskRewardRatio(signal.entry_price, signal.stop_loss, signal.take_profit);
  const toolCalls = parseToolCalls(signal);
  const candles = parseRecentCandles(signal);
  const smcSummary = parseSmcSummary(signal);
  const canDecide = signal.status === 'pending';

  const handleApprove = () => {
    Alert.alert('Approve trade?', `This will execute a real ${signal.direction} order on ${signal.symbol} once the bridge picks it up.`, [
      { text: 'Cancel', style: 'cancel' },
      { text: 'Approve', onPress: () => approve() },
    ]);
  };

  return (
    <ScrollView style={styles.container} contentContainerStyle={styles.content}>
      <View style={styles.headerRow}>
        <View>
          <Text style={styles.symbol}>{signal.symbol}</Text>
          <Text style={styles.timeframe}>{signal.timeframe} · {formatRelativeTime(signal.created_at)}</Text>
        </View>
        <ConfidenceRing confidence={signal.ai_confidence} />
      </View>

      <View style={styles.badgeRow}>
        <DirectionBadge direction={signal.direction} />
        <StatusBadge status={signal.status} />
        {signal.ai_decision && (
          <View
            style={[
              styles.decisionPill,
              { backgroundColor: signal.ai_decision === 'enter' ? colors.buyMuted : colors.surfaceRaised },
            ]}
          >
            <Text style={[styles.decisionText, { color: signal.ai_decision === 'enter' ? colors.buy : colors.textSecondary }]}>
              Zenas: {signal.ai_decision}
            </Text>
          </View>
        )}
      </View>

      {candles.length > 1 && (
        <View style={styles.chartCard}>
          <CandlestickChart
            candles={candles}
            smcSummary={smcSummary}
            entryPrice={signal.entry_price}
            stopLoss={signal.stop_loss}
            takeProfit={signal.take_profit}
            width={320}
            height={220}
          />
        </View>
      )}

      <View style={styles.priceGrid}>
        <PriceStat label="Entry" value={formatPrice(signal.entry_price, signal.symbol)} />
        <PriceStat label="Stop loss" value={formatPrice(signal.stop_loss, signal.symbol)} color={colors.sell} />
        <PriceStat label="Take profit" value={formatPrice(signal.take_profit, signal.symbol)} color={colors.buy} />
        <PriceStat label="Risk:Reward" value={`1 : ${rr.toFixed(1)}`} />
      </View>

      {signal.ai_reasoning && (
        <Section title="Reasoning">
          <Text style={styles.bodyText}>{signal.ai_reasoning}</Text>
        </Section>
      )}

      {signal.ai_risk_notes && (
        <Section title="Risk notes" accent={colors.amber}>
          <Text style={[styles.bodyText, { color: colors.amber }]}>{signal.ai_risk_notes}</Text>
        </Section>
      )}

      {smcSummary?.available && (
        <Section title="Market structure">
          <Text style={styles.bodyText}>
            Trend: <Text style={styles.bold}>{smcSummary.market_structure?.trend ?? 'unknown'}</Text>
          </Text>
          {smcSummary.order_blocks_recent && smcSummary.order_blocks_recent.length > 0 && (
            <Text style={styles.caption}>
              {smcSummary.order_blocks_recent.length} recent order block(s) identified
            </Text>
          )}
          {smcSummary.fair_value_gaps_recent && smcSummary.fair_value_gaps_recent.length > 0 && (
            <Text style={styles.caption}>
              {smcSummary.fair_value_gaps_recent.length} recent fair value gap(s) identified
            </Text>
          )}
        </Section>
      )}

      {toolCalls.length > 0 && (
        <Section title={`Evidence gathered (${toolCalls.length} tool calls)`}>
          {toolCalls.map((tc, i) => (
            <Text key={i} style={styles.caption}>
              • {tc.tool}
            </Text>
          ))}
        </Section>
      )}

      {signal.trigger_text && (
        <Section title="Trigger">
          <Text style={styles.caption}>{signal.trigger_text}</Text>
        </Section>
      )}

      {(signal.spread_at_entry != null || signal.commission != null || signal.swap != null) && (
        <Section title="Real costs">
          <View style={styles.costGrid}>
            {signal.spread_at_entry != null && (
              <CostStat label="Spread paid" value={formatSpread(signal.spread_at_entry, signal.symbol)} />
            )}
            {signal.lot_size != null && <CostStat label="Lot size" value={signal.lot_size.toFixed(2)} />}
            {signal.commission != null && (
              <CostStat label="Commission" value={formatCurrency(signal.commission)} color={colors.sell} />
            )}
            {signal.swap != null && (
              <CostStat label="Swap" value={formatCurrency(signal.swap)} color={signal.swap < 0 ? colors.sell : colors.buy} />
            )}
          </View>
          <Text style={styles.caption}>
            Real values from MT5 — spread from the actual order execution, commission/swap from MT5's deal history.
          </Text>
        </Section>
      )}

      {signal.status === 'closed' && signal.closed_price != null && (
        <Section title="Closed">
          <Text style={styles.bodyText}>
            {signal.closed_reason ?? 'Closed'} @ {formatPrice(signal.closed_price, signal.symbol)}
          </Text>
        </Section>
      )}

      {signal.rejection_reason && (
        <Section title="Rejection reason" accent={colors.sell}>
          <Text style={[styles.bodyText, { color: colors.sell }]}>{signal.rejection_reason}</Text>
        </Section>
      )}

      {canDecide && (
        <View style={styles.actionRow}>
          <View style={styles.actionButton}>
            <Button
              label="Reject"
              variant="secondary"
              onPress={() => reject()}
              loading={actionLoading === 'reject'}
              disabled={actionLoading !== null}
            />
          </View>
          <View style={styles.actionButton}>
            <Button
              label="Approve"
              variant="primary"
              onPress={handleApprove}
              loading={actionLoading === 'approve'}
              disabled={actionLoading !== null}
            />
          </View>
        </View>
      )}
    </ScrollView>
  );
}

function Section({ title, children, accent }: { title: string; children: React.ReactNode; accent?: string }) {
  return (
    <View style={styles.section}>
      <Text style={[styles.sectionTitle, accent && { color: accent }]}>{title}</Text>
      {children}
    </View>
  );
}

function PriceStat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <View style={styles.priceStat}>
      <Text style={styles.priceStatLabel}>{label}</Text>
      <Text style={[styles.priceStatValue, color && { color }]}>{value}</Text>
    </View>
  );
}

function CostStat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <View style={styles.costStat}>
      <Text style={styles.priceStatLabel}>{label}</Text>
      <Text style={[styles.costStatValue, color && { color }]}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.background },
  content: { padding: spacing.lg, gap: spacing.lg, paddingBottom: spacing.xxl },
  headerRow: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'flex-start' },
  symbol: { ...typography.displayLarge, color: colors.textPrimary },
  timeframe: { ...typography.caption, color: colors.textTertiary },
  badgeRow: { flexDirection: 'row', flexWrap: 'wrap', gap: spacing.sm },
  decisionPill: { paddingHorizontal: spacing.sm, paddingVertical: 4, borderRadius: radius.pill },
  decisionText: { ...typography.label },
  chartCard: {
    backgroundColor: colors.surface,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
    padding: spacing.lg,
    alignItems: 'center',
  },
  priceGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: spacing.md },
  priceStat: {
    flexBasis: '47%',
    flexGrow: 1,
    backgroundColor: colors.surface,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    padding: spacing.md,
    gap: 2,
  },
  priceStatLabel: { ...typography.label, color: colors.textTertiary },
  priceStatValue: { ...typography.mono, fontSize: 17, color: colors.textPrimary },
  costGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: spacing.md, marginBottom: spacing.xs },
  costStat: { flexBasis: '30%', flexGrow: 1, gap: 2 },
  costStatValue: { ...typography.bodyStrong, color: colors.textPrimary },
  section: { gap: spacing.xs },
  sectionTitle: { ...typography.label, color: colors.textTertiary },
  bodyText: { ...typography.body, color: colors.textPrimary },
  caption: { ...typography.caption, color: colors.textSecondary },
  bold: { fontWeight: '700', color: colors.textPrimary },
  actionRow: { flexDirection: 'row', gap: spacing.md, marginTop: spacing.md },
  actionButton: { flex: 1 },
});
