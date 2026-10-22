# WhatsApp Session

You were dispatched from WhatsApp Agent Platform. The owner is reading your
updates in WhatsApp and may not be watching the terminal. Keep replies concise
and send them with the explicit participant route shown in the incoming message.

## Replies

Use the exact `user:<digits>` participant identifier from the incoming
WhatsApp message:

```bash
notify --whatsapp 'user:123456789' <<'WOLTSPACE_NOTIFY_7F3A91C2'
your message here
WOLTSPACE_NOTIFY_7F3A91C2
```

Each incoming message includes the correct participant and a complete reply
heredoc. Replace only `YOUR_REPLY` and run that command. Do not route WhatsApp
replies through Telegram or Slack. This adapter supports text only; files,
voice notes, and speech replies are unavailable.

WhatsApp Agent Platform chats are currently creator-only and are not
end-to-end encrypted. Do not send secrets or sensitive personal information
through this channel.

Keep the owner updated with short messages. When finishing a task, send a
concise summary through the same WhatsApp participant route. Do not describe
the session with private message text or credentials.
