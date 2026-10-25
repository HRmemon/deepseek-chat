"""Offline context-limit regression: immediate stop, preserved files, durable error."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

os.environ['DS_IMPORT_ONLY'] = '1'
spec = importlib.util.spec_from_file_location('ds', Path(__file__).with_name('deepseek_chat.py'))
ds = importlib.util.module_from_spec(spec); spec.loader.exec_module(ds)
for text in ('Length limit reached', 'This conversation has reached its maximum length',
             'Context limit exceeded', '当前对话已达到长度上限'):
    assert ds.CONTEXT_LIMIT_RE.search(text), text
assert not ds.CONTEXT_LIMIT_RE.search('too many requests; please wait')
assert not ds.CONTEXT_LIMIT_RE.search('upload file size limit reached')

message = {'role': 'ASSISTANT', 'fragments': [{'type': 'RESPONSE',
    'content': '=== FILE: scene17.html\n```html\n<template>partial</template>\n```'}]}
with patch.object(ds, 'history', return_value={}), \
     patch.object(ds, 'summarize', return_value=([message], 'Length limit reached')), \
     patch.object(ds, 'js', side_effect=AssertionError('API error should short circuit DOM'), create=True), \
     patch.object(ds, 'resume_control', side_effect=AssertionError('must not continue full chat')):
    try:
        ds.wait_for_answer('old-chat', 0, {'max_wait': 30, 'rate_waits': []})
    except ds.ContextLimit as error:
        assert error.sid == 'old-chat' and error.messages == [message]
    else:
        raise AssertionError('must stop immediately')

with patch.object(ds, 'js', return_value='Length limit reached', create=True):
    assert ds.context_limit_evidence() == 'Length limit reached'
with patch.object(ds, 'js', return_value='', create=True):
    assert ds.context_limit_evidence() is None

with tempfile.TemporaryDirectory() as directory, \
     patch.dict(os.environ, {'DS_OUTDIR': directory, 'DS_SAVE_DIR': directory+'/files'}), \
     patch.object(ds, 'save_run', return_value=Path(directory)):
    ds.record_context_limit(ds.ContextLimit('old-chat', [message], 'Length limit reached'))
    state = json.loads((Path(directory)/'state.json').read_text())
    assert state['state'] == 'context_limit'
    assert state['recovery'] == 'fresh_chat_with_full_inputs'
    assert (Path(directory)/'files/scene17.html').exists()
    assert json.loads((Path(directory)/'errors.jsonl').read_text())['chat'] == 'old-chat'
print('context-limit checks: ok')
