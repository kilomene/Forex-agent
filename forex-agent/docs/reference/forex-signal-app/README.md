# forex-signal-app

The mobile app — stage 3 of 3. Real API integration against every route
`forex-signal-worker` exposes, no mocked data anywhere. Built with Expo +
expo-router, TypeScript throughout (the whole project type-checks clean —
`npx tsc --noEmit` passes with zero errors).

1. `forex-bot-bridge` — MT5 connection, signal engine, risk engine, exit management, SMC analysis, performance review ✅
2. `forex-signal-worker` — signal storage, the agent (tool-calling + reflection), push notifications ✅
3. **This app** — you are here

## Setup

```bash
npm install
npx expo start
```

You'll need a **physical Android device with a development build** for
full functionality — see the push notifications section below for why
Expo Go specifically won't work here.

On first launch, the app asks for your Worker's URL and the
`WORKER_API_KEY` you set when deploying `forex-signal-worker`. It tests
the connection before saving anything.

## Screens

| Screen | What it does |
|---|---|
| **Signals** | Live feed, filterable by status, pull-to-refresh, polls every 30s while foregrounded |
| **Charts** | Browse any watched symbol/timeframe — candlestick chart with EMA overlay, independent of any specific trade signal. See the honest staleness note below. |
| **Signal detail** | Full breakdown: real candlestick chart (OHLC + SMC zone overlay), confidence ring, reasoning, risk notes, market structure, real costs (spread/commission/swap), which tools the agent actually called, approve/reject |
| **History** | Every signal that's been decided one way or another |
| **Performance** | Real stats from MT5 deal history (win rate, profit factor, gross vs net P/L after real costs, P/L by symbol) + recent reflections |
| **Settings** | Kill switch (halt entries + emergency close-all), trading mode toggle (manual/autonomous), biometric lock, push notification setup + test push, connection management |

## The Charts tab — real charting, honestly scoped

This is what "full charting like MT5" actually became, and it's worth
being precise about the gap rather than let the name imply more than it
is. You can browse any symbol/timeframe the bridge is configured to
watch, with a real candlestick chart and real EMA overlay lines — not
just the snapshot bundled with one trade signal. What it is **not**:
live, tick-by-tick data the way the actual MT5 terminal shows it. The
whole system is poll-based (the bridge polls the Worker, the app polls
the Worker), and Charts follows that same pattern — data refreshes on a
schedule the bridge controls (`CHART_UPDATE_INTERVAL_SECONDS`, default
120s), and the screen shows the real "updated X ago" timestamp rather
than implying real-time. Building actual live streaming would mean
introducing a websocket layer (Cloudflare Durable Objects, most likely)
that doesn't exist anywhere else in this system — a real, larger
architecture change, not something this update includes.

Still missing versus a real MT5 terminal: drawing tools, more indicator
types, and manual trade placement from the chart — none of that exists
here, this is a read-only viewing surface.

## Reasoning engine visibility — Settings, above the model connection

A read-only status card showing which provider is actually generating
Zenas's reasoning right now, and — the part that actually matters —
whether the real multi-turn tool-calling agent loop is running for it, or
whether it's on the pre-fetched-context fallback path. This exists
because that distinction was previously invisible: switching providers
(e.g. Alibaba → OpenRouter) was a server-side-only change with no way to
confirm from the app whether the new provider actually got the full
agentic capability or silently fell back. Check this after changing
providers, or after your first real signal, rather than assuming.

Accounts for the self-hosted model override below it — if you've
configured a custom VM connection, this correctly shows `custom` (and
`uses_tool_calling: false`, since small self-hosted models mostly can't
do reliable multi-turn tool use), not whatever the Worker's static
`AI_PROVIDER` secret says.

## Self-hosted AI model connection — test-then-save, in Settings

A new "AI model (self-hosted)" section lets you point Zenas at your own
model server (e.g. a Qwen model on an always-on Alibaba Cloud VM) instead
of the Worker's default provider, without redeploying anything. Two
independent fields, each with their own test-then-save flow:

- **VM address** — tested against the server's `/health` endpoint before
  being saved. A wrong or unreachable address is never silently persisted.
- **Model API key** — tested with a real prompt against `/generate`
  (needs the VM address saved first, or entered in the same session) —
  confirms the model actually responds correctly, not just that the
  server is up. The input is cleared immediately after a successful save,
  so a saved secret never sits visible in a text field.

A "Disconnect" option reverts to the Worker's own deploy-time provider
configuration. See the Worker's README for the exact routes and the
precedence rule (an app-configured URL always wins over the Worker's
static `AI_PROVIDER` secret while it's set).

## The kill switch — the most prominent control in the app, on purpose

Its own card at the top of Settings, bordered in red, separate from every
other section — this is the control you need to find fastest under
stress, not one more toggle among many. Two distinct actions, not one:

- **Halt all new entries** — a switch. Confirms once before engaging
  (same "hard to flip by accident" principle as autonomous mode below).
  Stops new trades; does NOT touch positions already open.
- **Close all positions now** — a button, not a switch, and deliberately
  the highest-friction action in the entire app: a two-step confirmation
  where the second dialog requires reading a distinct warning, not just
  tapping the same one twice. This force-closes every real open position
  at current market price, bypassing every exit rule. That level of
  friction is intentional — this is the one action in the app that should
  never fire from a stray tap.

Both are backed by real Worker routes and a bridge-side fast-polling loop
(10s default) — see the bridge and Worker READMEs for the full mechanism,
including the defense-in-depth check in `risk.py` that blocks entries
even in a race between approval and engaging the switch.

## The autonomous-mode toggle — deliberately hard to flip by accident

Flipping trading mode to autonomous means the agent's own `"enter"`
decision gets auto-approved with no human tap. A plain `Switch` is too
easy to hit by accident for something this consequential, so it's gated
behind a native confirmation dialog explaining exactly what changes.
Turning it back off needs no confirmation — only enabling more autonomy
does.

## Push notifications — requires a real build, not Expo Go

The Worker sends pushes via FCM's HTTP v1 API directly
(`forex-signal-worker/src/fcm.js`), not through Expo's push service. That
means the app needs the device's **raw FCM token**
(`getDevicePushTokenAsync`), not an Expo push token. Raw FCM tokens
require a native Firebase config baked into the build — this **will not
work in Expo Go**, full stop. You need:

1. A `google-services.json` from the same Firebase project the Worker's
   `FCM_SERVICE_ACCOUNT_JSON`/`FCM_PROJECT_ID` secrets point at — drop it
   in the project root (it's gitignored, don't commit it).
2. An EAS build (`eas build --platform android --profile development` for
   testing, or let the CI workflow below handle production builds) —
   not `expo start` in Expo Go.

If you skip this, everything else in the app still works — signals,
approval, performance, settings — you just won't get push alerts, and
`registerForPushNotifications()` will fail gracefully with a clear reason
rather than crashing.

## Building — same CI-only pattern as your other apps

Matches ZAO/BITGEN/GitDeploy: Termux only ever runs `git push`, the real
build happens in GitHub Actions. `.github/workflows/eas-build.yml` runs
`tsc --noEmit` first (so a type error fails fast, before spending EAS
build minutes on broken code), then triggers an EAS cloud build. You'll
need:

- An Expo account, `eas.json` already configured for an APK production
  profile
- `EXPO_TOKEN` added as a GitHub Actions secret (from
  `expo.dev/accounts/[you]/settings/access-tokens`)
- `google-services.json` — EAS needs this too; either commit it to a
  private repo (acceptable since it's not a secret key, just a client
  config) or wire it through EAS's secret file mechanism

## Biometric lock — added given what this app can do

`useBiometricLock.ts` gates the whole app behind Face ID/fingerprint
(with the device passcode as `expo-local-authentication`'s automatic
fallback) when enabled in Settings. Re-locks on every foreground
transition, not just cold start — an app left unlocked in the background
would defeat the entire point. Off by default (consistent with every
other safety-relevant default in this system being opt-in, not opt-out),
toggleable in Settings, and gracefully disabled if no biometric hardware
is enrolled on the device.

## Real candlestick chart — not just the list-view sparkline

`CandlestickChart.tsx` renders actual OHLC wicks and bodies from the real
candles the bridge sends, with entry/SL/TP reference lines and an SMC
support/resistance zone overlay — the first place in the app you can
actually *see* what `smc_analysis.py` found, rather than just read it as
text. Kept separate from `PriceSparkline` (still used in list cards) —
full candle geometry in every list item would be visual noise; the detail
screen is where "let me actually look at this" belongs.

One real bug caught during development, worth knowing about: an early
version nested a React Native `<View>` inside the `<Svg>` tree to group a
wick and body together. This type-checks fine (View is valid JSX) but
fails at render time — `react-native-svg` requires SVG-specific
containers (`<G>`) for grouping, not RN views, since the whole tree
renders through a different native path than normal RN views. Fixed by
switching to `<G>`.

**A second, more significant bug found later, worth being direct about:**
this chart never actually had real data to render. `recent_candles` and
`smc_summary` were being sent by the bridge and used by the Worker's
agent within a single request, but were never persisted to D1 — so every
real `GET /signals/:id` the app made returned nothing for either field,
silently. The chart component's graceful `null` handling meant this never
crashed or looked obviously broken; it just quietly never showed
anything. Fixed on the Worker side (new JSON columns) and the app side
(`parseRecentCandles`/`parseSmcSummary` in `src/api/types.ts`, same
pattern as the existing `parseToolCalls`) — this is now genuinely wired
end-to-end, not just type-correct.

## Real cost tracking — spread, commission, swap

Signal detail now shows spread paid at entry, confirmed lot size, and
(once a trade closes) real commission and swap pulled from MT5's own
deal history — not estimates. The Performance screen distinguishes gross
P/L from net P/L after these real costs, plus total commission/swap paid
across the period. All of it comes from `forex-bot-bridge/cost_tracking.py`
and flows through the same D1 columns and parser pattern as everything
else — see the Worker's README for the full mechanism.

## Design decisions worth knowing about

- **Dark charcoal-blue base (`#0F1419`), not pure black** — pure black on
  OLED reads as "empty," this reads as a considered terminal aesthetic.
- **Muted teal/coral for buy/sell, not saturated green/red** — saturated
  colors read as stock-ticker cliché and are fatiguing for something
  glanced at all day; muted versions read as deliberate.
- **Confidence shown as a ring, not a bar** — a progress bar borrows
  "download in progress" framing that doesn't fit; a ring reading "how
  much of the full circle of certainty this fills" fits better.
- **Price sparkline uses the REAL candles** the bridge sends with each
  signal (`recent_candles` from `signal_engine.py`) — not a placeholder
  chart. It's deliberately just a closes-only line, not full OHLC wicks —
  a glance-level "which way has it moved" indicator, not a trading chart.
  A real candlestick chart screen would be a reasonable future addition,
  not squeezed into a card.

## What's NOT built yet

- **No real-time push while backgrounded beyond what FCM delivers** — the
  30-second foreground poll is a freshness backstop, not a replacement
  for push.
- **No multi-account support** — one Worker connection per install, which
  matches the single-user architecture of the whole system. Deliberately
  not built even when the rest of this round's work was, since adding it
  would contradict that architecture rather than complete it.
