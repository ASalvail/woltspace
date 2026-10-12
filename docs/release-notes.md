# Woltspace 0.5.16

Python **0.5.16** lets wolts send you files and voice notes, and keeps the
lodge's secrets off process command lines and out of the apps it runs. The
separately distributed **@woltspace/tui remains at 0.5.2**; no npm release is
required.

## Files and voice notes from your wolts

A community contribution: thanks to [@asharakeh](https://github.com/asharakeh)
for building this, from the first idea to spoken replies (#519).

- **`notify --file PATH`** sends one file with the message, to the same chat
  or thread the message goes to, on Telegram or Slack (up to 50 MB). On
  Telegram, `.ogg`/`.oga`/`.opus` audio arrives as a voice note, `.mp3`/`.m4a`
  in Telegram's player, and anything else as a document.
- **`notify --speak`** has the lodge read the message aloud and send it as a
  voice note. It uses the `OPENAI_API_KEY` that already transcribes the voice
  notes you send (`gpt-4o-mini-tts`, voice `alloy`; change them with
  `OPENAI_TTS_MODEL` and `OPENAI_TTS_VOICE`). Each spoken message is one OpenAI
  API request, billed to that key, of at most 4096 characters.
- **`notify --voice PATH`** sends audio you already have as a voice note.
- **Replying works.** A reply to a file or a voice note the wolt sent reaches the
  session that sent it.
- **Files go only to the lodge's own chats:** the allowed Telegram users, the
  Slack notify channel, or the chat the session started from. Text is not
  limited this way.
- **Credentials are refused:** `.env` files, Claude and Codex logins, SSH keys,
  `.pem`/`.key`/`.p12`/`.pfx` files, `.netrc`, `.git-credentials`, `.pgpass`,
  AWS credentials, password vaults, and anything in the lodge's own `.space`
  folder. Spoken audio is capped at what the chat can carry while it downloads.
- **Slack** needs the `files:write` scope for files. The setup-slack skill's
  manifest includes it; an existing Slack app needs it added and reinstalled.

## Secrets stay off command lines and out of apps

On a machine with more than one user, every user can read every process's
command line with `ps`. A report from a Mac with two users, each running a
lodge, showed one lodge's Cloudflare tunnel token that way, and a Slack app
token in an app's process.

- **The tunnel token is no longer on cloudflared's command line.** The lodge
  hands it over in the `TUNNEL_TOKEN` environment variable. Same tunnel, same
  behaviour.
- **Apps no longer receive the lodge's service credentials.** Apps, their
  install step, and processes started through the tools route stop inheriting
  `TELEGRAM_*`, `SLACK_*`, `GITHUB_APP_*`, `CLOUDFLARE_*` and the owner's
  `CLAUDE_CODE_OAUTH_TOKEN`. Everything else still reaches them, model API keys
  included.
- **The cloudflare skill keeps tokens out of the session.** It writes a new
  tunnel token straight to `.env` instead of printing it, and its `curl` calls
  pass the API token as a config file instead of a command-line header. So do
  the wildcard-subdomain docs.
- **Wolts learn the rules.** The apps and new-app skills have a Secrets section,
  and new wolts get one rule: never print, log or commit a secret, never put one
  on a command line, and ask the human for a key instead of looking for one.

## Before you update

- **An app that used the lodge's Slack, Telegram, GitHub-app or Cloudflare
  token stops seeing it.** Give that app its own token in its own `.env`
  (`wolts/apps/<name>/.env`).
- **If other people can log in to your machine,** rotate your Cloudflare tunnel
  token after updating and restarting.
- **To send files to Slack,** add the `files:write` scope to your Slack app and
  reinstall it.

## Known limits

- Wolt sessions still inherit the whole lodge environment, so a wolt can read
  the lodge's secrets. The secrets service planned for 0.6 is the full fix.
- A wolt session's opening message is still passed on the engine's command
  line. A fix is planned.
- There is no spending limit on `--speak` yet beyond one request per call.

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
