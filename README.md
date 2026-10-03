# deepseek-chat

Automate `chat.deepseek.com` in the **chromium-agent** browser profile: open a new chat,
optionally attach a file, set the DeepThink/Search toggles, send a prompt, wait for the
answer, resume it if it stalls, then save the transcript.

Driven through `browser-harness` over CDP. The transcript comes from DeepSeek's own
`history_messages` API, so thinking and response are already separated — no DOM scraping.

## Setup

```bash
./launch.sh        # chromium-agent + CDP on 127.0.0.1:9222
```

Log into `chat.deepseek.com` once in that window, then leave it open. `launch.sh` is
idempotent; it does nothing if the profile is already running.

## Run

```bash
DS_PROMPT='Explain the CAP theorem' ./run.sh

DS_PROMPT_FILE=prompt.md DS_ATTACH=paper.pdf DS_DEEPTHINK=1 DS_SEARCH=0 ./run.sh
```

All configuration is environment variables, because `browser-harness` reads the script
from stdin and there is no argv. The useful ones:

| variable | meaning | default |
|---|---|---|
| `DS_PROMPT` / `DS_PROMPT_FILE` | prompt text | — |
| `DS_ATTACH` | file to upload into the chat | none |
| `DS_DEEPTHINK` / `DS_SEARCH` | `1` or `0` | `1` / `0` |
| `DS_OUTDIR` | where the run is written | `./runs` |
| `DS_MAX_WAIT` | give up after this many seconds | `1800` |
| `DS_CONTINUE_DELAY` | wait before clicking Continue/Retry | `20` |
| `DS_RATE_WAITS` | rate-limit backoff ladder, seconds | `150,300,300` |
| `DS_STALL_AFTER` | seconds of no output before trying to resume | `45` |
| `DS_JEVA` / `DS_JEVA_URL` / `DS_JEVA_MIN_CONF` | decision-model fallback | on / opencode Zen / `0.8` |

## Output

Each run writes `chat_id.txt`, `transcript.json` (raw API) and `transcript.md`
(user prompt, thinking, response) into `DS_OUTDIR`.

## Check

```bash
python3 test_stall_gate.py     # offline, no browser, no network
```

## Status

Verified end to end on a live account: new chat, toggles, send, chat-id capture,
transcript export, and resuming a genuinely stalled answer
(`INCOMPLETE` -> click Continue -> `WIP` -> `FINISHED`).

Not yet exercised: the rate-limit backoff ladder, and the resume path as part of a full
`./run.sh` run (which always starts a new chat).

`AGENTS.md` has the verified selectors, API endpoint and the decision-model caveats.
