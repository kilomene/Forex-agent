# Telegram signal notifications — setup

Get every detected trade signal (and every critical alert) as a Telegram
message, seconds after the subsystem sees it. Signal messages look like:

```
SIGNAL DETECTED
Symbol: EURUSD M15
Direction: BUY
Entry: 1.08520
SL: 1.08300   TP: 1.08950
Strategy: ema_rsi
```

Three steps, about 5 minutes. Steps 1–2 are in the Telegram app on your
phone; step 3 is where the dispatcher runs.

## 1. Create the bot

1. Open Telegram and chat with **@BotFather**.
2. Send `/newbot`, pick a name (e.g. `Forex Signals`) and a username
   (must end in `bot`, e.g. `forex_signals_zenas_bot`).
3. BotFather replies with a **bot token** like
   `123456789:AAH...`. This token is a secret — treat it like a password.

## 2. Get your chat ID

1. In Telegram, send any message (e.g. `hi`) to your new bot.
2. In a browser, open:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   (replace `<YOUR_TOKEN>` with the token from step 1).
3. Find `"chat":{"id":123456789,...}` in the result — that number is
   your **chat ID**.

## 3. Enable the channel

On the machine running the notification dispatcher:

```bash
export NOTIFICATIONS_ENABLED=1
export NOTIFY_CHANNEL_TELEGRAM=1
export TELEGRAM_BOT_TOKEN='<your bot token>'   # secret — env only, never in files
export TELEGRAM_CHAT_ID='<your chat id>'
```

Then start (or restart) the dispatcher as usual. Check health
(secret-free — the token value is never shown):

```bash
scripts/forex notify --status
```

`telegram` should report `configured: true`.

## What you will receive

Severity routing (`NOTICE` and above reaches Telegram by default):

| Event | Severity | Message |
|---|---|---|
| `signal.detected` | NOTICE | the trade-signal card above |
| `trade.executed`, `trade.closed` | NOTICE | one-line summary |
| `risk.*`, kill switch | CRITICAL | one-line alert |
| `signal.approved` / `signal.rejected` | NOTICE | one-line summary |

The dispatcher polls the delivery journal every 5 seconds
(`NOTIFY_POLL_INTERVAL_SECONDS`), so messages arrive within seconds of
detection. A dead Telegram never blocks anything else: every failure is
isolated, logged without the token, and the agent channel keeps working.

## Notes

- The bot can only notify about signals the pipeline actually produces.
  The detector is the deterministic EMA/RSI `signal_monitor` daemon; it
  needs live market data (broker connected) to fire.
- Notifications are **advisory**. The bot never places trades — it only
  tells you what the detector found. Execution stays behind the gateway,
  risk checks, and (for live trading) your approval.
- To rotate the token: revoke it in @BotFather (`/revoke`), set the new
  `TELEGRAM_BOT_TOKEN`, restart the dispatcher.
