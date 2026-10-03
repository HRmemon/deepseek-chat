# DeepSeek chat automation. Run with ./run.sh (this file is fed to browser-harness on stdin).
#
# Hands-off loop: new chat -> optional attach -> toggles -> send -> wait (resuming a stalled
# answer by clicking Continue/Retry) -> save any `=== FILE:` blocks to disk -> read the reply's
# own "INCOMPLETE - continue from X" instruction and send it as the next prompt -> repeat until
# the reply stops asking, then write the transcript.
#
# Config (env):
#   DS_PROMPT        prompt text            (or DS_PROMPT_FILE = path)
#   DS_ATTACH        optional file to upload in the chat
#   DS_DEEPTHINK     1/0  (default 1)
#   DS_SEARCH        1/0  (default 0)
#   DS_OUTDIR        default ./runs
#   DS_SAVE_DIR      where `=== FILE:` blocks land (default <outdir>/files)
#   DS_FOLLOWUPS     max auto follow-ups before stopping (default 60)
#   DS_MAX_WAIT      seconds to wait per reply (default 1800)
#   DS_CONTINUE_DELAY  seconds to wait before clicking Continue/Retry (default 20)
#   DS_RATE_WAITS    comma list of backoff seconds (default 150,300,300)
#   DS_STALL_AFTER   seconds of no output before trying to resume (default 45)

import json
import os
import pathlib
import re
import time
import urllib.request

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


def rate_limited():
    return bool(RATE_RE.search(page_text()))


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


def chat_id_from_url():
    m = re.search(r"/a/chat/s/([0-9a-f-]{36})", js("location.href") or "")
    return m.group(1) if m else None


# ------------------------------------------------------- decision model fallback
# Optional. Used only when the deterministic checks find no resume control.
# Both endpoints speak the same /v1/systemone wire format, so it is a URL swap:
#   jev-1.13-free  https://opencode.ai/zen/v1/systemone   (needs OPENCODE_API_KEY)
#   localdecide    http://127.0.0.1:8791/v1/systemone     (local Laya, no key)
# See the browser-decide / decide skills for the contract: the model picks an index
# of something we observed. It never returns a selector, coordinate, or text.

def jev(state, questions, model="jev-1.13-free", timeout=20):
    url = env("DS_JEVA_URL", "https://opencode.ai/zen/v1/systemone")
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    key = os.environ.get("OPENCODE_API_KEY")
    if key and "opencode.ai" in url:
        headers["Authorization"] = "Bearer " + key
    body = json.dumps({"model": model, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r).get("answers")
    except Exception as e:
        say("decision model unavailable:", e)
        return None


def visible_controls(maxlen=30, limit=20):
    """[{index, label, role}] for visible clickable controls. Indices are the contract."""
    return js(f"""(() => {{
      const seen = new Set(), out = [];
      for (const e of document.querySelectorAll('button,[role=button],[tabindex],a')) {{
        const label = (e.innerText || '').trim().replace(/\\s+/g, ' ');
        if (!label || label.length > {maxlen}) continue;
        const r = e.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        if (seen.has(label)) continue;
        seen.add(label);
        out.push({{index: out.length, label: label, role: e.getAttribute('role') || e.tagName.toLowerCase()}});
      }}
      return out.slice(0, {limit});
    }})()""") or []


def resolve_choice(ans, controls, min_conf):
    """Pure: a decision-model answer -> control label, or None when unsure / out of range.

    The model reliably picks the right control when one exists (conf ~0.99) but
    confabulates one when none does (measured 0.29 and 0.66), so confidence is a
    required gate, not a nicety. It also may only reference an index we listed.
    """
    action = (ans or {}).get("action") or {}
    if (action.get("confidence") or 0) < min_conf:
        return None
    choice = str(action.get("choice", ""))
    if not choice.isdigit():
        return None
    idx = int(choice)
    return controls[idx]["label"] if 0 <= idx < len(controls) else None


def jev_pick_resume(msgs):
    """Ask the decision model which observed control resumes a stalled answer.
    Returns the label of the chosen control, resolved back from its index."""
    controls = visible_controls()
    if not controls:
        return None
    last = next((m for m in reversed(msgs) if m.get("role") == "ASSISTANT"), {})
    state = {
        "url": js("location.href"),
        "assistant_status": last.get("status"),
        "auto_continue": last.get("auto_continue"),
        "has_pending_fragment": last.get("has_pending_fragment"),
        "incomplete_message": str(last.get("incomplete_message"))[:200],
        "controls": controls,
        "page_text_tail": (page_text() or "")[-600:],
    }
    criteria = {str(c["index"]): f"[{c['index']}] {c['label']} ({c['role']})" for c in controls}
    criteria["none"] = "No listed control resumes the answer"
    ans = jev(state, {"action": {
        "type": "choice",
        "instructions": ("The DeepSeek answer looks stalled. Which listed control resumes "
                         "it? Answer with its number, or 'none'."),
        "criteria": criteria}})
    if not ans:
        return None
    label = resolve_choice(ans, controls, float(env("DS_JEVA_MIN_CONF", "0.8")))
    if not label:
        say(f"decision model unsure: {json.dumps((ans.get('action') or {}))[:120]}")
    return label


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


def next_followup(text):
    """The reply states its own next step, e.g. 'INCOMPLETE - continue from scene 04'.

    Returns the part after the dash, which is exactly the message the chat expects next.
    """
    text = (text or "").rstrip()
    if not text:
        return None
    m = INCOMPLETE_RE.search(text)
    if m:
        return m.group(1).strip()
    if BARE_INCOMPLETE_RE.search(text.splitlines()[-1]):
        return "continue"
    return None


def strip_fence(block):
    lines = block.strip("\n").split("\n")
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).rstrip("\n") + "\n"


def parse_file_blocks(text):
    parts = FILE_RE.split(text or "")
    return [(parts[i].strip(), strip_fence(parts[i + 1]))
            for i in range(1, len(parts) - 1, 2)]


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
        click_text(r"send", timeout=3)


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
        if (err and RATE_RE.search(err)) or rate_limited():
            if not waits_left:
                raise SystemExit(f"rate limited and out of retries: {err}")
            nap = waits_left.pop(0)
            say(f"rate limited; waiting {nap:.0f}s ({len(waits_left)} retries left)")
            wait(nap)
            last_progress = time.time()
            continue
        if len(assistant_texts(msgs)) > baseline and not assistant_pending(msgs):
            say("answer settled")
            return msgs
        # stalled output: resume it. Fixed labels first, then ask the decision model.
        if time.time() - last_progress > cfg["stall_after"] and time.time() >= next_resume_try:
            next_resume_try = time.time() + 30
            clicked = None
            if locator(CONTINUE_RE, 20):
                clicked = click_text(CONTINUE_RE, timeout=3, maxlen=20)
            elif cfg["use_jev"]:
                label = jev_pick_resume(msgs)
                if label:
                    say(f"decision model picked {label!r}; waiting {cfg['cont_delay']:.0f}s")
                    wait(cfg["cont_delay"])
                    clicked = click_text("^" + re.escape(label) + "$", timeout=5, maxlen=30)
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
    if not prompt and not env("DS_ATTACH"):
        raise SystemExit("need DS_PROMPT, DS_PROMPT_FILE or DS_ATTACH")

    # 1. fresh chat. The sidebar "New chat" button is hidden when the sidebar is
    #    collapsed (narrow window), but the root URL always opens an empty chat.
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

    # 3. optional attachment
    attach = env("DS_ATTACH")
    if attach:
        upload_file("input[type=file]", str(pathlib.Path(attach).resolve()))
        wait(3.0)
        say("attached:", pathlib.Path(attach).name)

    # 4. hands-off rounds: send, wait, save files, then obey the reply's own
    #    "INCOMPLETE - continue from X" line until it stops asking.
    cfg = dict(max_wait=max_wait, cont_delay=cont_delay, rate_waits=rate_waits,
               stall_after=float(env("DS_STALL_AFTER", "45")),
               use_jev=env("DS_JEVA", "1") == "1")
    max_rounds = int(env("DS_FOLLOWUPS", "60"))

    sid = None
    baseline = 0          # assistant messages already present, so rounds cannot be confused
    rnd = 0
    msgs = []
    prompt = prompt or "continue"       # an attachment-only run still needs a send
    while True:
        rnd += 1
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
        baseline = len(assistant_texts(msgs))
        save_run(sid, msgs, outdir)
        written = save_file_blocks(msgs, savedir)
        if written:
            say(f"round {rnd}: saved {len(written)} file(s) ->", savedir)

        texts = assistant_texts(msgs)
        nxt = next_followup(texts[-1]) if texts else None
        if not nxt:
            say("reply is complete; nothing to follow up")
            break
        if rnd > max_rounds:
            say(f"follow-up limit ({max_rounds}) reached; stopping")
            break
        prompt = nxt
        say(f"auto follow-up: {nxt!r}")

    d = save_run(sid, msgs, outdir)
    say(f"done after {rnd} round(s)")
    say("chat id:", sid)
    say("transcript:", d / "transcript.md")
    say("files:", savedir)
# browser-harness execs this file inside its own module, so __name__ is never
# "__main__" here. Use an explicit opt-out so tests can import without running a chat.
if os.environ.get("DS_IMPORT_ONLY") != "1":
    main()
