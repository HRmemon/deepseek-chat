# DeepSeek chat automation. Run with ./run.sh (this file is fed to browser-harness on stdin).
#
# Hands-off loop: new chat -> optional attach -> toggles -> send -> wait (resuming a stalled
# answer by clicking Continue/Retry) -> save any `=== FILE:` blocks to disk -> read the reply's
# own "INCOMPLETE - continue from X" instruction and send it as the next prompt -> repeat until
# the reply stops asking, then write the transcript.
#
# Config (env):
#   DS_PROMPT        prompt text            (or DS_PROMPT_FILE = path)
#   DS_ATTACH        file(s) to upload, comma-separated; avoids pasting a huge prompt
#   DS_DEEPTHINK     1/0  (default 1)
#   DS_SEARCH        1/0  (default 0)
#   DS_OUTDIR        default ./runs
#   DS_SAVE_DIR      where `=== FILE:` blocks land (default <outdir>/files)
#   DS_FOLLOWUPS     max auto follow-ups before stopping (default 60)
#   DS_MAX_WAIT      seconds to wait per reply (default 1800)
#   DS_CONTINUE_DELAY  seconds to wait before clicking Continue/Retry (default 20)
#   DS_RATE_WAITS    comma list of backoff seconds (default 150,300,300)
#   DS_STALL_AFTER   seconds of no output before trying to resume (default 45)
#   DS_STABLE_S      seconds the reply text must stay unchanged before it counts as complete (default 4)
#   DS_TOTAL_WAIT    overall seconds for the whole output before giving up, exit 2 (default 21600)
#   DS_CONTINUE_PROMPT  text sent when a reply stops without a COMPLETE line (default "continue")
#
# The run ends only when a reply ends with "COMPLETE — ...". A reply that stops early without
# either terminator gets another continue, so a dropped tail costs a round, not the whole output.
# Watch progress with <DS_OUTDIR>/state.json.

import json
import os
import pathlib
import re
import time

RATE_RE = re.compile(
    r"too frequent|too many|rate.?limit|try again later|please wait|"
    r"\u7e41\u7406|\u592a\u9891\u7e41|\u7a0d\u540e", re.I)
CONTINUE_RE = r"^(continue|retry|try again|regenerate|resume)$"
# DeepSeek marks a cut-off reply in its own text, e.g. "INCOMPLETE - continue from scene 04".
# That line is the next instruction, so the loop needs no model to decide what to do.
# Anchored to a line start and the end of the reply, so a code comment that merely mentions
# INCOMPLETE cannot fake a follow-up.
INCOMPLETE_RE = re.compile(r"^INCOMPLETE\s*[\u2014\u2013-]\s*(.+?)\s*\Z", re.M)
BARE_INCOMPLETE_RE = re.compile(r"INCOMPLETE[^\w]*\Z")
# The other half of the contract: the reply says when the OUTPUT is done, e.g. "COMPLETE - 42 scenes".
# Only that line ends the run. A reply that merely stops is cut off and gets another continue.
COMPLETE_RE = re.compile(r"^COMPLETE\s*[\u2014\u2013-]\s*(.+?)\s*\Z", re.M)
BARE_COMPLETE_RE = re.compile(r"^COMPLETE[^\w]*\Z", re.I)
FILE_RE = re.compile(r"^=== FILE:\s*(.+?)\s*$", re.M)   # no closing === on the real files


def say(*a):
    print("[deepseek]", *a, flush=True)


def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


# ---------------------------------------------------------------- browser bits

def locator(pattern, maxlen=40):
    """Innermost visible element whose text matches `pattern`, as a clickable box.

    Collects every matching candidate and weights by fewest descendants, skipping any
    whose clickable ancestor has no box — a hidden match must not shadow a visible one.
    """
    return js(f"""(() => {{
      const re = new RegExp({json.dumps(pattern)}, 'i');
      const cands = [];
      for (const e of document.querySelectorAll('body *')) {{
        const t = (e.innerText || '').trim();
        if (!t || t.length > {maxlen} || !re.test(t)) continue;
        const c = e.closest('button,[role=button],[tabindex]') || e;
        const r = c.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        cands.push({{c: c, t: t, kids: e.querySelectorAll('*').length}});
      }}
      if (!cands.length) return null;
      cands.sort((a, b) => a.kids - b.kids);
      const win = cands[0];
      win.c.scrollIntoView({{block: 'center'}});
      const r = win.c.getBoundingClientRect();
      return {{x: r.x + r.width/2, y: r.y + r.height/2, text: win.t.slice(0, 40)}};
    }})()""")


def click_text(pattern, timeout=0.0, maxlen=40):
    """Click the element matching `pattern`. Location is resolved at click time."""
    deadline = time.time() + timeout
    while True:
        box = locator(pattern, maxlen)
        if box:
            click_at_xy(box["x"], box["y"])
            return box["text"]
        if time.time() >= deadline:
            return None
        wait(1.0)


def page_text():
    return js("document.body.innerText") or ""


# Only *live* banners count as a rate limit. Scanning the whole page text matched the conversation
# itself — a "too frequent" line from an earlier reply or a toast still in the DOM made every later
# state look rate-limited, so the loop waited out its retries instead of clicking the Continue
# button that was plainly on screen (seen in the iv1 run, 2026-10-03).
ALERT_TEXT = """(() => {
  const sel = '[role=alert],[class*=toast],[class*=alert],[class*=error],[class*=rate]';
  let s = '';
  for (const e of document.querySelectorAll(sel)) {
    if (e.offsetWidth || e.offsetHeight) s += ' ' + (e.innerText || '');
  }
  return s;
})()"""


def alert_text():
    return js(ALERT_TEXT) or ""


def rate_limited():
    return bool(RATE_RE.search(alert_text()))


def resume_control():
    """The visible Continue/Retry control for a stalled answer, if there is one."""
    return locator(CONTINUE_RE, 20)


def _toggle_js(label, body):
    return js(f"""(() => {{
      const b = [...document.querySelectorAll('.ds-toggle-button')]
        .find(x => new RegExp('^' + {json.dumps(label)} + '$', 'i').test((x.innerText||'').trim()));
      if (!b) return null;
      {body}
    }})()""")


def toggle_state(label):
    return _toggle_js(label,
        "return b.getAttribute('aria-pressed') === 'true' || /--selected/.test(b.className);")


def set_toggle(label, want):
    """Set a DeepThink/Search style toggle. Returns the state read back after the click."""
    if toggle_state(label) is None:
        return None
    _toggle_js(label, f"""const on = b.getAttribute('aria-pressed') === 'true' || /--selected/.test(b.className);
      if (on !== {json.dumps(bool(want))}) b.click();
      return true;""")
    wait(0.5)
    return toggle_state(label)


def set_composer(text):
    """React-controlled textarea: native setter + input event."""
    ok = js(f"""(() => {{
      const e = document.querySelector('textarea');
      if (!e) return false;
      const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
      set.call(e, {json.dumps(text)});
      e.dispatchEvent(new Event('input', {{bubbles: true}}));
      return e.value.length;
    }})()""")
    return ok


def click_send():
    box = js("""(() => {
      const b = [...document.querySelectorAll('div,button')]
        .find(e => /ds-button--primary/.test(String(e.className)) && !/--disabled/.test(String(e.className)));
      if (!b) return null;
      b.scrollIntoView({block:'center'});
      const r = b.getBoundingClientRect();
      return {x: r.x + r.width/2, y: r.y + r.height/2};
    })()""")
    if not box:
        return False
    click_at_xy(box["x"], box["y"])
    return True


# ---------------------------------------------------------------- state via API

def history(sid):
    """Structured transcript straight from the page's own API (no DOM scraping)."""
    raw = js(f"""(async () => {{
      const tok = JSON.parse(localStorage.userToken).value;
      const r = await fetch('/api/v0/chat/history_messages?chat_session_id={sid}',
                            {{headers: {{Authorization: 'Bearer ' + tok}}}});
      return await r.text();
    }})()""")
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw}


def summarize(payload):
    """-> (messages, error_text). error_text non-empty on API-level failure."""
    if not isinstance(payload, dict):
        return [], "unparseable API response"
    data = payload.get("data") or {}
    if payload.get("code") not in (0, None):
        return [], str(payload.get("msg") or payload.get("code"))
    if data.get("biz_code") not in (0, None):
        return [], str(data.get("biz_msg") or data.get("biz_code"))
    msgs = ((data.get("biz_data") or {}).get("chat_messages")) or []
    return msgs, ""


def assistant_pending(msgs):
    for m in reversed(msgs):
        if m.get("role") != "ASSISTANT":
            continue
        return (m.get("status") != "FINISHED"
                or bool(m.get("has_pending_fragment"))
                or bool(m.get("auto_continue"))
                or bool(m.get("incomplete_message")))
    return True


# --------------------------------------------------------------- DOM stability
# `assistant_pending` reads the page's own API, which is the authoritative "still streaming?"
# signal. This adds the second, independent one: how long the rendered reply text has gone
# unchanged. Both must agree before a reply counts as complete, because the API can flip to
# FINISHED a beat before the last fragment lands in the DOM.
OBSERVER_JS = """(() => {
  if (window.__dsObs) return true;
  const o = {changed: Date.now(), len: 0};
  window.__dsObs = o;
  const arm = () => {
    const nodes = document.querySelectorAll('.ds-message');
    const last = nodes[nodes.length - 1];
    if (!last || last.__dsWatch) return;
    last.__dsWatch = true;
    new MutationObserver(() => { o.changed = Date.now(); o.len = (last.innerText || '').length; })
      .observe(last, {childList: true, subtree: true, characterData: true});
    o.changed = Date.now();
    o.len = (last.innerText || '').length;
  };
  window.__dsArm = arm;
  arm();
  setInterval(arm, 1000);   // a new reply becomes the watched node
  return true;
})()"""


def dom_state():
    """-> {'age': seconds since the reply text last changed, 'len': chars} or None."""
    st = js("""(() => {
      const o = window.__dsObs;
      return o ? {age: (Date.now() - o.changed) / 1000, len: o.len} : null;
    })()""")
    if not st:
        js(OBSERVER_JS)
        st = js("""(() => {
          const o = window.__dsObs;
          return o ? {age: (Date.now() - o.changed) / 1000, len: o.len} : null;
        })()""")
    return st


def chat_id_from_url():
    m = re.search(r"/a/chat/s/([0-9a-f-]{36})", js("location.href") or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------- transcript

def render_markdown(msgs):
    out = []
    for m in msgs:
        role = m.get("role", "?").lower()
        out.append(f"## {role}\n")
        for f in m.get("fragments") or []:
            kind = (f.get("type") or "").upper()
            body = f.get("content") or ""
            if kind == "THINK":
                out.append(f"<details><summary>thinking</summary>\n\n{body}\n\n</details>\n")
            elif kind == "RESPONSE":
                out.append(body + "\n")
            elif kind == "REQUEST":
                out.append(body + "\n")
            else:
                out.append(f"*[{kind}]*\n\n{body}\n")
        out.append("")
    return "\n".join(out)


def save_run(sid, msgs, outdir):
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "chat_id.txt").write_text(sid + "\n")
    (outdir / "transcript.json").write_text(json.dumps(msgs, indent=2, ensure_ascii=False))
    (outdir / "transcript.md").write_text(render_markdown(msgs))
    return outdir


# ------------------------------------------------------------------ loop pieces

def assistant_texts(msgs):
    """Every RESPONSE fragment of every assistant message, in order."""
    return ["".join(f.get("content") or "" for f in m.get("fragments") or []
                    if (f.get("type") or "").upper() == "RESPONSE")
            for m in msgs if m.get("role") == "ASSISTANT"]


def terminator(text):
    """What the reply says about its own output: ('complete'|'incomplete'|None, payload).

    The payload of an incomplete reply is exactly the message the chat expects next
    ("continue from scene 04"). No terminator at all means the reply was cut off.
    """
    text = (text or "").rstrip()
    if not text:
        return None, ""
    last = text.splitlines()[-1].strip()
    for kind, rx in (("incomplete", INCOMPLETE_RE), ("complete", COMPLETE_RE)):
        m = rx.search(last) or (None if "\n" in last else rx.search(text))
        if m:
            return kind, m.group(1).strip()
    if BARE_INCOMPLETE_RE.search(last):
        return "incomplete", "continue"
    if BARE_COMPLETE_RE.match(last):
        return "complete", ""
    return None, ""


def write_state(outdir, **kw):
    """A one-line answer to "is it still going?" for whoever watches the run from outside."""
    kw["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "state.json").write_text(json.dumps(kw, ensure_ascii=False, indent=2) + "\n")


def strip_fence(block):
    lines = block.strip("\n").split("\n")
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().startswith("```"):
            # a closing fence ends the file; the terminator line and any prose come after it
            lines = lines[:i]
            break
    return "\n".join(lines).rstrip("\n") + "\n"


def parse_file_blocks(text):
    parts = FILE_RE.split(text or "")
    return [(parts[i].strip(), strip_fence(parts[i + 1]))
            for i in range(1, len(parts) - 1, 2)]


def attachment_state(names):
    """Only composer upload cards, never old attachments in conversation messages."""
    return js(f"""(() => {{
      const names = {json.dumps(names)};
      const cards = [...document.querySelectorAll('.cd314545')]
        .filter(e => !e.closest('.ds-message'));
      const items = names.map(name => {{
        const matches = cards.filter(e => e.querySelector('.e70accd6')?.textContent.trim() === name);
        const e = matches[0];
        const text = e ? (e.innerText || '') : '';
        const box = node => {{ if (!node) return null; const r=node.getBoundingClientRect();
          return {{x:r.x+r.width/2,y:r.y+r.height/2}}; }};
        return {{name, count:matches.length, text,
          failed:/server busy|failed|error|try again/i.test(text),
          pending:/uploading|processing|parsing|reading|queued/i.test(text),
          retry:box(e?.querySelector('._21072a6')),
          remove:box(e?.querySelector('._8402d8c'))}};
      }});
      const send = [...document.querySelectorAll('div,button')].find(e =>
        /ds-button--primary/.test(String(e.className)) && !/--disabled/.test(String(e.className)));
      return {{items, sendEnabled:!!send}};
    }})()""")


def attachments_ready(state):
    return bool(state and state['sendEnabled'] and state['items'] and all(
        i['count'] == 1 and not i['failed'] and not i['pending'] for i in state['items']))


def ensure_attachments(paths, prompt):
    """Bounded upload retry; never send a prompt with missing/failed attachments."""
    names = [pathlib.Path(p).name for p in paths]
    if len(set(names)) != len(names):
        raise ValueError('attachment basenames must be unique')
    state = attachment_state(names)
    missing = [p for p,i in zip(paths,state['items']) if not i['count']]
    if missing:
        upload_file('input[type=file]', missing if len(missing)>1 else missing[0])
    set_composer(prompt or 'Read the attached files and follow the brief.')
    deadline = time.time() + float(env('DS_UPLOAD_WAIT','180'))
    tries = 0
    next_retry = time.time() + 3
    while time.time() < deadline:
        state = attachment_state(names)
        if attachments_ready(state):
            say('attachments ready:', ', '.join(names))
            return prompt
        failed = [i for i in state['items'] if i['failed']]
        if failed and tries < 3 and time.time() >= next_retry:
            for item in failed:
                if item['retry']:
                    click_at_xy(item['retry']['x'], item['retry']['y'])
            tries += 1
            say(f'upload retry {tries}/3:', ', '.join(i['name'] for i in failed))
            next_retry = time.time() + (10,20,40)[tries-1]
        if failed and tries == 3 and time.time() >= next_retry:
            break
        wait(2)
    # Small local text can be supplied intact in the composer instead. Never extract
    # or silently drop a PDF/image; larger inputs stop with a visible blocker.
    text_suffixes = {'.md','.txt','.json','.css','.html'}
    if all(pathlib.Path(p).suffix.lower() in text_suffixes for p in paths):
        chunks = [f'\n\n=== INPUT: {pathlib.Path(p).name} ===\n'+pathlib.Path(p).read_text() for p in paths]
        combined = (prompt or '') + ''.join(chunks)
        if len(combined.encode()) <= 8000:
            for item in attachment_state(names)['items']:
                if item['remove']:
                    click_at_xy(item['remove']['x'], item['remove']['y'])
            wait(.5)
            if any(i['count'] for i in attachment_state(names)['items']):
                raise RuntimeError('failed upload card could not be removed')
            say('upload unavailable; using complete small text inputs in composer')
            return combined
    raise RuntimeError('attachment upload not ready after retries: '+', '.join(
        i['name']+': '+i['text'] for i in state['items']))


def parse_attach(value):
    """DS_ATTACH -> [absolute paths]. Comma-separated, so several files can go in one send.
    Blank entries are dropped, so a trailing comma is harmless."""
    return [str(pathlib.Path(p.strip()).resolve())
            for p in (value or "").split(",") if p.strip()]


def save_file_blocks(msgs, savedir):
    """Write every `=== FILE:` block under its basename in `savedir`.

    Basename only, on purpose: the declared path is model output, so it must not be able
    to write anywhere on disk by itself. Copy files out of savedir when they look right.
    """
    written = []
    for text in assistant_texts(msgs):
        for declared, body in parse_file_blocks(text):
            p = savedir / pathlib.Path(declared).name
            if p.exists() and p.read_text() == body:
                continue
            savedir.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
            written.append(p)
    return written


def send_prompt(text):
    set_composer(text)
    wait(0.3)
    if not js("document.querySelector('textarea').value.length"):
        raise RuntimeError("prompt did not land in the composer")
    if not click_send():
        raise RuntimeError('send disabled: prompt was NOT sent (check attachment readiness)')
    for _ in range(30):
        if not js("document.querySelector('textarea')?.value"):
            return
        wait(.5)
    raise RuntimeError('send not acknowledged: composer did not clear')


def wait_for_answer(sid, baseline, cfg):
    """Wait for one reply: resume stalls, back off on rate limits, return the messages.

    `baseline` is the number of assistant messages already present, so a previous round's
    finished reply is never mistaken for this round's answer.
    """
    started = time.time()
    waits_left = list(cfg["rate_waits"])
    last_sig = None
    last_progress = time.time()
    next_resume_try = 0.0
    msgs = []
    while time.time() - started < cfg["max_wait"]:
        msgs, err = summarize(history(sid))
        sig = json.dumps(msgs, sort_keys=True)
        if sig != last_sig:
            last_sig, last_progress = sig, time.time()
        msg_err = err and RATE_RE.search(err)
        # A visible Continue/Retry means the answer stalled and can be resumed: click it rather
        # than treating the state as a rate limit. Clicking is cheap and idempotent, waiting is not.
        stalled = resume_control()
        if (msg_err or rate_limited()) and not stalled:
            if not waits_left:
                raise SystemExit(f"rate limited and out of retries: {err}")
            nap = waits_left.pop(0)
            say(f"rate limited; waiting {nap:.0f}s ({len(waits_left)} retries left)")
            wait(nap)
            last_progress = time.time()
            continue
        if len(assistant_texts(msgs)) > baseline and not assistant_pending(msgs):
            st = dom_state()
            if not st:
                say("no reply node to watch; trusting the API")
                return msgs
            if st["age"] >= cfg["stable_s"]:
                say(f"answer settled (reply text quiet {st['age']:.1f}s, {st['len']} chars)")
                return msgs
        # stalled output: resume it by clicking the Continue/Retry control
        if stalled and time.time() >= next_resume_try:
            next_resume_try = time.time() + 20
            wait(cfg["cont_delay"])
            clicked = click_text(CONTINUE_RE, timeout=3, maxlen=20)
            if clicked:
                say("resumed via:", clicked)
                last_progress = time.time()
                wait(3.0)
                continue
        if int(time.time() - started) % 15 < 4:
            say(f"...waiting ({int(time.time()-started)}s)")
        wait(3.0)
    say(f"max wait {cfg['max_wait']:.0f}s reached")
    return msgs


# ---------------------------------------------------------------- main

def main():
    outdir = pathlib.Path(env("DS_OUTDIR", str(pathlib.Path.cwd() / "runs")))
    savedir = pathlib.Path(env("DS_SAVE_DIR", str(outdir / "files")))
    max_wait = float(env("DS_MAX_WAIT", "1800"))
    cont_delay = float(env("DS_CONTINUE_DELAY", "20"))
    rate_waits = [float(x) for x in env("DS_RATE_WAITS", "150,300,300").split(",")]
    deepthink = env("DS_DEEPTHINK", "1") == "1"
    search = env("DS_SEARCH", "0") == "1"

    prompt = env("DS_PROMPT", "")
    if not prompt and env("DS_PROMPT_FILE"):
        prompt = pathlib.Path(env("DS_PROMPT_FILE")).read_text()
    resume_url = env("DS_RESUME_URL")
    resume = bool(resume_url) or env("DS_RESUME", "0") == "1"
    if not prompt and not env("DS_ATTACH") and not resume:
        raise SystemExit("need DS_PROMPT, DS_PROMPT_FILE, DS_ATTACH or DS_RESUME")

    sid = None
    baseline = 0          # assistant messages already present, so rounds cannot be confused
    skip_send = False

    if resume:
        # Continue an existing chat instead of starting over: an interrupted run leaves the
        # chat, its attachments and its partial files on disk, and re-sending the brief wastes it.
        if resume_url:
            goto_url(resume_url)
            wait_for_load()
            wait(2.0)
        sid = chat_id_from_url()
        if not sid:
            raise SystemExit("resume: the open tab is not a deepseek chat")
        msgs0, _err = summarize(history(sid))
        pending = assistant_pending(msgs0)
        baseline = len(assistant_texts(msgs0)) - (1 if pending else 0)
        say(f"resuming {sid}: {len(assistant_texts(msgs0))} replies, answer in flight: {pending}")
        last = (assistant_texts(msgs0) or [""])[-1]
        if pending:
            skip_send = True          # the reply is still coming; wait for it, then follow up
        else:
            kind, payload = terminator(last)
            if kind == "complete" and not prompt:
                # a pure hand-off resume: the chat settled on a COMPLETE line, so there is nothing
                # to send. With an explicit DS_PROMPT, that prompt is a new stage for this same
                # chat, and the previous stage's COMPLETE line says nothing about it.
                say(f"resume: the reply already ends with {last.splitlines()[-1].strip()!r}; nothing to do")
                write_state(outdir, chat=sid, state="complete", parts=[])
                return 0
            prompt = prompt or (payload if kind == "incomplete" else "continue")
            say(f"resume: last reply was {kind or 'cut off'}; next prompt {prompt[:60]!r}")
    else:
        pass

    # 1. fresh chat. The sidebar "New chat" button is hidden when the sidebar is
    #    collapsed (narrow window), but the root URL always opens an empty chat.
    if not resume:
        if "chat.deepseek.com" not in (js("location.href") or ""):
            new_tab("https://chat.deepseek.com/")
        goto_url("https://chat.deepseek.com/")
        wait_for_load()
        wait(2.5)
        if "sign_in" in (js("location.href") or ""):
            raise SystemExit("not logged in on the chromium-agent profile")
        if js("document.querySelectorAll('.ds-message').length"):
            raise SystemExit("root URL did not open an empty chat")
        say("new chat ready")

        # 2. toggles
        say("deepthink ->", set_toggle("DeepThink", deepthink))
        say("search ->", set_toggle("Search", search))

    # 3. optional attachments. Files are uploaded rather than pasted because a large prompt
    #    cannot be sent: the CDP/IPC transport rejects a single oversized frame ("Separator is
    #    found, but chunk is longer than limit"), so a ~100KB prompt cannot reach the composer
    #    as text. Works on resume too, which is how a later stage's brief joins a live chat.
    paths = parse_attach(env("DS_ATTACH"))
    if paths:
        missing = [p for p in paths if not os.path.exists(p)]
        if missing:
            raise SystemExit("attachment not found: " + ", ".join(missing))
        prompt = ensure_attachments(paths, prompt)
        say("inputs supplied:", ", ".join(pathlib.Path(p).name for p in paths))

    js(OBSERVER_JS)   # watch the reply text, so "complete" needs API and DOM to agree
    # 4. hands-off rounds: send, wait, save files, then obey the reply's own
    #    "INCOMPLETE - continue from X" line until it stops asking.
    cfg = dict(max_wait=max_wait, cont_delay=cont_delay, rate_waits=rate_waits,
               stall_after=float(env("DS_STALL_AFTER", "45")),
               stable_s=float(env("DS_STABLE_S", "4")))
    max_rounds = int(env("DS_FOLLOWUPS", "60"))
    total_wait = float(env("DS_TOTAL_WAIT", "21600"))
    continue_prompt = env("DS_CONTINUE_PROMPT", "continue")
    deadline_all = time.time() + total_wait
    written_all = set()
    state = "running"

    rnd = 0
    msgs = []
    prompt = prompt or continue_prompt   # an attachment-only run still needs a send
    while True:
        rnd += 1
        if rnd > max_rounds:
            state = "rounds"
            say(f"follow-up limit ({max_rounds}) reached; stopping")
            break
        if time.time() > deadline_all:
            state = "timeout"
            say(f"overall budget {total_wait:.0f}s reached with no COMPLETE line; stopping")
            break
        if skip_send:
            skip_send = False
            say(f"round {rnd}: waiting for the reply already in flight")
        elif msgs and assistant_pending(msgs):
            # the previous reply is still streaming: a new prompt would collide with it
            say(f"round {rnd}: reply still streaming, waiting instead of sending")
        else:
            say(f"round {rnd}: sending {prompt[:70]!r}")
            send_prompt(prompt)
        if sid is None:
            deadline = time.time() + 30
            while not sid and time.time() < deadline:
                wait(1.0)
                sid = chat_id_from_url()
            if not sid:
                raise SystemExit("no chat id in URL after send")
            say("chat id:", sid)

        msgs = wait_for_answer(sid, baseline, cfg)
        if not msgs:
            raise SystemExit("no transcript returned")
        pending = assistant_pending(msgs)
        baseline = len(assistant_texts(msgs)) - (1 if pending else 0)
        save_run(sid, msgs, outdir)
        written = save_file_blocks(msgs, savedir)
        if written:
            say(f"round {rnd}: saved {len(written)} file(s) ->", savedir)
        written_all |= {p.name for p in savedir.glob("*")}
        write_state(outdir, chat=sid, round=rnd, state="waiting" if pending else "running",
                    parts=sorted(written_all))

        texts = assistant_texts(msgs)
        kind, payload = terminator(texts[-1]) if texts else (None, "")
        if kind == "complete":
            state = "complete"
            say(f"output complete after {rnd} round(s): {texts[-1].splitlines()[-1].strip()!r}")
            break
        if pending:
            # it is still writing: keep waiting on the same reply, send nothing
            prompt = continue_prompt
            continue
        if kind == "incomplete":
            prompt = payload or continue_prompt
            say(f"auto follow-up: {prompt!r}")
        else:
            prompt = continue_prompt
            say("reply ended without a COMPLETE line; asking it to continue")

    d = save_run(sid, msgs, outdir)
    write_state(outdir, chat=sid, round=rnd, state=state, parts=sorted(written_all))
    say(f"done after {rnd} round(s): {state}")
    say("chat id:", sid)
    say("transcript:", d / "transcript.md")
    say("files:", savedir)
    if state == "timeout":
        raise SystemExit(2)
# browser-harness execs this file inside its own module, so __name__ is never
# "__main__" here. Use an explicit opt-out so tests can import without running a chat.
if os.environ.get("DS_IMPORT_ONLY") != "1":
    main()
