/**
 * Deliberate design choices, not framework defaults:
 *   - Base is a deep charcoal-blue, not pure black — pure black on OLED
 *     reads as "empty," charcoal reads as "premium terminal."
 *   - Buy/sell use a muted teal/coral rather than saturated green/red —
 *     saturated green/red reads as "stock ticker cliché," muted versions
 *     read as considered and are easier on the eyes for something you'll
 *     glance at all day.
 *   - Accent is a warm amber, used sparingly for confidence/attention,
 *     not blue — blue is already used structurally (links, focus states
 *     in most RN defaults) so a second accent avoids visual competition.
 */

export const colors = {
  background: '#0F1419',
  surface: '#161D26',
  surfaceRaised: '#1D2530',
  border: '#26303C',

  textPrimary: '#EDF1F5',
  textSecondary: '#8B98A5',
  textTertiary: '#5B6673',

  buy: '#4FB89E',
  buyMuted: 'rgba(79, 184, 158, 0.14)',
  sell: '#E0705C',
  sellMuted: 'rgba(224, 112, 92, 0.14)',

  amber: '#D9A548',
  amberMuted: 'rgba(217, 165, 72, 0.14)',

  success: '#4FB89E',
  danger: '#E0705C',
  warning: '#D9A548',
  info: '#5B8FBD',

  statusPending: '#8B98A5',
  statusApproved: '#5B8FBD',
  statusExecuted: '#4FB89E',
  statusClosed: '#8B98A5',
  statusRejected: '#E0705C',
  statusSkipped: '#5B6673',
} as const;

export const spacing = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 24,
  xxl: 32,
} as const;

export const radius = {
  sm: 8,
  md: 12,
  lg: 16,
  pill: 999,
} as const;

export const typography = {
  displayLarge: { fontSize: 32, fontWeight: '700' as const, letterSpacing: -0.5 },
  headline: { fontSize: 20, fontWeight: '700' as const, letterSpacing: -0.3 },
  title: { fontSize: 17, fontWeight: '600' as const },
  body: { fontSize: 15, fontWeight: '400' as const, lineHeight: 21 },
  bodyStrong: { fontSize: 15, fontWeight: '600' as const },
  caption: { fontSize: 13, fontWeight: '400' as const, color: colors.textSecondary },
  label: { fontSize: 11, fontWeight: '700' as const, letterSpacing: 0.6 },
  mono: { fontSize: 15, fontWeight: '600' as const, fontVariant: ['tabular-nums' as const] },
  monoLarge: { fontSize: 22, fontWeight: '700' as const, fontVariant: ['tabular-nums' as const] },
};
