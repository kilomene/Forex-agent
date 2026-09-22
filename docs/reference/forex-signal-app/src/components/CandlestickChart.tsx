import { StyleSheet, Text, View } from 'react-native';
import Svg, { G, Line, Polyline, Rect, Text as SvgText } from 'react-native-svg';
import { colors, spacing, typography } from '@/theme/tokens';
import type { OhlcCandle, SmcSummary } from '@/api/types';

/**
 * A real candlestick chart — actual OHLC wicks and bodies, not just a
 * closes-only line like the sparkline used in list views. Optionally
 * overlays SMC zones (support/resistance, order blocks) as shaded
 * rectangles, since the whole point of building smc_analysis.py was to
 * eventually let you SEE the structure it found, not just read it as text.
 *
 * Kept as its own component rather than replacing PriceSparkline — the
 * sparkline's job is a glance-level list-item indicator, this one is a
 * deliberate "let me actually look at this" surface, and cramming full
 * candle geometry into every list card would be visual noise.
 */
export function CandlestickChart({
  candles,
  smcSummary,
  entryPrice,
  stopLoss,
  takeProfit,
  emaFast,
  emaSlow,
  width = 340,
  height = 220,
}: {
  candles: OhlcCandle[];
  smcSummary?: SmcSummary | null;
  entryPrice?: number;
  stopLoss?: number;
  takeProfit?: number;
  /** Same length as candles, null-padded before the EMA warms up — from chart_data.py/build_chart_payload, not computed client-side. */
  emaFast?: (number | null)[] | null;
  emaSlow?: (number | null)[] | null;
  width?: number;
  height?: number;
}) {
  if (!candles || candles.length < 2) return null;

  const padding = { top: 16, bottom: 16, left: 8, right: 48 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;

  const allPrices = candles.flatMap((c) => [c.high, c.low]);
  const relevantLevels = [entryPrice, stopLoss, takeProfit].filter(
    (p): p is number => typeof p === 'number'
  );
  const min = Math.min(...allPrices, ...relevantLevels);
  const max = Math.max(...allPrices, ...relevantLevels);
  const range = max - min || 1;
  const rangePadding = range * 0.08; // breathing room so wicks don't touch the edge

  const priceToY = (price: number) =>
    padding.top + (1 - (price - (min - rangePadding)) / (range + rangePadding * 2)) * chartHeight;

  const candleSlotWidth = chartWidth / candles.length;
  const bodyWidth = Math.max(candleSlotWidth * 0.6, 2);

  return (
    <View>
      <Svg width={width} height={height}>
        {/* Price reference lines: entry (neutral), stop (red), target (green) */}
        {entryPrice != null && (
          <ReferenceLine y={priceToY(entryPrice)} width={chartWidth} label="Entry" price={entryPrice} color={colors.textTertiary} />
        )}
        {stopLoss != null && (
          <ReferenceLine y={priceToY(stopLoss)} width={chartWidth} label="SL" price={stopLoss} color={colors.sell} />
        )}
        {takeProfit != null && (
          <ReferenceLine y={priceToY(takeProfit)} width={chartWidth} label="TP" price={takeProfit} color={colors.buy} />
        )}

        {/* SMC support/resistance zones as shaded bands */}
        {smcSummary?.support_resistance_near_price?.map((zone, i) => {
          const yTop = priceToY(zone.price_high);
          const yBottom = priceToY(zone.price_low);
          return (
            <Rect
              key={`sr-${i}`}
              x={padding.left}
              y={yTop}
              width={chartWidth}
              height={Math.max(yBottom - yTop, 1)}
              fill={zone.type === 'resistance' ? colors.sellMuted : colors.buyMuted}
            />
          );
        })}

        {/* Candlesticks */}
        {candles.map((candle, i) => {
          const x = padding.left + i * candleSlotWidth + candleSlotWidth / 2;
          const isUp = candle.close >= candle.open;
          const color = isUp ? colors.buy : colors.sell;
          const bodyTop = priceToY(Math.max(candle.open, candle.close));
          const bodyBottom = priceToY(Math.min(candle.open, candle.close));

          return (
            <G key={i}>
              <Line x1={x} y1={priceToY(candle.high)} x2={x} y2={priceToY(candle.low)} stroke={color} strokeWidth={1} />
              <Rect
                x={x - bodyWidth / 2}
                y={bodyTop}
                width={bodyWidth}
                height={Math.max(bodyBottom - bodyTop, 1)}
                fill={color}
              />
            </G>
          );
        })}

        {/* EMA overlays — real values from chart_data.py, drawn on top of the candles so they're actually visible */}
        {emaFast && (
          <Polyline
            points={buildEmaPoints(emaFast, priceToY, candleSlotWidth, padding.left)}
            fill="none"
            stroke={colors.amber}
            strokeWidth={1.5}
          />
        )}
        {emaSlow && (
          <Polyline
            points={buildEmaPoints(emaSlow, priceToY, candleSlotWidth, padding.left)}
            fill="none"
            stroke={colors.info}
            strokeWidth={1.5}
          />
        )}
      </Svg>
      {smcSummary?.market_structure && (
        <Text style={styles.structureLabel}>
          Structure: <Text style={styles.structureValue}>{smcSummary.market_structure.trend}</Text>
        </Text>
      )}
    </View>
  );
}

function buildEmaPoints(
  series: (number | null)[],
  priceToY: (price: number) => number,
  candleSlotWidth: number,
  paddingLeft: number
): string {
  // EMA is null-padded only as a contiguous prefix before it warms up —
  // never null again afterward — so skipping nulls just means starting
  // the line partway through, not leaving gaps in the middle of it.
  return series
    .map((value, i) => {
      if (value == null) return null;
      const x = paddingLeft + i * candleSlotWidth + candleSlotWidth / 2;
      return `${x},${priceToY(value)}`;
    })
    .filter((p): p is string => p !== null)
    .join(' ');
}

function ReferenceLine({
  y,
  width,
  label,
  price,
  color,
}: {
  y: number;
  width: number;
  label: string;
  price: number;
  color: string;
}) {
  return (
    <>
      <Line x1={8} y1={y} x2={8 + width} y2={y} stroke={color} strokeWidth={1} strokeDasharray="4,3" opacity={0.6} />
      <SvgText x={8 + width + 4} y={y + 3} fontSize={9} fill={color}>
        {label}
      </SvgText>
    </>
  );
}

const styles = StyleSheet.create({
  structureLabel: {
    ...typography.caption,
    color: colors.textTertiary,
    marginTop: spacing.xs,
  },
  structureValue: {
    color: colors.textSecondary,
    fontWeight: '700',
  },
});
