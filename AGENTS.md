# deepseek-chat

Browser automation for chat.deepseek.com, driven through `browser-harness` over CDP.
Runs against the **chromium-agent** profile (`~/.config/chromium-agent`), which stays
logged in to DeepSeek.

**Profile rule: never touch the Work profile** (`~/.config/chromium`, profile `Default`).
It is a different logged-in account and the operator's own browser. Only `chromium-agent`
belongs to this tool. Reading another profile is allowed only when asked for explicitly, and
then only via a copy — never by closing or driving the running browser.

## How it runs (hands-off loop)

`main()` does not send one prompt and stop. It loops:

1. Send the prompt (round 1: `DS_PROMPT`/`DS_ATTACH`; later rounds: the follow-up below).
2. `wait_for_answer()` polls the API, resumes a stall by clicking Continue/Retry, and backs off
   on rate limits, until a **new** assistant message exists and is no longer pending.
3. Save the transcript, and write every `=== FILE:` block to `DS_SAVE_DIR` (basename only).
4. Read the next step out of the reply itself: a reply ending in
   `INCOMPLETE — continue from scene 04` makes `continue from scene 04` the next prompt.
5. Repeat until the reply asks for nothing (or `DS_FOLLOWUPS` rounds are used).

Step 4 is why no model is needed to decide what to do next — the chat states it in its own
text. Verified by replaying the real chapter-3 chat: the loop reproduces all 7 follow-ups
exactly and saves all 72 scene files with no gaps.

The whole flow is also importable: the helpers (`send_prompt`, `wait_for_answer`,
`next_followup`, `parse_file_blocks`, `save_file_blocks`, `history`, `summarize`, …) are plain
module-level functions, so a custom run can compose them. Import with `DS_IMPORT_ONLY=1` to
skip `main()`. They only work inside a browser-harness script, since `js`, `wait`,
`click_at_xy` and friends come from the harness globals.

## Run

```bash
./launch.sh                 # once: starts chromium-agent + CDP on 127.0.0.1:9222
                            # log into chat.deepseek.com in that window, leave it open
DS_PROMPT='...' ./run.sh    # one hands-off chat run
```

Config is env-only (browser-harness reads the script from stdin, so there is no argv):

| var | meaning | default |
|---|---|---|
| `DS_PROMPT` / `DS_PROMPT_FILE` | prompt text | — |
| `DS_ATTACH` | file to upload into the chat | none |
| `DS_DEEPTHINK` | `1`/`0` | `1` |
| `DS_SEARCH` | `1`/`0` | `0` |
| `DS_OUTDIR` | output dir | `./runs` |
| `DS_SAVE_DIR` | where `=== FILE:` blocks land | `<outdir>/files` |
| `DS_FOLLOWUPS` | max auto follow-ups before stopping | `60` |
| `DS_MAX_WAIT` | seconds to wait per reply | `1800` |
| `DS_CONTINUE_DELAY` | wait before clicking Continue/Retry | `20` |
| `DS_RATE_WAITS` | rate-limit backoff ladder, seconds | `150,300,300` |
| `DS_STALL_AFTER` | seconds of no output before trying to resume | `45` |

Output per run: `chat_id.txt`, `transcript.json`, `transcript.md`, plus extracted files.

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

## Decision model: deliberately not used

An earlier version fell back to a `typesafe`-style decision model (Jev 1.13 free over opencode
Zen, or local `localdecide`) to pick the resume control when the text match found nothing.
**The hands-off script does not use it, and it has been removed.** The deterministic path is
the only path: `locator(CONTINUE_RE)` finds the control by its text and clicks its
runtime-resolved box.

If it is ever reconsidered, the measured facts are: both backends speak
`POST <base>/systemone` with `{"model", "state", "questions"}`; opencode Zen **rejects a
request with no `User-Agent`** (403); the model picks a real resume control at 0.99 confidence
but **invents one when none exists** (0.29 and 0.66 were observed), so a confidence gate of
~0.8 is mandatory. Without the gate it clicks arbitrary buttons. Also note `localdecide` was
broken on this machine — `localdecide doctor` fails with
`FileNotFoundError: Subfolder 'v10s' not found in 'cklxx/laya-browser'` because the upstream
repo was flattened on 2026-09-29.

## Open items

- The rate-limit backoff path has not been triggered end-to-end.
- Resume was verified live on a real stalled chat through `wait_for_answer()` (the function
  `main()` uses): `INCOMPLETE` -> clicked Continue -> `FINISHED`, no navigation. It has still
  not been exercised inside a full `./run.sh` run, which opens a new chat and would need that
  generation to stall.
- **Two reply formats exist and only one is handled.** The chapter-3 chat used explicit
  `=== FILE: <path>` headers (handled by `save_file_blocks`). The chapter-5 reply used *no*
  markers at all — just one fenced `json` block (73,822 chars) and one fenced `markdown` block,
  with 2 stray characters outside them. Those had to be extracted by hand. If a reply carries no
  `=== FILE:` header, `save_file_blocks` silently saves nothing; a fallback that saves lone
  fenced blocks by language is the obvious next step.
