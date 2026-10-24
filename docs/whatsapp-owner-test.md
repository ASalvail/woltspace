# Owner-operated WhatsApp test

This checkout includes an opt-in test for the official WhatsApp Agent Platform.
It has two isolated stages. Stage one checks real WhatsApp polling and sending
with a mocked session handler. Stage two sends one inbound test message through
the real session registry and `server.notify` sender, but still uses no live
Woltspace server or model session.

## Requirements and commands

Use Python 3.11 or newer. The runner uses the checkout's Python sources and
standard library for stage one. Stage two needs the normal Woltspace runtime
dependencies; run it with the checkout-local `.venv` after `uv sync --extra test`.
Both stages require network access to `api.whatsapp.com` after you enter a key.

From the checkout root, start stage one:

```bash
python scripts/whatsapp_owner_test.py
```

The script requires a controlling terminal at `/dev/tty`, asks for explicit
confirmation, then uses `getpass` so key entry is hidden. It writes the key
only to `.whatsapp-owner-test/agent-key` (directory mode `0700`, file mode
`0600`). That directory is gitignored. The key is not placed in argv,
environment variables, output, logs, or shell history. The runner reads it
inside a child process and holds it only in process memory while the test runs.
It does not start a Wolt session, agent, tmux server, or lodge server.

After the isolated poll starts, send `wolt wa-test` to the WhatsApp Agent and
wait for the selection confirmation. That is one expected outbound message.
Then send the one-time `wa-check-…` phrase printed by the local runner. The
single test message is dispatched to the mocked session handler, which returns
one fixed reply through the real WhatsApp Agent API. The runner prints no
participant ID or inbound message text. It stops after that reply, on timeout,
or when you press Ctrl+C. Stage one proves the Agent API transport and adapter
routing with mocked session dispatch; it does not prove real model execution or
`server.notify` delivery.

For stage two, first complete stage one so the key is stored. Then run:

```bash
.venv/bin/python scripts/whatsapp_owner_test.py --notify-path
```

After the confirmation prompt, send the exact one-time phrase shown by the
runner in WhatsApp. The runner creates a synthetic session record in private
temporary state, then invokes the real `server.notify.send_notification` path
and WhatsApp sender for one canned reply. It tests registry/account binding,
notify routing, rate limiting, and the official send API. It still does not
launch a model session or touch the installed server or port 7777.

The API client may perform bounded retries after an explicit HTTP 429, using
the server's `Retry-After` value. It does not retry an ambiguous send timeout.
If a send times out, check WhatsApp before running another test because the
outcome may be ambiguous. Each one-time phrase reserves a single outbound
attempt before the send starts, even if a duplicate update arrives in the same
poll batch.

## Cleanup

The temporary state is removed when each stage exits. The credential remains
until you remove the private test directory with:

```bash
python scripts/whatsapp_owner_test.py --cleanup
```

This removes the saved key and any remaining private test files. Do not copy
the key into chat or a command line. If the checkout is deleted, the private
gitignored directory is deleted with it.

## Later real model-session test

A true model-session test should use a separate temporary Woltspace data root
and a separate server on a dynamically selected non-7777 port. It should create
one synthetic test wolt/session, point the session process only at that
isolated server, and verify the real WhatsApp inbound → registry → model reply
→ notify → WhatsApp outbound flow. It must verify tmux environment scrubbing,
restrict the agent prompt to a fixed harmless task, and clean up the test
session and temporary data root afterward. Do not launch that stage until the
exact server command, port, session lifetime, and runtime cleanup are reviewed.
## Local owner-tool acceptance (review before launch)

The optional stdio owner tool has a separate bounded diagnostic that uses the
supported platform Codex session path and only a local mocked sender:

```bash
.venv/bin/python scripts/whatsapp_startup_inspection.py --run --harness codex --timeout 120 --directory-trust normal --notify-owner-tool --capture-snapshot
```

Review this candidate before executing that command. It does not read the
saved WhatsApp key, poll WhatsApp, send provider messages or ask for a human
WhatsApp challenge. It reuses the existing owner-authorized harness login
path through the private per-run Codex setup, without exposing auth contents.
The worker/config receive no provider key; a public synthetic token exists
only in the mocked server lookup. The whole outbound sender boundary is
replaced, so Telegram/Slack/WhatsApp cannot be reached by a fallback.

The runner enables the owner tool only in its private environment. Its fixed
synthetic prompt asks for exactly one `notify_owner` call. Success requires
the exact identity-prefixed nonce, verified session/runtime/account route,
one `/notify/owner` request and zero legacy `/notify` requests. No dialogs are
answered. A specific approval/trust prompt fails the diagnostic; broad echoed
instruction text alone is not evidence of a displayed prompt. Configuration
parser success and process presence are not runtime no-prompt acceptance.

The 120-second observation budget begins after session/runtime verification.
Existing private server/tmux/session cleanup runs on success, error or Ctrl-C;
the installed lodge and port 7777 are preserved. A redacted snapshot and fixed
failure record may be retained privately after verified cleanup, following
the existing strict validation/retention rules. Never inspect auth/key files
or dump raw logs to diagnose this test. A failure needs evidence and review,
not an automatic second model or provider run. This diagnostic mode has
offline coverage; its real model execution remains a separate review step.
