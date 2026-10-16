"""`ccr dashboard`: a self-contained HTML report over resolved history.

One file, no server, no network: the data is embedded as JSON and drawn with inline SVG.
Prompt text and the decision process appear only when `hooks.store_prompts` was on
(.ccr/local/prompts.jsonl); otherwise each task shows the signals kept in history.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ccr.config import TIERS
from ccr.history import prompts as prompt_log
from ccr.history import shadow, why
from ccr.history.learn import FAILURE, SUCCESS
from ccr.history.stats import compute

TASK_FIELDS = ("id", "ts", "tier", "class", "area", "outcome", "tokens", "source", "escalated_to")


def _task_row(r: dict, logged: dict) -> dict:
    row = {k: r.get(k) for k in TASK_FIELDS}
    row["full_id"] = str(r.get("id") or "")
    row["trace"] = r.get("trace") if isinstance(r.get("trace"), dict) else None
    row["confidence"] = r.get("confidence")
    row["risk"] = r.get("risk")
    row["decision"] = logged.get(row["full_id"])
    d = row["decision"]
    row["why"] = d["why"] if d and d.get("why") else None
    o = r.get("outcome")
    row["result"] = "ok" if o in SUCCESS else "fail" if o in FAILURE else "unknown"
    row["id"] = str(row["id"] or "")[:8]
    return row


def build_payload(records: list[dict], cfg, root: Path) -> dict:
    s = compute(records, cfg)
    tasks = [r for r in records if r.get("tier") in TIERS]
    tasks.sort(key=lambda r: r.get("ts") or 0)
    s["project"] = root.name
    s["generated"] = int(time.time())
    s["tier_order"] = list(TIERS)
    s["tier_models"] = {t: f"{cfg.tier(t).model}/{cfg.tier(t).effort}" for t in TIERS}
    logged = prompt_log.read(root)
    s["shadow"] = shadow.summarize(shadow.read(root))
    s["pricing"] = dict(cfg["pricing"])
    s["tier_family"] = {t: cfg.tier(t).model for t in TIERS}
    s["task_rows"] = [_task_row(r, logged) for r in tasks]
    for row in s["task_rows"]:
        if row["why"] is None:
            row["why"] = why.from_trace(row, s["tier_models"])
    s["prompts_logged"] = len(logged)
    s["store_prompts"] = bool(cfg["hooks"]["store_prompts"])
    return s


def render(payload: dict) -> str:
    data = json.dumps(payload, default=str).replace("</", "<\\/")
    return _TEMPLATE.replace("__DATA__", data)


def write(root: Path, records: list[dict], cfg, out: Path) -> Path:
    html = render(build_payload(records, cfg, root))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ccr Dashboard</title>
<style>
:root{color-scheme:light;--bg:#f4f5fa;--bg2:#eaecf6;--surface:#ffffff;--ink:#12131a;--ink2:#4a4d5e;--muted:#8a8da0;
--line:#e6e8f1;--shadow:0 1px 2px rgba(20,22,40,.04),0 8px 24px -12px rgba(20,22,60,.12);
--accent:#5b5bf0;--accent2:#8b5cf6;--good:#0f9d63;--bad:#d9433f;--warn:#c27a0a;
--t0:#cfe0fb;--t1:#a3c4f5;--t2:#6f9eec;--t2h:#4a7fdf;--t3:#3158b8;--t4:#1d2f78;
--c1:#5b5bf0;--c2:#f0713a;--c3:#17b585;--hero:linear-gradient(120deg,#4f46e5 0%,#7c3aed 55%,#c026d3 100%)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#0c0d14;--bg2:#12131d;--surface:#161824;--ink:#eef0f8;--ink2:#b4b8cc;--muted:#7d8198;--line:#262a3b;--shadow:0 1px 2px rgba(0,0,0,.3),0 10px 30px -14px rgba(0,0,0,.6);--accent:#7c7cff;--accent2:#a78bfa;--good:#34d399;--bad:#f87171;--warn:#fbbf24;
--t0:#26405f;--t1:#33598a;--t2:#4478b8;--t2h:#5f93d2;--t3:#86b1e6;--t4:#bcd6f6;--c1:#7c7cff;--c2:#f0713a;--c3:#2dd4a0;--hero:linear-gradient(120deg,#312e81 0%,#5b21b6 55%,#86198f 100%)}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#0c0d14;--bg2:#12131d;--surface:#161824;--ink:#eef0f8;--ink2:#b4b8cc;--muted:#7d8198;--line:#262a3b;--shadow:0 1px 2px rgba(0,0,0,.3),0 10px 30px -14px rgba(0,0,0,.6);--accent:#7c7cff;--accent2:#a78bfa;--good:#34d399;--bad:#f87171;--warn:#fbbf24;
--t0:#26405f;--t1:#33598a;--t2:#4478b8;--t2h:#5f93d2;--t3:#86b1e6;--t4:#bcd6f6;--c1:#7c7cff;--c2:#f0713a;--c3:#2dd4a0;--hero:linear-gradient(120deg,#312e81 0%,#5b21b6 55%,#86198f 100%)}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:radial-gradient(1200px 500px at 85% -10%,var(--bg2),transparent 60%),var(--bg);color:var(--ink);font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1240px;margin:0 auto;padding:20px 20px 64px}
header{background:var(--hero);color:#fff;border-radius:20px;padding:26px 28px;margin-bottom:20px;display:flex;flex-wrap:wrap;gap:16px;align-items:flex-end;justify-content:space-between;box-shadow:0 18px 40px -20px rgba(91,70,240,.55);position:relative;overflow:hidden}
header:after{content:"";position:absolute;right:-60px;top:-80px;width:280px;height:280px;border-radius:50%;background:radial-gradient(circle,rgba(255,255,255,.22),transparent 65%);pointer-events:none}
h1{margin:0;font-size:28px;font-weight:750;letter-spacing:-.6px}header .sub{color:rgba(255,255,255,.82);margin-top:4px}
h2{margin:0 0 2px;font-size:15px;font-weight:650;letter-spacing:-.1px}.sub{color:var(--ink2);font-size:13px}
.ctl{display:flex;gap:8px;align-items:center;flex-wrap:wrap;position:relative;z-index:1}
header label.sub{color:rgba(255,255,255,.85)}
select,button,input[type=search]{background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:7px 12px;font:inherit;cursor:pointer;transition:border-color .15s,box-shadow .15s,transform .05s}
select:hover,button:hover:not(:disabled),input[type=search]:hover{border-color:var(--accent)}
select:focus-visible,button:focus-visible,input:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button:active:not(:disabled){transform:translateY(1px)}
header select,header button{background:rgba(255,255,255,.16);color:#fff;border-color:rgba(255,255,255,.28);backdrop-filter:blur(6px)}
header select option{color:#111}
input[type=search]{min-width:240px;cursor:text}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-bottom:16px}
.tile,.card{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:16px 18px;box-shadow:var(--shadow)}
.tile{position:relative;overflow:hidden}.tile:before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:linear-gradient(var(--accent),var(--accent2))}
.tile:nth-child(2):before{background:linear-gradient(var(--good),var(--c3))}.tile:nth-child(3):before{background:linear-gradient(#0ea5e9,#38bdf8)}
.tile:nth-child(4):before{background:linear-gradient(var(--warn),#f59e0b)}.tile:nth-child(5):before{background:linear-gradient(var(--c2),#f59e0b)}
.tile .v{font-size:30px;font-weight:750;letter-spacing:-1px;margin-top:2px}.tile .l{color:var(--ink2);font-size:11.5px;text-transform:uppercase;letter-spacing:.07em;font-weight:600}
.tile .n{color:var(--muted);font-size:12px;margin-top:2px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px;margin-bottom:14px}.wide{grid-column:1/-1}
.card h2+.sub{margin-bottom:8px}
svg{display:block;width:100%;height:auto;overflow:visible}svg text{fill:var(--ink2);font:11px Inter,system-ui,sans-serif}
svg rect.bar{transform-box:fill-box;transform-origin:left center;animation:grow .55s cubic-bezier(.2,.8,.2,1) both}
svg rect.rise{transform-box:fill-box;transform-origin:center bottom;animation:rise .55s cubic-bezier(.2,.8,.2,1) both}
@keyframes grow{from{transform:scaleX(0)}}@keyframes rise{from{transform:scaleY(0)}}
@media (prefers-reduced-motion:reduce){svg rect.bar,svg rect.rise{animation:none}}
svg rect:hover{filter:brightness(1.12)}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin:10px 0 0;font-size:12px;color:var(--ink2)}
.sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
th{color:var(--ink2);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.05em}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;font-weight:600;border:1.5px solid var(--line);background:var(--bg)}
.ok{color:var(--good)}.fail{color:var(--bad)}.unk{color:var(--muted)}
#tip{position:fixed;pointer-events:none;background:#1b1d2b;color:#fff;padding:7px 10px;border-radius:9px;font-size:12px;opacity:0;transition:opacity .1s;z-index:9;max-width:270px;box-shadow:0 8px 24px rgba(0,0,0,.35)}
.empty{color:var(--muted);padding:28px 0;text-align:center}ul.tune{margin:8px 0 0;padding-left:18px;color:var(--ink2)}ul.tune li{margin:3px 0}
.note{color:var(--muted);font-size:12px;margin-top:10px}code{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:0 5px;font-size:12px}
.pager{display:flex;gap:14px;align-items:center;justify-content:center;margin-top:14px}button:disabled{opacity:.4;cursor:default}
details.dec{border:1px solid var(--line);border-radius:14px;margin:0 0 10px;background:var(--surface);transition:box-shadow .15s,border-color .15s}
details.dec:hover{box-shadow:var(--shadow)}details.dec[open]{border-color:var(--accent);box-shadow:var(--shadow)}
details.dec>summary{list-style:none;cursor:pointer;display:grid;grid-template-columns:auto auto 1fr auto auto;gap:12px;align-items:center;padding:11px 14px}
details.dec>summary::-webkit-details-marker{display:none}
.pr{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ink)}.pr.dim{color:var(--muted);font-style:italic}
.body{padding:6px 18px 16px;border-top:1px solid var(--line)}.body h3{margin:14px 0 6px;font-size:11.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--ink2)}
pre.pt{white-space:pre-wrap;word-break:break-word;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:12px;margin:0;font:12.5px/1.5 ui-monospace,"SF Mono",Menlo,monospace;max-height:220px;overflow:auto}
ol.steps{margin:0;padding-left:20px}ol.steps li{margin:3px 0}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:4px 16px;color:var(--ink2)}.kv b{color:var(--ink)}
.banner{background:var(--bg);border:1px dashed var(--line);border-radius:12px;padding:10px 14px;color:var(--ink2);font-size:13px;margin-bottom:12px}
.chain{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:2px 0 10px}.chain span{background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:4px 10px;font-size:12.5px;color:var(--ink)}
.chain i{font-style:normal;color:var(--muted)}.chain span:last-child{background:linear-gradient(120deg,var(--accent),var(--accent2));border-color:transparent;color:#fff;font-weight:650}
.chk{display:grid;grid-template-columns:28px 1fr auto;gap:2px 12px;padding:10px 0;border-top:1px solid var(--line)}.chk:first-of-type{border-top:0}
.chk .no{grid-row:span 3;width:24px;height:24px;border-radius:50%;background:var(--bg);border:1px solid var(--line);text-align:center;font-size:12px;line-height:22px;color:var(--ink2);font-weight:600}
.chk .q{font-weight:650}.chk .lk{color:var(--muted);font-size:12px;grid-column:2}.chk .fd{grid-column:2;color:var(--ink)}
.eff{grid-column:3;grid-row:1;align-self:start;white-space:nowrap;font-size:12px;border-radius:99px;padding:2px 11px;border:1px solid var(--line);color:var(--ink2);background:var(--bg)}
.eff.up{border-color:var(--warn);color:var(--warn);font-weight:650}.eff.down{border-color:var(--good);color:var(--good);font-weight:650}.eff.rule{border-color:var(--bad);color:var(--bad);font-weight:650}
.effnote{margin-top:8px;color:var(--ink2)}details.raw{margin-top:10px}details.raw summary{cursor:pointer;color:var(--muted);font-size:12px}
@media (max-width:640px){.wrap{padding:12px 12px 48px}header{padding:20px 18px}h1{font-size:23px}.grid{grid-template-columns:1fr}details.dec>summary{grid-template-columns:auto 1fr auto}details.dec>summary .hide-s{display:none}.chk{grid-template-columns:28px 1fr}.eff{grid-column:2;grid-row:auto;justify-self:start;margin-top:2px}}
.card>h2.sec{display:flex;align-items:center;gap:8px;cursor:pointer;user-select:none;border-radius:8px}
.card>h2.sec:hover{color:var(--accent)}.card>h2.sec:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.card>h2.sec:before{content:"";width:7px;height:7px;border-right:2px solid currentColor;border-bottom:2px solid currentColor;transform:rotate(45deg);transition:transform .18s;margin:-3px 4px 0 2px;flex:none}
.card.closed>h2.sec:before{transform:rotate(-45deg)}.card.closed .sec-body{display:none}.card.closed>h2.sec{margin-bottom:0}
.sec-body{margin-top:2px}
</style></head><body><div class="wrap">
<header><div><h1 id="title">ccr Dashboard</h1><div class="sub" id="meta"></div></div>
<input id="assume" type="hidden" value="50000"><div class="ctl"><label class="sub">Range <select id="range"><option value="0">All time</option><option value="7">7 days</option><option value="30">30 days</option><option value="90">90 days</option></select></label>
<button id="collapse" type="button">Collapse all</button><button id="expand" type="button">Expand all</button><button id="theme" type="button">Toggle theme</button></div></header>
<div class="tiles" id="tiles"></div>
<div class="grid">
<section class="card wide"><h2>Tasks per day, by tier</h2><div class="sub">Darker means a stronger model</div><div id="daily"></div><div class="legend" id="tierLegend"></div></section>
<section class="card"><h2>Success rate by tier</h2><div class="sub">Decided tasks only</div><div id="succ"></div></section>
<section class="card"><h2>Estimated cost</h2><div class="sub">Tokens priced by <code>pricing:</code>; an estimate</div><div id="cost"></div></section>
<section class="card"><h2 id="areaTitle">Tokens by area</h2><div id="area"></div></section>
<section class="card"><h2>Task class &times; tier</h2><div class="sub">Task counts</div><div id="heat"></div></section>
<section class="card wide"><h2>Routing health</h2><div id="health"></div></section>
<section class="card wide"><h2>Routing decisions</h2><div class="sub">Each task: the prompt, the tier chosen, and how ccr got there. Click a row to expand.</div>
<div class="ctl" style="margin:8px 0"><input id="q" type="search" placeholder="Search prompt, class, area, id" aria-label="Search"><select id="fres"><option value="">All results</option><option>ok</option><option>fail</option><option>unknown</option></select>
<select id="ftier"><option value="">All tiers</option></select><select id="psize"><option>10</option><option>25</option><option>50</option></select></div>
<div id="banner"></div><div id="tasks"></div><div class="pager"><button id="prev" type="button">&larr; Newer</button><span id="pginfo" class="sub"></span><button id="next" type="button">Older &rarr;</button></div></section>
</div><div class="note">Generated by <code>ccr dashboard</code>. Data comes from <code>.ccr/history.jsonl</code>; prompts appear only if <code>hooks.store_prompts</code> is on (local file, never committed).</div>
<div id="tip"></div></div>
<script>
const D=__DATA__;
const $=s=>document.querySelector(s),NS='http://www.w3.org/2000/svg';
const tierVar=t=>'var(--'+t.toLowerCase()+')';
const fk=n=>n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':String(Math.round(n));
const pct=v=>v==null?'-':Math.round(v*100)+'%';
const money=v=>'$'+(v>=100?v.toFixed(0):v.toFixed(2));
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtT=t=>new Date(t*1000).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
const tip=$('#tip');
function showTip(e,html){tip.innerHTML=html;tip.style.opacity=1;const x=Math.min(e.clientX+14,innerWidth-270);tip.style.left=x+'px';tip.style.top=(e.clientY+14)+'px'}
function hideTip(){tip.style.opacity=0}
function el(n,a,p){const e=document.createElementNS(NS,n);for(const k in a)e.setAttribute(k,a[k]);if(p)p.appendChild(e);return e}
function txt(p,x,y,s,a){const t=el('text',Object.assign({x,y},a||{}),p);t.textContent=s;return t}
function hover(node,html){node.addEventListener('mousemove',e=>showTip(e,html));node.addEventListener('mouseleave',hideTip)}
function svg(w,h){const s=document.createElementNS(NS,'svg');s.setAttribute('viewBox','0 0 '+w+' '+h);return s}
function mount(sel,node){const m=$(sel);m.innerHTML='';m.appendChild(node)}
function empty(sel,msg){$(sel).innerHTML='<div class="empty">'+(msg||'No data in this range')+'</div>'}

let rows=[];
function inRange(){const d=+$('#range').value;const cut=d?Date.now()/1000-d*86400:0;rows=D.task_rows.filter(r=>(r.ts||0)>=cut)}
const decided=r=>r.result!=='unknown';

function price(model){const p=D.pricing||{};for(const k in p)if(String(model).includes(k))return p[k];return 0}
function assumed(){return +($('#assume')?$('#assume').value:50000)}
function measured(){return rows.filter(r=>(r.tokens||0)>0).length}
function est(rs){
 const a=assumed();let routed=0,opus=0,sonnet=0,dr=0,do_=0,dn=0,dok=0;
 rs.forEach(r=>{const tok=(r.tokens||0)>0?r.tokens:a,c=tok*price(D.tier_family[r.tier])/1e6,o=tok*price('opus')/1e6;
  routed+=c;opus+=o;sonnet+=tok*price('sonnet')/1e6;
  if(r.result!=='unknown'){dn++;do_+=o;dr+=c;if(r.result==='ok')dok++}});
 return {routed,opus,sonnet,perOk:dok?dr/dok:null,opusPer:dn?do_/dn:null,so:opus?1-routed/opus:null,ss:sonnet?1-routed/sonnet:null}}
function tiles(){
 const n=rows.length,ok=rows.filter(r=>r.result==='ok').length,bad=rows.filter(r=>r.result==='fail').length;
 const m=measured(),tok=rows.reduce((a,r)=>a+(r.tokens||0),0),esc_=rows.filter(r=>r.escalated_to).length;
 const sr=ok+bad?ok/(ok+bad):null,e=est(rows);
 const T=[['Tasks',n,(D.runs||0)+' headless run(s)'],['Success rate',pct(sr),ok+' ok / '+bad+' failed / '+(n-ok-bad)+' unknown'],
 ['Tokens',m?fk(tok):'not measured',m?(m===n?fk(tok/n)+' per task':m+' of '+n+' tasks measured'):'no session transcript was available'],
 ['Escalated',n?pct(esc_/n):'-',esc_+' retried one tier up'],
 ['Saved vs all-Opus',e.so==null?'-':pct(e.so),money(e.routed)+' vs '+money(e.opus)+(m===n?'':' (assumes '+fk(assumed())+' tokens/task where unmeasured)')]];
 $('#tiles').innerHTML=T.map(t=>'<div class="tile"><div class="l">'+t[0]+'</div><div class="v"'+(t[1]==='not measured'?' style="font-size:18px;color:var(--muted)"':'')+'>'+t[1]+'</div><div class="n">'+t[2]+'</div></div>').join('');
}

function daily(){
 if(!rows.length)return empty('#daily');
 const day=ts=>{const d=new Date(ts*1000);return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')};
 const m={};rows.forEach(r=>{const k=day(r.ts||0);(m[k]=m[k]||{})[r.tier]=(m[k][r.tier]||0)+1});
 const days=Object.keys(m).sort(),W=1100,H=240,L=34,B=28,T=10,pw=W-L-8,ph=H-B-T;
 const tot=d=>Object.values(m[d]).reduce((a,b)=>a+b,0),mx=Math.max(...days.map(tot));
 const s=svg(W,H);
 for(let i=0;i<=4;i++){const v=Math.round(mx*i/4),y=T+ph-ph*i/4;el('line',{x1:L,x2:W-8,y1:y,y2:y,stroke:'var(--line)'},s);txt(s,L-6,y+4,v,{'text-anchor':'end'})}
 const bw=Math.min(48,pw/days.length*.7),step=pw/days.length;
 days.forEach((d,i)=>{let y=T+ph;const x=L+step*i+(step-bw)/2;
  D.tier_order.forEach(t=>{const c=m[d][t];if(!c)return;const h=ph*c/mx;y-=h;
   const r=el('rect',{x,y:y+1,width:bw,height:Math.max(h-2,1),rx:4,fill:tierVar(t),class:'rise'},s);
   hover(r,'<b>'+d+'</b><br>'+t+' ('+D.tier_models[t]+'): '+c+' task'+(c>1?'s':''))});
  if(days.length<=14||i%Math.ceil(days.length/12)===0)txt(s,x+bw/2,H-8,d.slice(5),{'text-anchor':'middle'})});
 mount('#daily',s);
 $('#tierLegend').innerHTML=D.tier_order.filter(t=>rows.some(r=>r.tier===t)).map(t=>'<span><i class="sw" style="background:'+tierVar(t)+'"></i>'+t+' '+D.tier_models[t]+'</span>').join('');
}

function hbars(sel,items,fmt,color){
 if(!items.length)return empty(sel);
 const W=420,rh=30,H=items.length*rh+4,L=150,mx=Math.max(...items.map(i=>i.v),1e-9),s=svg(W,H);
 items.forEach((it,i)=>{const y=i*rh+4,w=Math.max((W-L-52)*it.v/mx,it.v>0?3:0);
  txt(s,L-8,y+14,it.k.length>26?it.k.slice(0,25)+'…':it.k,{'text-anchor':'end'});
  const r=el('rect',{x:L,y,width:w,height:18,rx:6,fill:it.c||color,class:'bar'},s);
  txt(s,L+w+6,y+14,fmt(it.v),{});hover(r,'<b>'+esc(it.k)+'</b><br>'+(it.tip||fmt(it.v)))});
 mount(sel,s)}

function succ(){
 const items=D.tier_order.map(t=>{const rs=rows.filter(r=>r.tier===t&&decided(r));if(!rs.length)return null;
  const ok=rs.filter(r=>r.result==='ok').length;return {k:t+' · '+D.tier_models[t],v:ok/rs.length,c:tierVar(t),tip:ok+' of '+rs.length+' decided tasks succeeded'}}).filter(Boolean);
 hbars('#succ',items,pct,'var(--c1)')}

function cost(){
 if(!rows.length)return empty('#cost');
 const e=est(rows),m=measured(),n=rows.length;
 const items=[{k:'Routed (ccr)',v:e.routed,c:'var(--c1)'},{k:'All Sonnet',v:e.sonnet,c:'var(--c3)'},{k:'All Opus',v:e.opus,c:'var(--c2)'}];
 hbars('#cost',items,money,'var(--c1)');
 const d=document.createElement('div');d.className='note';
 d.innerHTML=(m<n?'<label>Assumed tokens per unmeasured task <select id="assume2"><option>20000</option><option>50000</option><option>100000</option><option>200000</option></select></label><br>':'')+
 'Saved '+pct(e.so)+' vs all-Opus, '+pct(e.ss)+' vs all-Sonnet (negative means routing cost more).'+
 (e.perOk!=null?'<br>Per success: '+money(e.perOk)+' routed vs '+money(e.opusPer)+' all-Opus.':'')+
 '<br>'+(m===n?'Estimate: measured tokens priced by <code>pricing:</code>.':(m?m+' of '+n+' tasks have measured tokens; the rest use the assumption.':'No tokens were measured, so every task uses the assumption: this shows the relative cost of the tier mix, not real spend.'));
 $('#cost').appendChild(d);
 const sel=$('#assume2');if(sel){sel.value=String(assumed());sel.onchange=()=>{$('#assume').value=sel.value;tiles();cost()}}}

function area(){
 const m={};rows.forEach(r=>{const k=r.area||'*';m[k]=m[k]||{t:0,n:0};m[k].t+=r.tokens||0;m[k].n++});
 const byTok=rows.some(r=>(r.tokens||0)>0);
 $('#areaTitle').textContent=byTok?'Tokens by area':'Tasks by area';
 const items=Object.entries(m).sort((a,b)=>byTok?b[1].t-a[1].t:b[1].n-a[1].n).slice(0,10)
  .map(([k,v])=>({k,v:byTok?v.t:v.n,tip:v.n+' task(s)'+(byTok?', '+fk(v.t)+' tokens':'')}));
 hbars('#area',items,byTok?fk:String,'var(--c1)')}

function heat(){
 const cls=[...new Set(rows.map(r=>r.class||'?'))].sort(),ts=D.tier_order.filter(t=>rows.some(r=>r.tier===t));
 if(!cls.length)return empty('#heat');
 const cw=72,rh=28,L=88,W=L+cw*ts.length+4,H=rh*cls.length+42,s=svg(W,H);
 const cnt={};rows.forEach(r=>{const k=(r.class||'?')+'|'+r.tier;cnt[k]=(cnt[k]||0)+1});
 const mx=Math.max(...Object.values(cnt));
 ts.forEach((t,j)=>{txt(s,L+cw*j+cw/2,13,t,{'text-anchor':'middle',style:'fill:var(--ink);font-weight:600'});txt(s,L+cw*j+cw/2,26,(D.tier_models[t]||'').replace('/',' '),{'text-anchor':'middle',style:'font-size:9px'})});
 cls.forEach((c,i)=>{txt(s,L-8,42+rh*i+17,c,{'text-anchor':'end'});
  ts.forEach((t,j)=>{const v=cnt[c+'|'+t]||0;
   const r=el('rect',{x:L+cw*j+1,y:38+rh*i+1,width:cw-2,height:rh-2,rx:4,fill:v?'var(--c1)':'var(--line)','fill-opacity':v?.18+.82*v/mx:.35},s);
   if(v){txt(s,L+cw*j+cw/2,38+rh*i+18,v,{'text-anchor':'middle',style:'fill:var(--ink);font-weight:600'});hover(r,'<b>'+c+' at '+t+'</b><br>'+v+' task(s)')}})});
 mount('#heat',s)}

function health(){
 const mis=D.misroutes||{},L=D.learning||[];let h='';
 if(mis.decided){h+='<div class="sub">Over-routed '+mis.over+'/'+mis.decided+' ('+pct(mis.over_rate)+') · Under-routed '+mis.under+'/'+mis.decided+' ('+pct(mis.under_rate)+')</div>';
  if(mis.suggestions&&mis.suggestions.length)h+='<ul class="tune">'+mis.suggestions.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>';
  if(mis.items&&mis.items.length)h+='<table style="margin-top:10px"><tr><th>Task</th><th>Kind</th><th>Tier</th><th>Class</th><th>Area</th><th>Why</th></tr>'+mis.items.slice(0,15).map(i=>'<tr><td>'+esc(String(i.id||'').slice(0,8))+'</td><td>'+i.kind+'</td><td>'+i.tier+' <span class="sub">'+esc((D.tier_models[i.tier]||'').replace('/',' '))+'</span></td><td>'+esc(i.class)+'</td><td>'+esc(i.area||'*')+'</td><td>'+esc(i.why)+'</td></tr>').join('')+'</table>'+(mis.items.length>15?'<div class="note">Showing 15 of '+mis.items.length+'. Run <code>ccr stats --misroutes</code> for the full list.</div>':'');
 }else h+='<div class="sub">No decided tasks yet, so misroutes cannot be measured.</div>';
 if(D.manual_escalations||D.possible_reworks)h+='<div class="sub" style="margin-top:6px">'+D.manual_escalations+' manual move(s) to a stronger model and '+D.possible_reworks+' possible rework(s) after a cheap answer, counted against the tier they came from.</div>';
 const sh=D.shadow||{n:0};
 h+='<h2 style="margin-top:14px">Would a cheaper tier have done it?</h2>'+(sh.n?'<div class="sub">'+sh.ok+' of '+sh.n+' sampled deep-tier tasks also succeeded at a lower tier ('+Math.round(sh.rate*100)+'%); '+sh.verified_ok+'/'+sh.verified_n+' verified by tests. '+Object.entries(sh.pairs).map(([k,v])=>k+' '+v.ok+'/'+v.n).join(', ')+'.</div>':'<div class="sub">Not measured. <code>ccr shadow --max-budget-usd 1 --yes</code> re-runs a few deep-tier tasks at a lower tier in a throwaway worktree (uses your claude usage).</div>');
 const act=L.filter(x=>x.action!=='watch');
 h+='<h2 style="margin-top:14px">Learning</h2>'+(act.length?'<ul class="tune">'+act.map(x=>'<li><b>'+esc(x.action)+'</b> '+esc(x.note)+'</li>').join('')+'</ul>':'<div class="sub">No adjustments yet. Learning starts after a few decided tasks per class and area (see <code>history:</code> in config).</div>');
 $('#health').innerHTML=h}

let page=0;
const RES={ok:['✓ ok','ok'],fail:['✗ failed','fail'],unknown:['? unknown','unk']};
function steps(r){
 const d=r.decision,t=r.trace||{};
 if(d){
  const s=[];
  s.push('Classified as <b>'+esc(d.class.kind)+'</b> ('+esc(d.class.source)+', confidence '+d.class.confidence.toFixed(2)+(d.class.secondary?', runner-up '+esc(d.class.secondary):'')+'); scope '+esc(d.class.scope)+', vagueness '+d.class.vagueness.toFixed(2)+(d.class.evidence.length?'. Matched: '+d.class.evidence.map(esc).join(', '):'')+'.');
  s.push('Starting tier for this class: <b>'+esc(d.base)+'</b>.');
  s.push(d.files.length?'Located '+d.files.length+' candidate file(s): '+d.files.map(f=>'<code>'+esc(f.path)+'</code> ('+f.confidence.toFixed(2)+')').join(', ')+'.':'No specific files located.');
  if(d.risk&&d.risk.score!=null)s.push('Risk <b>'+d.risk.score.toFixed(2)+' ('+esc(d.risk.level)+')</b>: '+Object.entries(d.risk.components).map(([k,c])=>k.replace(/_/g,' ')+' '+c.value.toFixed(2)).join(', ')+'.');
  d.reasons.forEach(x=>s.push(esc(x)));
  if(d.learning&&d.learning!=='none')s.push('Learning: '+esc(d.learning));
  if(d.budget)s.push('Budget: '+esc(d.budget));
  d.notes.forEach(x=>s.push(esc(x)));
  if(d.t4&&d.t4.length)s.push('T4 rule: '+d.t4.map(esc).join('; '));
  s.push('Final: <b>'+esc(d.tier)+'</b> ('+esc(d.model)+'/'+esc(d.effort)+(d.retry?', retry after a failure':'')+').');
  return s}
 if(!r.trace)return ['No decision details were recorded for this task (older history).'];
 const s=['Class <b>'+esc(r.class||'?')+'</b> (confidence '+(t.class_conf??'?')+', '+esc(t.class_source||'rules')+'); scope '+esc(t.scope||'?')+', vagueness '+(t.vagueness??'?')+'.'];
 if(t.base)s.push('Starting tier for the class: <b>'+esc(t.base)+'</b>.');
 if((t.rules||[]).length)s.push('Cue words that fired: '+t.rules.map(esc).join(', ')+'.');
 if((t.hard_terms||[]).length)s.push('Hard-problem words: '+t.hard_terms.map(esc).join(', ')+'.');
 if((t.bumps||[]).length)s.push('Moved up a tier for: '+t.bumps.map(esc).join(', ')+'.');
 s.push((t.located?'Code located':'No code located')+'; risk '+esc(t.risk_level||'unknown')+(t.code_risk!=null?' (code-only '+t.code_risk+')':'')+'.');
 if((t.critical||[]).length)s.push('Critical path: '+t.critical.map(esc).join(', ')+'.');
 if((t.memory_flags||[]).length)s.push('Memory flags: '+t.memory_flags.map(esc).join(', ')+'.');
 if(t.t4)s.push('A T4 rule applied.');
 if(t.budget_pressure)s.push('Weekly budget pressure lowered the tier.');
 if(r.escalated_to)s.push('It failed here and was retried at '+esc(r.escalated_to)+'.');
 s.push('Final: <b>'+esc(r.tier)+'</b>.');return s}
const ARROW={up:'↑ ',down:'↓ ',rule:'⚑ ',none:'= '};
function why(r){
 const w=r.why;if(!w||!w.checks.length)return '<div class="sub">No decision details were recorded for this task.</div>';
 let h='<div class="chain">'+w.chain.map(c=>'<span>'+esc(c)+'</span>').join('<i>→</i>')+'</div>';
 h+=w.checks.map(c=>'<div class="chk"><div class="no">'+c.n+'</div><div class="q">'+esc(c.title)+'</div><div class="eff '+c.move+'">'+ARROW[c.move]+esc(c.effect)+'</div>'+
  '<div class="lk">Looked at: '+esc(c.looked_at)+'</div><div class="fd">Found: '+esc(c.found)+'</div></div>').join('');
 if(w.partial)h+='<div class="sub" style="margin-top:6px">Older task: only the saved routing signals are available. Turn on <code>hooks.store_prompts</code> for the full checklist on new tasks.</div>';
 return h+'<div class="effnote">'+esc(w.effort_note)+'</div>'}
function card(r){
 const d=r.decision,[lab,cls]=RES[r.result],pr=d?d.prompt:null;
 const sum='<summary><span class="sub">'+fmtT(r.ts)+'</span><span class="pill" title="'+esc(D.tier_models[r.tier]||'')+'" style="border-color:'+tierVar(r.tier)+'">'+r.tier+' <small style="color:var(--muted);font-weight:500">'+esc((D.tier_models[r.tier]||'').replace('/',' '))+'</small></span>'+
  '<span class="pr'+(pr?'':' dim')+'">'+(pr?esc(pr.replace(/\s+/g,' ').slice(0,140)):'prompt not stored · '+esc(r.class||'?')+' in '+esc(r.area||'*'))+'</span>'+
  '<span class="'+cls+'">'+lab+(r.escalated_to?' → '+r.escalated_to:'')+'</span><span class="sub hide-s">'+fk(r.tokens||0)+'</span></summary>';
 const body='<div class="body"><h3>Prompt</h3>'+(pr?'<pre class="pt">'+esc(pr)+'</pre>':'<div class="sub">Not stored. Set <code>hooks.store_prompts: true</code> in <code>.ccr/config.yaml</code> to keep prompts for new tasks.</div>')+
  '<h3>What ccr checked before choosing '+r.tier+' ('+esc(D.tier_models[r.tier]||'')+')</h3>'+why(r)+
  '<details class="raw"><summary>Raw engine notes</summary><ol class="steps">'+steps(r).map(x=>'<li>'+x+'</li>').join('')+'</ol></details>'+
  '<h3>Details</h3><div class="kv"><span>ID <b>'+esc(r.full_id.slice(0,12))+'</b></span><span>Class <b>'+esc(r.class||'?')+'</b></span><span>Area <b>'+esc(r.area||'*')+'</b></span><span>Model <b>'+esc(D.tier_models[r.tier]||'')+'</b></span><span>Tokens <b>'+fk(r.tokens||0)+'</b></span><span>Source <b>'+esc(r.source||'')+'</b></span>'+(r.confidence!=null?'<span>Confidence <b>'+Number(r.confidence).toFixed(2)+'</b></span>':'')+'</div></div>';
 return '<details class="dec">'+sum+body+'</details>'}
function tasks(reset){
 if(reset)page=0;
 const q=$('#q').value.toLowerCase().trim(),f=$('#fres').value,ft=$('#ftier').value,size=+$('#psize').value;
 $('#banner').innerHTML=D.store_prompts?'':'<div class="banner">Prompt text is off. Showing the routing signals kept in history. Enable <code>hooks.store_prompts: true</code> to see prompts and the full decision steps for new tasks (stored locally in <code>.ccr/local/prompts.jsonl</code>, never committed).</div>';
 const hay=r=>[r.decision&&r.decision.prompt,r.class,r.area,r.full_id,r.tier].join(' ').toLowerCase();
 const rs=rows.filter(r=>(!f||r.result===f)&&(!ft||r.tier===ft)&&(!q||hay(r).includes(q))).slice().reverse();
 const pages=Math.max(1,Math.ceil(rs.length/size));page=Math.min(page,pages-1);
 $('#tasks').innerHTML=rs.length?rs.slice(page*size,page*size+size).map(card).join(''):'<div class="empty">No tasks match</div>';
 $('#pginfo').textContent='Page '+(page+1)+' of '+pages+' · '+rs.length+' task'+(rs.length===1?'':'s');
 $('#prev').disabled=page<=0;$('#next').disabled=page>=pages-1}
function draw(){inRange();tiles();daily();succ();cost();area();heat();health();tasks(true)}
$('#title').textContent=D.project;document.title=D.project+' · ccr dashboard';
$('#meta').textContent='ccr dashboard · '+D.tasks+' task(s)'+(D.since?' · '+fmtT(D.since)+' to '+fmtT(D.until):'')+' · generated '+fmtT(D.generated);
$('#range').onchange=draw;
$('#ftier').innerHTML+=D.tier_order.map(t=>'<option value="'+t+'">'+t+' · '+D.tier_models[t].replace('/',' ')+'</option>').join('');
['#fres','#ftier','#psize'].forEach(i=>$(i).onchange=()=>tasks(true));$('#q').oninput=()=>tasks(true);
$('#prev').onclick=()=>{page--;tasks()};$('#next').onclick=()=>{page++;tasks()};
$('#theme').onclick=()=>{const r=document.documentElement,dark=r.dataset.theme?r.dataset.theme==='dark':matchMedia('(prefers-color-scheme:dark)').matches;r.dataset.theme=dark?'light':'dark'};
function secs(){return [...document.querySelectorAll('section.card')]}
function secKey(c){return c.querySelector('h2').textContent.trim()}
function loadClosed(){try{return JSON.parse(localStorage.getItem('ccr-closed')||'[]')}catch(e){return []}}
function saveClosed(){try{localStorage.setItem('ccr-closed',JSON.stringify(secs().filter(c=>c.classList.contains('closed')).map(secKey)))}catch(e){}}
function setOpen(c,open){c.classList.toggle('closed',!open);c.querySelector('h2').setAttribute('aria-expanded',open)}
(function initSections(){
 const closed=loadClosed();
 secs().forEach(c=>{const h=c.querySelector('h2');if(!h)return;h.classList.add('sec');h.setAttribute('role','button');h.tabIndex=0;
  const body=document.createElement('div');body.className='sec-body';
  while(h.nextSibling)body.appendChild(h.nextSibling);c.appendChild(body);
  setOpen(c,!closed.includes(secKey(c)));
  const toggle=()=>{setOpen(c,c.classList.contains('closed'));saveClosed()};
  h.addEventListener('click',toggle);
  h.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();toggle()}})});
 $('#collapse').onclick=()=>{secs().forEach(c=>setOpen(c,false));saveClosed()};
 $('#expand').onclick=()=>{secs().forEach(c=>setOpen(c,true));saveClosed()};
})();
draw();
</script></body></html>
"""
