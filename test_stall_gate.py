"""Regression checks for the hands-off loop: what to send next, and where files land.

Two risks worth pinning without a browser:
  1. The decision model confabulates a control when none resumes the answer, so the
     confidence gate must reject it. If it lets one through, the automation clicks a
     random button. These are actual answers observed from jev-1.13-free.
  2. The reply's own "INCOMPLETE - continue from X" line becomes the next prompt, and
     the `=== FILE:` paths in a reply are model output, so they must never be able to
     write outside the save directory.
"""
import os

os.environ["DS_IMPORT_ONLY"] = "1"

import importlib.util
import pathlib
import tempfile

spec = importlib.util.spec_from_file_location(
    "deepseek_chat", pathlib.Path(__file__).with_name("deepseek_chat.py"))
ds = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ds)

# --- confidence gate ---------------------------------------------------------
CONTROLS = [{"index": 0, "label": "ok"}, {"index": 1, "label": "Copy"},
            {"index": 2, "label": "Continue"}, {"index": 3, "label": "Regenerate"}]
MIN_CONF = 0.8


def answer(choice, confidence):
    return {"action": {"type": "choice", "choice": choice, "confidence": confidence}}


assert ds.resolve_choice(answer("2", 0.99), CONTROLS, MIN_CONF) == "Continue"
assert ds.resolve_choice(answer("0", 0.29), CONTROLS, MIN_CONF) is None   # confabulated
assert ds.resolve_choice(answer("3", 0.66), CONTROLS, MIN_CONF) is None   # confabulated
assert ds.resolve_choice(answer("none", 1.0), CONTROLS, MIN_CONF) is None
assert ds.resolve_choice(answer("9", 1.0), CONTROLS, MIN_CONF) is None
assert ds.resolve_choice(None, CONTROLS, MIN_CONF) is None
assert ds.resolve_choice({}, CONTROLS, MIN_CONF) is None

# --- what to send next -------------------------------------------------------
# The exact tails of the eight real replies in the DDIA chapter-3 chat.
for marker, expected in [
    ("INCOMPLETE \u2014 continue from scene 04", "continue from scene 04"),
    ("INCOMPLETE \u2013 continue from scene 51", "continue from scene 51"),
    ("INCOMPLETE - continue from scene 33", "continue from scene 33"),
    ("INCOMPLETE \u2014 continue from scene 72", "continue from scene 72"),
]:
    assert ds.next_followup(f"```\ncode\n```\n\n{marker}") == expected
assert ds.next_followup("```\ncode\n```\n\nAll 72 scenes complete.") is None
assert ds.next_followup("") is None
assert ds.next_followup(None) is None
# a bare marker still asks to be continued
assert ds.next_followup("some text\n\nINCOMPLETE") == "continue"
# the word appearing earlier in a code comment must not trigger a follow-up
assert ds.next_followup("# INCOMPLETE handling\nprint(1)\n") is None

# --- file blocks -------------------------------------------------------------
SAMPLE = (
    "Here you go.\n"
    "=== FILE: /home/someone/proj/manim/ch03/scene01.py\n"
    "```python\nprint(1)\n```\n\n"
    "=== FILE: /tmp/elsewhere/scene02.py\n"
    "```python\nprint(2)\n```\n"
)
blocks = ds.parse_file_blocks(SAMPLE)
assert blocks == [("/home/someone/proj/manim/ch03/scene01.py", "print(1)\n"),
                  ("/tmp/elsewhere/scene02.py", "print(2)\n")], blocks
assert ds.parse_file_blocks("no blocks here") == []

with tempfile.TemporaryDirectory() as tmp:
    saved = ds.save_file_blocks([{"role": "ASSISTANT",
                                  "fragments": [{"type": "RESPONSE", "content": SAMPLE}]}],
                                pathlib.Path(tmp))
    names = sorted(p.name for p in saved)
    # basenames only: the declared absolute paths must not be honoured
    assert names == ["scene01.py", "scene02.py"], names
    assert (pathlib.Path(tmp) / "scene01.py").read_text() == "print(1)\n"
    # a second pass over identical content rewrites nothing
    again = ds.save_file_blocks([{"role": "ASSISTANT",
                                  "fragments": [{"type": "RESPONSE", "content": SAMPLE}]}],
                                pathlib.Path(tmp))
    assert again == [], again

print("ok: confidence gate, follow-up detection and file saving all behave")
