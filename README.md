# Plaud2Claude

Every new recording from your Plaud voice recorder becomes its own **Claude session**: the transcript and Plaud's
own summary go to a Claude Code *scheduled task* on your Claude account, which writes the meeting notes. You read
them, and keep chatting about the meeting, at claude.ai/code or in the Claude app's Code tab.

It runs on your Claude subscription (Pro, Max, Team or Enterprise). No model API key is needed. Claude does the
writing.

Plaud2Claude is built on [OpenPlaud](https://github.com/AES256Afro/OpenPlaud)'s image and shares its Plaud sign-in
and Claude sender. Use OpenPlaud if you also want notes kept as files, folders, and a choice of models. OpenPlaud
can send to Claude too.

## Run

```bash
docker run -d -p 8796:8796 -v plaud2claude:/data \
  -e P2C_PASSWORD=choose-one \
  -e CLAUDE_ROUTINE_URL=https://api.anthropic.com/v1/claude_code/routines/trig_.../fire \
  -e CLAUDE_ROUTINE_TOKEN=sk-ant-oat01-... \
  ghcr.io/aes256afro/plaud2claude:0.1.0
```

It's also in the BoxPilot App Catalog.

1. Open `http://<server>:8796`, enter the password, and choose **Start Plaud sign-in**. Approve on Plaud's page.
   Your browser lands on a `localhost:8199` address that doesn't load. Paste that address into the page within two
   minutes.
2. At [claude.ai/code/routines](https://claude.ai/code/routines), create a scheduled task with the prompt shown under
   **Set up Claude** on the page. Add an **API** trigger, then copy its Fire URL and generate a token.
3. Put the Fire URL and token in the settings.

From then on, each new recording is sent once its transcript is ready, waiting up to `P2C_WAIT_FOR_SUMMARY` minutes
for Plaud's summary.

## Settings

| Variable | What it does | Default |
| --- | --- | --- |
| `CLAUDE_ROUTINE_URL` | Fire URL of the scheduled task's API trigger (or just its `trig_…` id) | |
| `CLAUDE_ROUTINE_TOKEN` | That trigger's token | |
| `P2C_PASSWORD` | Password for the page | |
| `P2C_SYNC_EVERY` | Minutes between checks for new recordings; `0` turns it off | `30` |
| `P2C_SYNC_DAYS` | How far back each check looks | `3` |
| `P2C_WAIT_FOR_SUMMARY` | Minutes to wait for Plaud's own summary before sending without it | `30` |
| `P2C_LOOK` | Page style: any OpenPlaud look (`console`, `inbox`, `phone`…) | `console` |

## Good to know

- **Only new recordings:** recordings made after the Claude settings are first saved are sent automatically.
  **Send now** sends any recording.
- **Limits:** Claude allows 30 sends an hour per scheduled task and 100 per account. A burst waits for the next
  check.
- **Long transcripts:** the message is cut to 65,536 characters, at a line, with a note and the Plaud file ID, so
  the task can fetch the rest through the Plaud connector.
- **No duplicates:** each send starts a new session, and Claude's endpoint has no way to tell a repeat. So a send
  that times out is never repeated automatically. Rate limits and overloads are retried on the next check; anything
  else waits for you.
- **Your token stays with Anthropic:** the token is sent only to `api.anthropic.com`'s fire endpoint. Plaud2Claude
  keeps a record of what it sent in `/data/plaud2claude.json`, and never deletes anything.
