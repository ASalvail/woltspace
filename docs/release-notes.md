# Woltspace 0.5.17

Python **0.5.17** is a small fix release for 0.5. The separately distributed
**@woltspace/tui remains at 0.5.2**; no npm release is required.

## Fixes

- **Create a wolt keeps the model you chose.** Choosing a model on a card (say
  Fable on the Raccoon) and then using the engine menu, even to re-pick the
  engine already selected, put the card back to its usual model, so the wolt
  was created without the model you picked. The card now keeps your choice
  whenever the engine you pick offers it, and falls back to that engine's
  usual model when it doesn't. Reported and fixed by
  [@asharakeh](https://github.com/asharakeh) (#541, #542).

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
