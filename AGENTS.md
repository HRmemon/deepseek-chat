# deepseek-chat

Browser automation for chat.deepseek.com, driven through `browser-harness` over CDP.
Runs against the **chromium-agent** profile (`~/.config/chromium-agent`), which stays
logged in to DeepSeek.

## Run

```bash
./launch.sh                 # once: starts chromium-agent + CDP on 127.0.0.1:9222
                            # log into chat.deepseek.com in that window, leave it open
DS_PROMPT='...' ./run.sh    # one chat run
```

Config is env-only (browser-harness reads the script from stdin, so there is no argv):

| var | meaning | default |
|---|---|---|
| `DS_PROMPT` / `DS_PROMPT_FILE` | prompt text | — |
| `DS_ATTACH` | file to upload into the chat | none |
| `DS_DEEPTHINK` | `1`/`0` | `1` |
| `DS_SEARCH` | `1`/`0` | `0` |
| `DS_OUTDIR` | output dir | `./runs` |
| `DS_MAX_WAIT` | seconds before giving up | `1800` |
| `DS_CONTINUE_DELAY` | wait before clicking Continue/Retry | `20` |
| `DS_RATE_WAITS` | rate-limit backoff ladder, seconds | `150,300,300` |

| `DS_JEVA` | `1`/`0` enable the decision-model fallback | `1` |
| `DS_JEVA_URL` | decision-model endpoint | opencode Zen `systemone` |
| `DS_JEVA_MIN_CONF` | confidence below which the model is ignored | `0.8` |
| `DS_STALL_AFTER` | seconds of no output before trying to resume | `45` |

Output per run: `chat_id.txt`, `transcript.json`, `transcript.md`.

## Verified behaviour (2026-10-03, logged-in account)

- **New chat** = navigate to `https://chat.deepseek.com/`. Always opens an empty chat
  (verified: 0 `.ds-message`, even when history exists). The sidebar "New chat" button is
  hidden when the sidebar is collapsed in a narrow window, so do not click for it.
- **Toggles**: `button.ds-toggle-button`, text `DeepThink` / `Search`; on-state is the
  `ds-toggle-button--selected` class *or* `aria-pressed="true"`. React updates async —
  re-read state after clicking, not immediately.
- **Composer**: single `textarea`. React-controlled, so set it with the native
  `HTMLTextAreaElement.prototype.value` setter plus a bubbling `input` event.
- **Send**: `div.ds-button--primary` (not a `<button>`); disabled shows `ds-button--disabled`.
  It is disabled whenever the composer is empty, so it is *not* a "still generating" signal.
- **Attach**: hidden `input[type=file]` via `upload_file()`.
- **Chat id**: URL becomes `/a/chat/s/<uuid>` right after send.
- **Transcript** (the thing visible in DevTools → Network):
  `GET /api/v0/chat/history_messages?chat_session_id=<id>`, header
  `Authorization: Bearer <JSON.parse(localStorage.userToken).value>`.
  Each message has `status` (`FINISHED`), `has_pending_fragment`, `auto_continue`,
  `incomplete_message`, and `fragments[]` with `type` `REQUEST` (user) / `THINK` (reasoning)
  / `RESPONSE` (answer). This is why there is no DOM scraping.
- **No local chat store**: IndexedDB is empty (only `applog_sdk_event_store_*`); localStorage
  holds flags only. Conversation state is server-side.
- **Continue detection**: `status` is the signal. Verified on a real stalled session:
  `INCOMPLETE` (stalled) -> click Continue -> `WIP` (streaming) -> `FINISHED`. `auto_continue`
  was **false** on that stalled session, so it cannot be used alone. The continued text merges
  into the existing assistant message — the message *count* does not change, so never gate
  completion on count.
- **Continue button markup** (verified): `div[role=button][tabindex="0"]` with
  `ds-button--outlinedNeutral ds-button--outlined ds-button--capsule ds-button--s`, label in
  `span.ds-button__content`. `locator(CONTINUE_RE)` resolves it correctly on the live page.
  Clicks use runtime-resolved coordinates, so the button's position is irrelevant.

## Attaching CDP to a profile that is already open

The open DeepSeek session may live in a profile that has no debug port. Profiles on this
machine: `~/.config/chromium` holds `Default` = **Work** and `Profile 1` = **AGENT**;
`~/.config/chromium-agent` is a separate user-data-dir. Only `chromium-agent` is normally
launched with CDP.

To read a profile without closing the running browser, clone it and launch the clone:

```bash
rm -rf ~/.cache/chromium-work-copy
cp --reflink=auto -a ~/.config/chromium ~/.cache/chromium-work-copy
rm -f ~/.cache/chromium-work-copy/Singleton*            # or it hands the URL to the running browser
chromium --user-data-dir="$HOME/.cache/chromium-work-copy" --profile-directory=Default \
  --remote-debugging-port=9223 --remote-allow-origins='*' --no-first-run URL
BU_NAME=work BU_CDP_URL=http://127.0.0.1:9223 browser-harness   # separate daemon per port
```

Copy to the same filesystem as the source: `--reflink` cannot cross devices, and `/tmp` is not
on `/home` here. `BU_NAME` must differ from the default daemon, which stays on 9222.
Cleanup: `rm -rf ~/.cache/chromium-work-copy` once its chromium is closed.
- API-level failures surface as `code`/`msg` or `data.biz_code`/`biz_msg`.

## Decision model (Continue/Retry detection)

Two interchangeable backends, same `/v1/systemone` wire format — swapping is a URL change:

| backend | URL | auth |
|---|---|---|
| Jev 1.13 free (opencode Zen) | `https://opencode.ai/zen/v1/systemone` | `OPENCODE_API_KEY` |
| localdecide / Laya (local) | `http://127.0.0.1:8791/v1/systemone` | none |

Request `{"model", "state", "questions"}` -> `{"answers": {...}}`. **A `User-Agent` header
is required** — without one opencode Zen returns 403.

- The `browser-decide` and `decide` skills document this pattern. Contract: the model may
  only pick the **index of something already observed**; it never returns a selector,
  coordinate, or text. `visible_controls()` builds the table, `resolve_choice()` maps the
  answer back, and an out-of-range index is treated as no answer.
- **The confidence gate is load-bearing.** Measured against jev-1.13-free: with a real
  resume control present it picks it at 0.99, but with *no* resume control on screen it
  invents one (`ok` at 0.29, `DeepThink` at 0.66). Without a gate the automation clicks
  random buttons. `test_stall_gate.py` pins this behaviour.
- `localdecide` itself is currently unusable on this machine: `localdecide doctor` fails
  with `FileNotFoundError: Subfolder 'v10s' not found in 'cklxx/laya-browser'` (upstream
  checkpoint layout moved). Jev-free is the working backend until that is fixed.
- `DS_JEVA=0` disables the model entirely; the deterministic fast path
  (`CONTINUE_RE` + runtime-resolved click) is tried first either way.

## Open items

- The rate-limit backoff path has not been triggered end-to-end.
- `localdecide` checkpoint fetch is broken (see above).
- Clicking Continue was verified by hand through this script's helpers; it has not yet been
exercised as part of a full `./run.sh` run, which starts by opening a *new* chat.
