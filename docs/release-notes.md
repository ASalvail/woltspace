# Woltspace 0.5.19

Python **0.5.19** is a small fix release for 0.5. The separately distributed
**@woltspace/tui remains at 0.5.2**; no npm release is required.

## Fixes

- **A message sent while a session is starting is no longer lost.** A
  Telegram message that started a new session, or one sent right after
  `/new`, could vanish while the lodge logged it as delivered: the agent
  ignores pasted input for its first moments. The message that starts a
  session now goes in with the session's opening prompt. Messages to a
  session that is still starting (Telegram, inter-wolt, `/message`) wait
  until the agent is ready before they are pasted, for up to 15 seconds. If
  it is still not ready, nothing is pasted, nothing is reported as
  delivered, and `/message` answers 409 `agent-booting`. Reported and fixed
  by [@asharakeh](https://github.com/asharakeh) (#555, #556).

## Upgrade notes

```sh
woltspace update
```

No colony-data migration is required. Native upgrades stop and restart the
control plane while preserving tmux sessions. Container lodges update from the
host as before.

After upgrading, verify the lodge is healthy:

```sh
woltspace status
```
