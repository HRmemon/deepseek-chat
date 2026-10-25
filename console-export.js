// Paste into the DevTools console on an open chat.deepseek.com chat.
// Copies the whole conversation (thinking included) to the clipboard AND downloads it as
// markdown. Pure page script: uses the page's own API + the token already in localStorage.
(async () => {
  const sid = (location.href.match(/\/a\/chat\/s\/([0-9a-f-]{36})/) || [])[1];
  if (!sid) return console.error('no chat id in the URL - open a chat first');
  const tok = JSON.parse(localStorage.userToken).value;
  const r = await fetch(`/api/v0/chat/history_messages?chat_session_id=${sid}`,
                        { headers: { Authorization: 'Bearer ' + tok } });
  const p = await r.json();
  const msgs = p?.data?.biz_data?.chat_messages || [];
  if (!msgs.length) return console.error('no messages in response:', p);

  const body = msgs.map(m => {
    const parts = (m.fragments || []).map(f => {
      const kind = (f.type || '').toUpperCase(), text = f.content || '';
      if (kind === 'THINK') return `<details><summary>thinking</summary>\n\n${text}\n\n</details>`;
      if (kind === 'RESPONSE' || kind === 'REQUEST') return text;
      return `*[${kind}]*\n\n${text}`;
    }).filter(Boolean);
    return `## ${(m.role || '?').toLowerCase()}\n\n${parts.join('\n\n')}`;
  }).join('\n\n');

  const md = `# DeepSeek chat ${sid}\n\n_${new Date().toISOString()}_\n\n${body}\n`;
  await navigator.clipboard.writeText(md)
    .then(() => console.log('copied to clipboard (' + md.length + ' chars)'))
    .catch(e => console.warn('clipboard blocked, download still saved:', e.message));
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([md], { type: 'text/markdown' }));
  a.download = `deepseek-${sid.slice(0, 8)}.md`;
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
  console.log(msgs.length + ' messages exported');
})();
