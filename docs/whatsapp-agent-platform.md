# WhatsApp Agent Platform connector

The opt-in connector uses Meta's official WhatsApp Agent Platform API at
`https://api.whatsapp.com/agent/v1`. It does not use WhatsApp Business Cloud
API or linked-device automation. The platform is currently creator-only and
messages are not end-to-end encrypted; use it only if those account limits fit.

Create an agent in WhatsApp **Settings > Agents > Create an agent**, open its
chat, and copy its API key from **Chat info > API key**. Configure the key in
the lodge's private `.space/platform/config.json`:

```json
{
  "channels": {
    "whatsapp": {
      "enabled": true,
      "agent_token": "<Agent API key>"
    }
  }
}
```

Or use `ENABLE_WHATSAPP_AGENT=true` and `WHATSAPP_AGENT_TOKEN` in the private
environment. Never put the key in a prompt, log, repository, or public artifact.
Start the lodge through `woltspace start`. The connector runs only in the
platform entrypoint and takes an exclusive local poll lock per API key.

Send `wolt NAME` to choose the destination wolt, then send text. Replies from
that session return to the same recent WhatsApp participant. Send `wolt NAME`
again to switch destinations. Only plain text is supported; inbound media gets
an explanatory response and outbound files, voice, and speech are unsupported.

The first poll uses explicit `offset=0`, with a persisted activation time to
ignore older retained messages. The `next_offset` cursor is persisted; HTTP 204
keeps the same cursor because it has no body. Message IDs are also persisted
before dispatch. There is an unavoidable crash window: a process can persist an
offset or ID and stop before a session receives the message. Recovery therefore
does not claim exactly-once dispatch. The account token's one-way fingerprint
names cursor and routing files, so changing keys does not inherit earlier
participant ownership. The API caps updates polling at 15 requests/minute and
message sends at 12 requests/minute. Explicit HTTP 429 refusals receive a
bounded retry using `Retry-After`; timeouts and other ambiguous send failures
are not retried automatically to avoid duplicate replies.

Mocked tests exercise cursor and sender behavior. No live credentials or account
changes are part of development.

References: [WhatsApp Agent Platform Developer Manual, v1 (Aug. 25, 2026)](https://www.whatsapp.com/developer/WhatsApp-Agent-Platform-Developer-Manual.pdf)
