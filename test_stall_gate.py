"""Regression check for the decision-model gate that decides whether to click.

The risk: the model confabulates a control when none resumes the answer. If the gate
lets that through, the automation clicks a random button. These are the actual answers
observed from jev-1.13-free for the three DeepSeek states.
"""
import os

os.environ["DS_IMPORT_ONLY"] = "1"

import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location(
    "deepseek_chat", pathlib.Path(__file__).with_name("deepseek_chat.py"))
ds = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ds)

CONTROLS = [{"index": 0, "label": "ok"}, {"index": 1, "label": "Copy"},
            {"index": 2, "label": "Continue"}, {"index": 3, "label": "Regenerate"}]
MIN_CONF = 0.8


def answer(choice, confidence):
    return {"action": {"type": "choice", "choice": choice, "confidence": confidence}}


# A real resume control, high confidence -> click it.
assert ds.resolve_choice(answer("2", 0.99), CONTROLS, MIN_CONF) == "Continue"
# Observed confabulations when no resume control exists -> must NOT click.
assert ds.resolve_choice(answer("0", 0.29), CONTROLS, MIN_CONF) is None
assert ds.resolve_choice(answer("3", 0.66), CONTROLS, MIN_CONF) is None
# "none" and out-of-range indices -> no click.
assert ds.resolve_choice(answer("none", 1.0), CONTROLS, MIN_CONF) is None
assert ds.resolve_choice(answer("9", 1.0), CONTROLS, MIN_CONF) is None
assert ds.resolve_choice(None, CONTROLS, MIN_CONF) is None
assert ds.resolve_choice({}, CONTROLS, MIN_CONF) is None

print("ok: gate accepts confident real controls, rejects confabulations")
