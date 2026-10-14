# Woltspace 0.5.18

Python **0.5.18** is a small fix release for 0.5. The separately distributed
**@woltspace/tui remains at 0.5.2**; no npm release is required.

## Fixes

- **A new wolt's first page shows up instead of "not found".** Wolts often
  showed a page with `push-view page.html`; the viewport looked for it at the
  lodge root and showed "not found" until someone pointed it out. `push-view`
  now turns `page.html`, `wolt/site/page.html` or the file's path on disk into
  `/wolt/<name>/site/page.html`, and `push-view --help` prints its usage
  instead of showing a page called `--help` (#551).
- **`push-view` checks the page loads before showing it.** A page that is not
  there is not shown; the wolt gets an error naming the right path instead.
  The check and the push give up after 5 seconds, and say so plainly when the
  lodge cannot be reached (#551, #552).

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
