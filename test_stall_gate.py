"""Regression checks for the hands-off loop: what to send next, and where files land.

No browser, no network, no model. Two things worth pinning:
  1. The reply's own "INCOMPLETE - continue from X" line becomes the next prompt, and the
     detection must be anchored — a code comment that merely mentions INCOMPLETE must not
     trigger a follow-up, and a reply that is finished must not either.
  2. The `=== FILE:` paths in a reply are model output, so they must never be able to write
     outside the save directory.
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

# --- what to send next -------------------------------------------------------
# The exact tails of the real replies in the DDIA chapter-3 chat.
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
assert ds.next_followup("INCOMPLETE - continue from scene 04\nprint(1)\n") is None

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

# --- attachments -------------------------------------------------------------
# comma-separated, blanks ignored, resolved to absolute paths
got = ds.parse_attach(" a.md , b.md ")
assert [pathlib.Path(g).name for g in got] == ["a.md", "b.md"], got
assert all(pathlib.Path(g).is_absolute() for g in got), got
assert ds.parse_attach("a.md,") == [str(pathlib.Path("a.md").resolve())]
assert ds.parse_attach("") == [] and ds.parse_attach(None) == []
assert len(ds.parse_attach("a.md,b.md,c.md,d.md")) == 4

print("ok: follow-up detection and file saving behave")
