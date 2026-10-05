"""Upload failures must never be reported as a sent prompt. No browser/network."""
import importlib.util
import os
from pathlib import Path
import tempfile

os.environ['DS_IMPORT_ONLY']='1'
spec=importlib.util.spec_from_file_location('ds',Path(__file__).with_name('deepseek_chat.py'))
ds=importlib.util.module_from_spec(spec)
spec.loader.exec_module(ds)
ready={'sendEnabled':True,'items':[{'count':1,'failed':False,'pending':False}]}
assert ds.attachments_ready(ready)
for field,value in [('failed',True),('pending',True),('count',0),('count',2)]:
    bad={'sendEnabled':True,'items':[dict(ready['items'][0],**{field:value})]}
    assert not ds.attachments_ready(bad)
assert not ds.attachments_ready(dict(ready,sendEnabled=False))

os.environ['DS_UPLOAD_WAIT']='0'
ds.wait=lambda _:None
ds.set_composer=lambda _:True
ds.say=lambda *a:None
ds.upload_file=lambda *a:None
removed=[]
def click(x,y):
    removed.append((x,y))
ds.click_at_xy=click

def state(names):
    return {'sendEnabled':False,'items':[{'name':name,'count':0 if removed else 1,
        'text':'Server busy','failed':True,'pending':False,
        'remove':None if removed else {'x':1,'y':2},'retry':None} for name in names]}
ds.attachment_state=state
with tempfile.TemporaryDirectory() as d:
    p=Path(d)/'repair.md';p.write_text('Fix the moving scan cursor.')
    result=ds.ensure_attachments([str(p)],'Apply the repair.')
    assert 'Fix the moving scan cursor.' in result and 'repair.md' in result
    assert removed==[(1,2)]
    removed.clear()
    p=Path(d)/'source.pdf';p.write_bytes(b'%PDF')
    try:
        ds.ensure_attachments([str(p)],'Read source.')
    except RuntimeError as e:
        assert 'not ready' in str(e)
    else:
        raise AssertionError('failed PDF upload must block, not be silently omitted')
    assert not removed

ds.js=lambda _:1
ds.click_send=lambda:False
try:
    ds.send_prompt('repair')
except RuntimeError as e:
    assert 'NOT sent' in str(e)
else:
    raise AssertionError('disabled Send must fail immediately')
print('upload gate checks passed')
