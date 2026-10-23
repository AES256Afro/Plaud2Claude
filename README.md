# Plaud2Claude

Every new recording from your Plaud voice recorder becomes its own **Claude session**: the transcript and Plaud's
own summary go to a Claude Code *scheduled task* on your Claude account, which writes the meeting notes. You read
them, and keep chatting about the meeting, at claude.ai/code or in the Claude app's Code tab.

It runs on your Claude subscription (Pro, Max, Team or Enterprise). No model API key is needed. Claude does the
writing.

Plaud2Claude is built on [OpenPlaud](https://github.com/AES256Afro/OpenPlaud)'s image and shares its Plaud sign-in
and Claude sender. Use OpenPlaud if you also want notes kept as files, folders, and a choice of models. OpenPlaud
can send to Claude too.

Recordings can also skip Plaud's cloud: a collector beside the recorder (a PC it pairs with over Bluetooth, transcribing
with local Whisper) pushes its transcript to Plaud2Claude, which sends it to Claude the same way. See **Local recordings**.

## Run

```bash
docker run -d -p 8796:8796 -v plaud2claude:/data \
  -e P2C_PASSWORD=choose-one \
  -e CLAUDE_ROUTINE_URL=https://api.anthropic.com/v1/claude_code/routines/trig_.../fire \
  -e CLAUDE_ROUTINE_TOKEN=sk-ant-oat01-... \
  ghcr.io/aes256afro/plaud2claude:0.4.0
```

It's also in the BoxPilot App Catalog.

1. Open `http://<server>:8796`, enter the password, and choose **Start Plaud sign-in**. Approve on Plaud's page.
   Your browser lands on a `localhost:8199` address that doesn't load. Paste that address into the page within two
   minutes.
2. At [claude.ai/code/routines](https://claude.ai/code/routines), create a scheduled task with the prompt shown under
   **Set up Claude** on the page. Add an **API** trigger, then copy its Fire URL and generate a token.
3. In BoxPilot, open Plaud2Claude › Settings and paste them into **Claude fire URL** and **Claude token**.

From then on, each new recording is sent once its transcript is ready, waiting up to `P2C_WAIT_FOR_SUMMARY` minutes
for Plaud's summary.

## Settings

| Variable | What it does | Default |
| --- | --- | --- |
| `CLAUDE_ROUTINE_URL` | Fire URL of the scheduled task's API trigger (or just its `trig_…` id) | |
| `CLAUDE_ROUTINE_TOKEN` | That trigger's token | |
| `P2C_PASSWORD` | Password for the page (recommended: without one, anyone who can reach the page can send recordings) | |
| `P2C_CHECK_TOKEN` | Secret for the check trigger, `POST /api/check` (see below); empty turns the trigger off | |
| `P2C_INGEST_TOKEN` | Secret a collector uses to push local transcripts, `POST /api/recordings` (see **Local recordings**); empty turns that endpoint off | |
| `P2C_CHECK_ON_OPEN` | Opening the page checks Plaud when the last check is older than this many minutes; `0` turns it off | `5` |
| `P2C_ALLOWED_HOSTS` | Extra host names the page answers to, comma-separated. IP addresses, `localhost`, `*.local`, single-label names and `*.ts.net` always work. | |
| `P2C_SYNC_EVERY` | Minutes between checks for new recordings; `0` turns it off | `30` |
| `P2C_SYNC_DAYS` | How far back each check looks | `3` |
| `P2C_WAIT_FOR_SUMMARY` | Minutes to wait for Plaud's own summary before sending without it | `30` |
| `P2C_LOOK` | Page style: any OpenPlaud look (`console`, `inbox`, `phone`…) | `console` |

## Checking for new recordings right away

Besides the schedule (30 minutes by default), a check starts when:

- you press **Check now** on the page;
- you open the page and the last check is more than 5 minutes old (`P2C_CHECK_ON_OPEN`, minutes; `0` turns it off);
- a recent recording is still waiting on Plaud for its transcript or summary: the app checks again every 3 minutes for up to an hour, so it shows up soon after Plaud finishes;
- something calls the **check trigger**: `POST /api/check` with the header `Authorization: Bearer <P2C_CHECK_TOKEN>`. It needs no page password, only that token (16 characters or more; BoxPilot generates one, shown under the app's Secrets).

An iPhone Shortcut that checks after a meeting: add **Get Contents of URL**, set the URL to
`https://<your-box>.ts.net:8796/api/check`, Method **POST**, and a header `Authorization` = `Bearer <your check token>`.
Run it from the Home Screen, the Action button or an automation (for example, when the Plaud app closes).
From a computer: `curl -X POST -H "Authorization: Bearer $TOKEN" https://<your-box>.ts.net:8796/api/check`.

## Local recordings

A recording doesn't have to come from Plaud's cloud. A **collector** beside the recorder (for example the PC your Plaud
pairs with over Bluetooth, pulling recordings and transcribing them with local Whisper) can push the transcript it
made, and Plaud2Claude sends it to Claude exactly like a recording fetched from Plaud: the same payload, the same hourly
budget, the same page.

Set `P2C_INGEST_TOKEN` (16 characters or more; BoxPilot generates one, shown under the app's Secrets). The collector then
posts JSON to `POST /api/recordings` with the header `Authorization: Bearer <P2C_INGEST_TOKEN>`. No page password or
cookie is needed, only the token. Without the token set, the endpoint doesn't exist (404).

| Field | | |
| --- | --- | --- |
| `id` | required | 1 to 80 letters, digits, `_` or `-`. Pushing the same id again replaces the saved copy. |
| `transcript` | required | the transcript as text |
| `name` | optional | the title Claude sees; the id when missing |
| `recorded_at` | optional | ISO 8601 with a time zone, like `2026-10-22T14:03:00-04:00` (a time without one is read as UTC, like Plaud's) |
| `duration_seconds` | optional | number |
| `segments` | optional | `[{"start": 0.0, "end": 2.5, "text": "…"}, …]` in seconds. When given, Claude reads timestamped lines built from them, like Plaud's own transcripts; otherwise `transcript` as it came. |
| `source` | optional | who made it, like `plaude on chris-pc`; shown on the page and told to Claude |
| `model` | optional | like `whisper small` |
| `again` | optional | `true` sends a recording that is already in Claude once more (a second session). Default `false`. |

Answers: `202 {"queued": true}` (the send runs in the background; `"already_queued": true` means the same id was still
waiting, and the new copy is what gets sent), `200 {"status": "already_sent", "session_url": "…"}` when that id is in
Claude already and `again` is false, `400 {"error": "…"}` for a bad body, `403` for a wrong or missing token, `413` over
4 MB.

What happens next:

- The body is kept as `/data/local/<id>.json`. The recording is listed on the page with a **Local** marker, and **Send
  now** / **Send again** work for it from that file; Plaud is never asked about it.
- The "recordings made after setup" rule doesn't apply: a collector pushes on purpose, backfills included. A recording
  already in Claude is still not sent twice unless `again` is true (or its last send was a rate limit, which is retried).
- Claude's payload carries a *Local transcript* note in place of Plaud's summary ("Transcribed by *source* with *model*.
  Plaud's cloud was not used…"), so nothing waits for a summary and the task knows the Plaud connector can't fetch this one.

From a computer:

```bash
curl -sS -X POST "https://<your-box>.ts.net:8796/api/recordings" \
  -H "Authorization: Bearer $P2C_INGEST_TOKEN" -H "Content-Type: application/json" \
  -d '{"id": "2026-10-22-standup", "name": "Standup", "recorded_at": "2026-10-22T14:03:00-04:00",
       "duration_seconds": 61.5, "source": "plaude on chris-pc", "model": "whisper small",
       "transcript": "Line one.\nLine two."}'
```

From a transcript JSON as [OpenPlaudit](https://github.com/mar-maradnus/openplaudit) writes it
(`{file, duration_seconds, model, language, segments, text}`): `file` becomes `id` (extension dropped, other characters
replaced), `text` becomes `transcript`, and `duration_seconds`, `model` and `segments` carry over as they are:

```bash
jq '{id: (.file | sub("\\.[^.]+$"; "") | gsub("[^A-Za-z0-9_-]"; "-") | .[0:80]), name: .file, transcript: .text,
     duration_seconds, model, segments, source: "plaude on chris-pc"}' transcript.json \
  | curl -sS -X POST "https://<your-box>.ts.net:8796/api/recordings" \
      -H "Authorization: Bearer $P2C_INGEST_TOKEN" -H "Content-Type: application/json" --data-binary @-
```

## Good to know

- **Only new recordings:** recordings made after the Claude settings are first saved are sent automatically.
  **Send now** sends any recording.
- **Limits:** Claude allows 30 sends an hour per scheduled task and 100 per account. A burst waits for the next
  check.
- **Long transcripts:** the message is cut to 65,536 characters, at a line, with a note and the Plaud file ID, so
  the task can fetch the rest through the Plaud connector. A local recording has no copy in Plaud, and the prompt tells
  the task so; keep local transcripts within the limit.
- **No duplicates:** each send starts a new session, and Claude's endpoint has no way to tell a repeat. So a send
  that times out is never repeated automatically. Rate limits and overloads are retried on the next check; anything
  else waits for you.
- **Your token stays with Anthropic:** the token is sent only to `api.anthropic.com`'s fire endpoint. Plaud2Claude
  keeps a record of what it sent in `/data/plaud2claude.json`, pushed transcripts under `/data/local/`, and never
  deletes anything.
