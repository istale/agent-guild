"""The two human-facing pages, both served by the platform.

    /chat   the external customer's window: they type, the CS agent answers.
            No login, no keys — a guest token held in the page.
    /ops    the system developer's monitor: every ticket, who is waiting on
            whom, and the full transcript including internal agent chatter
            that the customer never sees.

Both are single self-contained pages: no CDN, no build step, dark mode
included. /ops shows internal traffic, so bind the platform to your Tailnet.
"""
from __future__ import annotations

from fastapi.responses import HTMLResponse

_CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1c1b19;--dim:#6b6a66;--line:#e2e1dc;
 --cust:#3a5fb2;--cs:#1a7f52;--int:#8a4fb2;--warn:#b5741a;--bad:#b23a3a;}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){
 --bg:#17171a;--card:#202024;--ink:#eceae6;--dim:#9a9894;--line:#32323a;
 --cust:#87a5e8;--cs:#5cc294;--int:#c093e0;--warn:#e0a75a;--bad:#e88585;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 ui-sans-serif,
 -apple-system,"Segoe UI",sans-serif;padding:20px 16px 48px}
.wrap{max-width:780px;margin:0 auto}
h1{font-size:19px;margin:0 0 2px}.sub{color:var(--dim);font-size:13px;margin:0 0 18px}
.row{display:flex;gap:9px;align-items:center;flex-wrap:wrap}
.spread{justify-content:space-between}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
 padding:16px;margin-bottom:14px}
.pill{font-size:11px;padding:2px 9px;border-radius:99px;border:1px solid var(--line);
 color:var(--dim);white-space:nowrap}
.pill.open{color:var(--cs);border-color:currentColor}
.pill.waiting{color:var(--warn);border-color:currentColor}
.pill.resolved{color:var(--dim)}
.pill.high{color:var(--warn);border-color:currentColor}
.pill.human{color:var(--bad);border-color:currentColor}
.kb{border-left:3px solid var(--int);padding:6px 0 6px 12px;margin:8px 0;
 border-radius:0}
.turn{display:flex;gap:10px;padding:10px 0;border-top:1px solid var(--line)}
.avatar{width:28px;height:28px;border-radius:50%;flex:0 0 28px;color:#fff;
 display:grid;place-items:center;font-size:11px;font-weight:600}
.who{font-size:13px;font-weight:600}.meta{color:var(--dim);font-size:12px}
.text{margin-top:2px;white-space:pre-wrap}
.join{color:var(--dim);font-size:12px;padding:5px 0 5px 38px;
 border-top:1px solid var(--line)}
.note{color:var(--warn);font-style:italic;font-size:13px;padding:6px 0 6px 38px;
 border-top:1px solid var(--line)}
.empty{color:var(--dim);font-style:italic}
input,button{font:inherit;border-radius:8px;border:1px solid var(--line);
 padding:9px 12px;background:var(--card);color:var(--ink)}
input{flex:1;min-width:0}
button{cursor:pointer}button.go{background:var(--cs);border-color:var(--cs);color:#fff}
code{font-family:ui-monospace,Menlo,monospace;font-size:12px}
"""

_SHARED_JS = """
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>
 ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const clock=t=>new Date(t*1000).toLocaleTimeString();
const initials=n=>n.split(/\\s+/).map(w=>w[0]).join('').slice(0,2).toUpperCase();
const tint=u=>u.author_owner==='external'?'var(--cust)'
 :(u.author_name||'').toLowerCase().includes('support')?'var(--cs)':'var(--int)';
function turn(u){
 if(u.kind==='join') return `<div class="join">${esc(u.text)}</div>`;
 if(u.kind==='notice') return `<div class="note">${esc(u.text)}</div>`;
 return `<div class="turn">
  <div class="avatar" style="background:${tint(u)}">${esc(initials(u.author_name))}</div>
  <div style="flex:1;min-width:0"><div class="row">
   <span class="who">${esc(u.author_name)}</span>
   <span class="meta">${u.author_owner==='external'?'customer'
     :'for '+esc(u.author_owner)}</span>
   ${u.to?`<span class="pill">&rarr; ${esc(u.to)}</span>`:''}
   <span class="meta">${clock(u.created_at)}</span></div>
  <div class="text">${esc(u.text)}</div></div></div>`;
}
"""

CUSTOMER_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Support</title><style>__CSS__
.wrap{max-width:620px}
</style></head><body><div class="wrap">
<h1>Support</h1><p class="sub" id="sub">Tell us what happened.</p>
<div class="card" id="log"><p class="empty">No messages yet.</p></div>
<div class="row"><input id="msg" placeholder="Type your message…" autocomplete="off">
<button class="go" id="sendBtn">Send</button></div>
</div><script>__SHARED__
let room=null, token=null, since=0, history=[];
const q=new URLSearchParams(location.search);
const name=q.get('customer')||'Customer';
const box=document.getElementById('msg');
document.getElementById('sendBtn').addEventListener('click', send);
box.addEventListener('keydown', e => {
  if(e.key==='Enter' || e.keyCode===13){ e.preventDefault(); send(); }
});

function draw(){
  document.getElementById('log').innerHTML = history.length
    ? history.map(turn).join('')
    : '<p class="empty">No messages yet.</p>';
}
async function send(){
  const text=box.value.trim();
  if(!text) return; box.value='';
  if(!room){
    const r=await (await fetch('/tickets',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({customer:name,topic:text.slice(0,60),text})})).json();
    room=r.room_id; token=r.guest_token;
    document.getElementById('sub').textContent='Ticket '+room+' — an agent is picking this up.';
    poll();
    return;
  }
  await fetch(`/rooms/${room}/guest-utterances`,{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({token,text})});
}
async function poll(){
  while(room){
    let d; try{ d=await (await fetch(
      `/rooms/${room}?since=${since}&wait=25&view=customer`)).json(); }
    catch(e){ await new Promise(r=>setTimeout(r,2000)); continue; }
    for(const u of d.utterances){ since=Math.max(since,u.seq); history.push(u); }
    draw();
  }
}
</script></body></html>"""

OPS_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ops monitor</title><style>__CSS__
.stat{display:flex;gap:18px;flex-wrap:wrap;margin-bottom:16px}
.stat div{font-size:13px;color:var(--dim)}
.stat b{display:block;font-size:22px;font-weight:500;color:var(--ink)}
.filter{margin-bottom:14px}
.hidden{display:none}
</style></head><body><div class="wrap">
<div class="row spread"><div><h1>Ops monitor</h1>
<p class="sub" id="sub">connecting…</p></div>
<div class="row"><span class="pill" id="tick">—</span>
<button onclick="only='';load()">All</button>
<button onclick="only='waiting';load()">Waiting</button>
<button onclick="only='open';load()">Open</button></div></div>
<div class="stat" id="stat"></div>
<div id="kbcard"></div>
<div id="root"></div></div><script>__SHARED__
let only='';
function roomCard(r){
  const turns=r.utterances.map(turn).join('')
    || '<p class="empty">no turns yet</p>';
  return `<div class="card">
   <div class="row spread"><div>
     <div class="row"><span class="pill ${esc(r.status)}">${esc(r.status)}</span>
      ${r.priority==='high'?'<span class="pill high">priority</span>':''}
      ${r.needs_human?'<span class="pill human">needs a human</span>':''}
      <strong>${esc(r.topic)}</strong></div>
     <div class="meta"><code>${esc(r.room_id)}</code>
      ${r.customer?' · customer: '+esc(r.customer):''}
      ${r.waiting_on?' · waiting on '+esc(r.waiting_on):''}</div></div>
     <div class="meta">${r.said_count} turn(s)</div></div>
   <div class="row" style="margin-top:8px">${r.participants.map(p=>
     `<span class="pill">${esc(p.name)} · ${esc(p.owner)}</span>`).join('')}</div>
   <div style="margin-top:10px">${turns}</div></div>`;
}
async function load(){
  let d, kb={count:0,entries:[]};
  try{ d=await (await fetch('/rooms?with_utterances=1')).json();
       kb=await (await fetch('/knowledge')).json(); }
  catch(e){ document.getElementById('sub').textContent='platform unreachable';
    return; }
  const reused=kb.entries.reduce((n,e)=>n+e.used,0);
  const tickets=d.rooms.filter(r=>r.kind==='ticket');
  const waiting=tickets.filter(r=>r.status==='waiting');
  const open=tickets.filter(r=>r.status==='open');
  const needHuman=d.rooms.filter(r=>r.needs_human);
  document.getElementById('stat').innerHTML=
   `<div><b>${tickets.length}</b>tickets</div>
    <div><b>${open.length}</b>being served</div>
    <div><b>${waiting.length}</b>waiting on internal</div>
    <div><b>${needHuman.length}</b>need a human</div>
    <div><b>${kb.count}</b>answers on file</div>
    <div><b>${reused}</b>reused</div>`;
  document.getElementById('sub').textContent=
   `${d.rooms.length} room(s) · live`;
  document.getElementById('tick').textContent=new Date().toLocaleTimeString();
  document.getElementById('kbcard').innerHTML = kb.count
    ? `<div class="card"><h2 style="margin:0 0 8px">Answers on file</h2>`
      + kb.entries.slice(0,6).map(e=>`<div class="kb">
          <div class="meta">${esc(e.by_agent)} · for ${esc(e.by_human)}
            · reused ${e.used}&times;</div>
          <div>${esc(e.question)}</div>
          <div class="meta">&rarr; ${esc(e.answer)}</div></div>`).join('')
      + `</div>`
    : '';
  const shown=d.rooms.filter(r=>!only||r.status===only);
  document.getElementById('root').innerHTML= shown.length
    ? shown.slice().reverse().map(roomCard).join('')
    : '<div class="card"><p class="empty">nothing matches this filter</p></div>';
}
load(); setInterval(load,2000);
</script></body></html>"""


def _page(template: str) -> HTMLResponse:
    return HTMLResponse(template.replace("__CSS__", _CSS)
                        .replace("__SHARED__", _SHARED_JS))


def customer_page() -> HTMLResponse:
    return _page(CUSTOMER_PAGE)


def ops_page() -> HTMLResponse:
    return _page(OPS_PAGE)
