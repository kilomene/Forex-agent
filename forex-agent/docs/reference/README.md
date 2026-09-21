# Reference — legacy mobile app

`forex-signal-app/` is a verbatim, read-only copy of the original Expo/React Native
mobile application. It is **legacy reference only**:

- It is NOT part of the forex-agent subsystem.
- NOTHING in the subsystem may depend on it (no test, daemon, tool, or
  installer step may require it).
- It may return one day as an optional remote dashboard, or serve as UI
  reference (confirm-friction patterns on destructive actions, the
  `CandlestickChart` SVG overlay, the FCM push registration flow).

Classification: OPTIONAL (see MIGRATION.md).
