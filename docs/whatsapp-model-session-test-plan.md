# Isolated WhatsApp model-session test plan

**Status: plan only; not launched.** Stage one and stage two in
`whatsapp-owner-test.md` do not create a real agent session. This plan covers a
later test of the complete inbound → session → model reply → notify → WhatsApp
outbound path.

## Isolation boundary

- Use a fresh directory under `.whatsapp-owner-test/` with mode `0700` for the
  test `WOLTS_DIR`, one synthetic `wa-test` wolt, session registry, logs, and
  temporary server state. Do not copy real wolt directories or their secrets.
- Run a source-checkout Woltspace server bound only to `127.0.0.1` on a
  dynamically allocated port. Reserve and pass an already-bound socket so the
  selected port cannot race with another process. Set `WOLTSPACE_API` and
  `PORT` to that local endpoint; never bind port 7777 or change the lodge's
  running server.
- Set `WOLTSPACE_DIR` to the reviewed checkout and both wolt directory
  variables to the synthetic test root. Disable all channel connectors on the
  isolated server. Run the WhatsApp poller as the only WhatsApp API-key
  consumer.
- Give the poller the key from the reviewed private key file in process memory.
  Do not put it in argv, output, logs, or the session environment. For the
  server's notify sender, run the isolated server in the test orchestrator
  process with an in-process secret lookup backed by that private file; do not
  duplicate the key into `.env` or a tracked artifact.
- Set `TMUX_TMPDIR` to a private directory inside the test root. Use an
  already owner-authorized harness login through the existing supported
  authentication path (for example, the configured `CODEX_HOME` or
  `CLAUDE_CONFIG_DIR` path carried by the session runtime). Do not copy,
  print, or inspect authentication files. The test runner should pass only the
  selected harness path through the existing session environment allowlist;
  it should not inherit unrelated owner environment variables. If no supported
  authorized harness login is configured, stop and report that prerequisite.

## Single-message flow

1. Create only the synthetic test wolt and start the isolated server and its
   private tmux server. Confirm the API endpoint is the selected loopback port
   and that it is not 7777.
2. Start the adapter with a fresh activation timestamp and explicit offset 0.
   The owner sends `wolt wa-test`, then one unique harmless request asking the
   test wolt to reply with the same nonce and no other action.
3. Verify the isolated session registry records the WhatsApp participant and
   account fingerprint; verify the new tmux process has no
   `WHATSAPP_AGENT_TOKEN`; verify the expected selection confirmation and one
   nonce reply through the real `server.notify` route.
4. Stop the exact test session, private tmux server, isolated API server, and
   poller. Remove the temporary data root and logs. Keep the credential file
   only until the owner runs the documented cleanup command.

## Stop conditions

Do not run this plan if an owner-authorized supported harness login is
unavailable, the isolated API endpoint cannot be confirmed, or the tmux socket
cannot be proven private. Stop after stage two's real registry/notify path
instead. Never use a real owner task as the test prompt, and do not retry an
ambiguous WhatsApp send until the owner checks the chat.
