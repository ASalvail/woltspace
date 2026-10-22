# Isolated WhatsApp model-session test plan

**Status: implementation ready for review; not launched.** The separate
runner is `scripts/whatsapp_model_session_test.py`; it requires an explicit
`--run`, a selected `--harness` (`claude` or `codex`), a real TTY, and typed
confirmation. Do not launch it until Survey reviews that implementation.
Stage one and stage two in
`whatsapp-owner-test.md` do not create a real agent session. This plan covers a
later test of the complete inbound → session → model reply → notify → WhatsApp
outbound path.

## Isolation boundary

- Use a fresh directory under `.whatsapp-owner-test/` with mode `0700` for the
  test `WOLTS_DIR`, one synthetic `wa-test` wolt, session registry, logs, and
  temporary server state. Do not copy real wolt directories or their secrets.
- Run the source-checkout `server.app:app` in the runner process with its
  lifecycle disabled, bound only to `127.0.0.1` on a dynamically allocated
  port. Reserve and pass an already-bound socket so the selected port cannot
  race with another process. This keeps the real `/notify` route while skipping
  tunnel, connector, watcher, and lodge lifecycle startup. Set `WOLTSPACE_API`
  and `PORT` to that local endpoint; never bind port 7777 or change the lodge's
  running server.
- Set `WOLTSPACE_DIR` to the reviewed checkout and both wolt directory
  variables to the synthetic test root. Disable all channel connectors on the
  isolated server. Run the WhatsApp poller as the only WhatsApp API-key
  consumer.
- Give the poller the key from the reviewed private key file in process memory.
  Do not put it in argv, output, logs, or the session environment. For the
  server's notify sender, use an in-process secret lookup backed by that
  private file; do not duplicate the key into `.env` or a tracked artifact.
  Server output goes to a private `0600` temporary log that is removed during
  successful cleanup.
- Set `TMUX_TMPDIR` to a private directory inside the test root. Use an
  already owner-authorized harness login through the existing supported
  authentication path (for example, the configured `CODEX_HOME` or
  `CLAUDE_CONFIG_DIR` path carried by the session runtime). Do not copy,
  print, or inspect authentication files. The test runner should pass only the
  selected harness path through the existing session environment allowlist;
  it should not inherit unrelated owner environment variables. If no supported
  authorized harness login is configured, stop and report that prerequisite.
  The test starts a fresh tmux server there, removes inherited `TMUX` state,
  and verifies the socket path and WhatsApp-token environment before accepting
  the model reply.

## Single-message flow

After review, invoke it from this checkout with the harness whose existing
owner-authorized login is available:

```sh
.venv/bin/python scripts/whatsapp_model_session_test.py --run --harness claude
# or: .venv/bin/python scripts/whatsapp_model_session_test.py --run --harness codex
```

The command refuses non-interactive input and asks for the exact word `MODEL`
before reading the private WhatsApp key. It prompts for two WhatsApp messages:
the exact wolt selection and a generated nonce challenge.
The selected harness must already have an authorized login directory:
`CLAUDE_CONFIG_DIR` (or `~/.claude`) for Claude, or `CODEX_HOME` (or
`~/.codex`) for Codex. Its CLI and `tmux` must be available on `PATH`. The
runner sets a disposable `HOME` but passes that existing auth directory via
the supported harness environment variable; harness state outside that auth
directory is intentionally not copied.

The test allows one exact `🦦 wa-test: <nonce>` payload from the selected
participant after checking the session registry, account fingerprint, and
private tmux socket. If the model replies while the inbound handler is still
saving its route, the notify request waits asynchronously for at most 45
seconds for dispatch completion, then verifies the saved route and fails
closed if it is absent. The notification sender retains its bounded HTTP 429
handling (up to three attempts, honoring `Retry-After`). The runner does not
retry an ambiguous timeout or any other send failure; the attempt is reserved
before network I/O and all later notify attempts are rejected.

1. Create only the synthetic test wolt and start the isolated server and its
   private tmux server. Confirm the API endpoint is the selected loopback port
   and that it is not 7777.
2. Start the adapter with a fresh activation timestamp and explicit offset 0.
   The owner sends `wolt wa-test` and receives the adapter's selection
   confirmation. Then they send one unique harmless request asking the test
   wolt to reply with the same nonce and no other action. The runner accepts
   only these two exact inbound texts from that same participant; other
   messages are left undispatched and stop the test.
3. Verify the isolated session registry records the WhatsApp participant and
   account fingerprint; verify the new tmux process has no
   `WHATSAPP_AGENT_TOKEN`; verify the adapter's selection confirmation and
   one nonce reply sent through the real `server.notify` route.
4. Stop the exact test session, private tmux server, isolated API server, and
   poller. Remove the temporary data root and logs. Keep the credential file
   only until the owner runs the documented cleanup command. If cleanup cannot
   verify the private tmux socket, retain the private run directory with a
   `.cleanup-required` marker and report that cleanup needs attention.

## Stop conditions

Do not run this plan if an owner-authorized supported harness login is
unavailable, the isolated API endpoint cannot be confirmed, or the tmux socket
cannot be proven private. Never use a real owner task as the test prompt, and
do not retry an ambiguous WhatsApp send until the owner checks the chat.
