"""Plantillas HTML autocontenidas (el grafo interactivo).

Extraido de soou.py (que lo re-exporta: `soou.<nombre>` sigue valiendo)."""




GRAPH_TEMPLATE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Grafo del cerebro</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family: ui-sans-serif, system-ui, sans-serif;
         background:#0e1116; color:#e6edf3; overflow:hidden; }}
  #hud {{ position:fixed; top:14px; left:14px; z-index:10; background:#161b22cc;
          backdrop-filter:blur(6px); border:1px solid #30363d; border-radius:12px;
          padding:14px 16px; min-width:210px; }}
  #hud h1 {{ font-size:14px; margin:0 0 10px; letter-spacing:.02em; }}
  .row {{ display:flex; justify-content:space-between; font-size:13px; margin:3px 0; }}
  .row b {{ font-variant-numeric: tabular-nums; }}
  .bar {{ height:6px; background:#21262d; border-radius:4px; overflow:hidden; margin-top:8px; }}
  .bar > span {{ display:block; height:100%; background:linear-gradient(90deg,#3fb950,#2ea043); }}
  .legend {{ display:flex; gap:12px; margin-top:12px; font-size:12px; }}
  .legend i {{ width:10px; height:10px; border-radius:50%; display:inline-block; margin-right:5px; }}
  .muted {{ color:#7d8590; font-size:11px; margin-top:10px; }}
  #tip {{ position:fixed; pointer-events:none; background:#161b22; border:1px solid #30363d;
          border-radius:8px; padding:6px 9px; font-size:12px; opacity:0; z-index:20; white-space:nowrap; }}
  canvas {{ display:block; }}
</style></head><body>
<div id="hud">
  <h1>🧠 Nivel de ingesta</h1>
  <div class="row"><span>Notas</span><b>{total}</b></div>
  <div class="row"><span>Aristas</span><b>{edges}</b></div>
  <div class="row"><span>Sintetizadas</span><b>{synth}</b></div>
  <div class="row"><span>Placeholders</span><b>{ph}</b></div>
  <div class="bar"><span style="width:{pct}%"></span></div>
  <div class="row" style="margin-top:6px"><span>Sintetizado</span><b>{pct}%</b></div>
  <div class="legend">
    <span><i style="background:#58a6ff"></i>code {code}</span>
    <span><i style="background:#bc8cff"></i>personal {personal}</span>
    <span><i style="background:#f0883e"></i>work {work}</span>
  </div>
  <div class="muted">Generado {gen} · arrastra nodos · scroll = zoom · doble clic = centrar</div>
</div>
<div id="tip"></div>
<canvas id="c"></canvas>
<script>
const DATA = {data};
const COLOR = {{code:'#58a6ff', personal:'#bc8cff', work:'#f0883e'}};
const cv = document.getElementById('c'), ctx = cv.getContext('2d');
let W, H, DPR = devicePixelRatio || 1;
function resize() {{ W = cv.width = innerWidth*DPR; H = cv.height = innerHeight*DPR;
  cv.style.width = innerWidth+'px'; cv.style.height = innerHeight+'px'; }}
resize(); addEventListener('resize', resize);

const NR = Math.min(60*Math.sqrt(DATA.nodes.length||1), Math.min(innerWidth,innerHeight)*0.4);
const idx = {{}}; DATA.nodes.forEach((n,i)=>{{ idx[n.id]=n;
  let ang=Math.random()*6.283, rad=Math.sqrt(Math.random())*NR;
  n.x = innerWidth/2 + Math.cos(ang)*rad; n.y = innerHeight/2 + Math.sin(ang)*rad;
  n.vx = 0; n.vy = 0; n.r = 5 + Math.min(14, n.indeg*2.2); }});
const E = DATA.edges.filter(e=>idx[e.source]&&idx[e.target]);

let view = {{x:0, y:0, k:1}};
let drag=null, pan=null;   // declarados ANTES del loop: tick() los usa desde el 1er frame
function tick() {{
  for (const a of DATA.nodes) for (const b of DATA.nodes) {{ if (a===b) continue;
    let dx=a.x-b.x, dy=a.y-b.y, d2=dx*dx+dy*dy; if (d2<1) d2=1;
    let d=Math.sqrt(d2), f=Math.min(1.5, 1600/d2);
    a.vx += dx/d*f; a.vy += dy/d*f; }}
  for (const e of E) {{ let s=idx[e.source], t=idx[e.target];
    let dx=t.x-s.x, dy=t.y-s.y, d=Math.sqrt(dx*dx+dy*dy)+.01, f=(d-70)*0.02;
    s.vx += dx/d*f; s.vy += dy/d*f; t.vx -= dx/d*f; t.vy -= dy/d*f; }}
  const cx=innerWidth/2, cy=innerHeight/2;
  for (const n of DATA.nodes) {{ if (n===drag) continue;
    n.vx += (cx-n.x)*0.003; n.vy += (cy-n.y)*0.003;
    n.vx*=0.85; n.vy*=0.85;
    n.vx=Math.max(-25,Math.min(25,n.vx)); n.vy=Math.max(-25,Math.min(25,n.vy));
    n.x+=n.vx; n.y+=n.vy; }}
}}
function draw() {{
  ctx.setTransform(DPR,0,0,DPR,0,0);
  ctx.fillStyle='#0e1116'; ctx.fillRect(0,0,innerWidth,innerHeight);   // fondo explicito
  ctx.save();
  ctx.translate(view.x, view.y); ctx.scale(view.k, view.k);
  ctx.lineWidth = 1/view.k; ctx.strokeStyle = '#30363d';
  for (const e of E) {{ let s=idx[e.source], t=idx[e.target];
    ctx.beginPath(); ctx.moveTo(s.x,s.y); ctx.lineTo(t.x,t.y); ctx.stroke(); }}
  for (const n of DATA.nodes) {{
    ctx.beginPath(); ctx.arc(n.x,n.y,Math.max(2,n.r),0,7);
    ctx.fillStyle = n.placeholder ? '#484f58' : (COLOR[n.area]||'#8b949e');
    ctx.fill();
    if (view.k > 0.7) {{ ctx.fillStyle='#c9d1d9'; ctx.font='11px sans-serif';
      ctx.fillText(n.id, n.x+n.r+3, n.y+3); }}
  }}
  ctx.restore();
  ctx.fillStyle='#3fb950'; ctx.font='13px sans-serif';   // contador de control (siempre visible si el canvas pinta)
  ctx.fillText(DATA.nodes.length+' nodos · '+E.length+' aristas', 16, innerHeight-16);
}}
function fitView() {{
  if (!DATA.nodes.length) return;
  let a=1e9,b=1e9,c=-1e9,e=-1e9;
  for (const n of DATA.nodes) {{ a=Math.min(a,n.x); c=Math.max(c,n.x); b=Math.min(b,n.y); e=Math.max(e,n.y); }}
  let w=(c-a)||1, h=(e-b)||1, k=Math.min((innerWidth-120)/w,(innerHeight-160)/h,2);
  if (!isFinite(k)||k<=0) k=1;
  view.k=k; view.x=innerWidth/2-(a+c)/2*k; view.y=innerHeight/2-(b+e)/2*k;
}}
let frame=0;
function loop() {{ tick(); draw();
  if (frame===40 || frame===120) fitView();   // auto-encuadre al asentarse
  frame++; requestAnimationFrame(loop); }} loop();
addEventListener('dblclick', fitView);         // doble clic = re-centrar

// interaccion (drag/pan ya declarados arriba)
function toWorld(mx,my) {{ return {{x:(mx-view.x)/view.k, y:(my-view.y)/view.k}}; }}
function pick(mx,my) {{ let w=toWorld(mx,my);
  for (const n of DATA.nodes) {{ let dx=n.x-w.x, dy=n.y-w.y;
    if (dx*dx+dy*dy < (n.r+4)*(n.r+4)) return n; }} return null; }}
cv.addEventListener('mousedown', e=>{{ let n=pick(e.clientX,e.clientY);
  if (n) drag=n; else pan={{x:e.clientX-view.x, y:e.clientY-view.y}}; }});
addEventListener('mouseup', ()=>{{ drag=null; pan=null; }});
const tip=document.getElementById('tip');
addEventListener('mousemove', e=>{{
  if (drag) {{ let w=toWorld(e.clientX,e.clientY); drag.x=w.x; drag.y=w.y; drag.vx=drag.vy=0; }}
  else if (pan) {{ view.x=e.clientX-pan.x; view.y=e.clientY-pan.y; }}
  let n = !drag && !pan ? pick(e.clientX,e.clientY) : null;
  if (n) {{ tip.style.opacity=1; tip.style.left=(e.clientX+12)+'px'; tip.style.top=(e.clientY+12)+'px';
    tip.textContent = n.id+'  ['+n.area+']  ·  '+n.indeg+' enlaces entrantes'+(n.placeholder?'  · placeholder':''); }}
  else tip.style.opacity=0;
}});
cv.addEventListener('wheel', e=>{{ e.preventDefault();
  let s = e.deltaY<0 ? 1.1 : 1/1.1; let mx=e.clientX, my=e.clientY;
  view.x = mx-(mx-view.x)*s; view.y = my-(my-view.y)*s; view.k*=s; }}, {{passive:false}});
</script></body></html>"""
