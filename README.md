# deepseek-chat

Automate `chat.deepseek.com` in the **chromium-agent** browser profile, hands-off: open a new
chat, optionally attach a file, set the DeepThink/Search toggles, send a prompt, wait for the
answer, resume it if it stalls, save any returned files, then send the reply's own requested
follow-up and repeat until the work is done.

Driven through `browser-harness` over CDP. The transcript comes from DeepSeek's own
`history_messages` API, so thinking and response are already separated — no DOM scraping.

No model is involved anywhere: resume detection is a text match on the Continue/Retry button,
and the next prompt is read out of the reply's own text.

The loop is self-driving: a reply that ends `INCOMPLETE — continue from scene 04` makes
`continue from scene 04` the next prompt, so no model is needed to decide what to do next.

`DS_RESUME=1` picks an interrupted run back up: it reads the open chat, waits out (and clicks Continue on) an
answer that is still in flight, and then carries on with the normal follow-up loop.

**Never touches the Work profile** (`~/.config/chromium`); only `chromium-agent`.

## Setup

```bash
./launch.sh        # chromium-agent + CDP on 127.0.0.1:9222
```

Log into `chat.deepseek.com` once in that window, then leave it open. `launch.sh` is
idempotent; it does nothing if the profile is already running.

## Run

```bash
DS_PROMPT='Explain the CAP theorem' ./run.sh

DS_PROMPT_FILE=prompt.md DS_ATTACH=a.pdf,b.md DS_DEEPTHINK=1 DS_SEARCH=0 ./run.sh
```

All configuration is environment variables, because `browser-harness` reads the script
from stdin and there is no argv. The useful ones:

| variable | meaning | default |
|---|---|---|
| `DS_PROMPT` / `DS_PROMPT_FILE` | prompt text | — |
| `DS_RESUME` / `DS_RESUME_URL` | continue the chat already open (or that URL) instead of starting a new one | off |
| `DS_ATTACH` | file(s) to upload, comma-separated | none |
| `DS_DEEPTHINK` / `DS_SEARCH` | `1` or `0` | `1` / `0` |
| `DS_OUTDIR` | where the run is written | `./runs` |
| `DS_SAVE_DIR` | where returned `=== FILE:` blocks land | `<outdir>/files` |
| `DS_FOLLOWUPS` | max auto follow-ups before stopping | `60` |
| `DS_MAX_WAIT` | give up after this many seconds per reply | `1800` |
| `DS_CONTINUE_DELAY` | wait before clicking Continue/Retry | `20` |
| `DS_RATE_WAITS` | rate-limit backoff ladder, seconds | `150,300,300` |
| `DS_STALL_AFTER` | seconds of no output before trying to resume | `45` |

## Output

Each run writes `chat_id.txt`, `transcript.json` (raw API) and `transcript.md`
(user prompt, thinking, response) into `DS_OUTDIR`, and extracts any returned files into
`DS_SAVE_DIR`. Extraction uses the basename only — reply text is not allowed to choose a
write path.

## Check

```bash
python3 test_stall_gate.py     # offline, no browser, no network
```

## Status

Verified on a live account: new chat, toggles, send, chat-id capture, transcript export,
resuming a genuinely stalled answer (`INCOMPLETE` -> Continue -> `FINISHED`), and — by
replaying the real chapter-3 chat — reproducing all 7 follow-ups and all 72 scene files.

Verified: `DS_RESUME=1` on a live stalled chat (iv1 run), the Continue click, and the rate-limit backoff
ladder.
A reply with no `=== FILE:` header saves no files automatically (the chapter-5 reply used
bare fenced blocks); see `AGENTS.md`.

`AGENTS.md` has the verified selectors, API endpoint and the decision-model caveats.
