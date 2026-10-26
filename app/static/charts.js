/* Small time-series charts (SVG) for mktk-map — one quantity per chart, no second axis.
   tsChart(el, {series:[{name,color,pts:[[ts,v],...]}], unit, fmt, down:[[t0,t1],...],
                range:[t0,t1], zero:true, gap:seconds})
   Colours: --s1 / --s2 (checked with a contrast validator for a dark surface). */
(function(){
  const NS="http://www.w3.org/2000/svg";
  const fmtT=(t,span)=>{ const d=new Date(t*1000);
    return span>2*86400? d.toLocaleDateString(I18N.locale,{weekday:"short",day:"numeric"})+" "+d.toLocaleTimeString(I18N.locale,{hour:"2-digit",minute:"2-digit"})
      : d.toLocaleTimeString(I18N.locale,{hour:"2-digit",minute:"2-digit"}); };
  const nice=(v)=>{ if(v<=0) return 1; const p=Math.pow(10,Math.floor(Math.log10(v))); const f=v/p;
    return (f<=1?1:f<=2?2:f<=5?5:10)*p; };

  window.tsChart=function(el, o){
    const W=Math.max(220, el.clientWidth||300), H=o.height||120, L=40, R=8, T=8, B=18;
    const all=o.series.flatMap(s=>s.pts.map(p=>p[1])).filter(v=>v!=null&&isFinite(v));
    const [t0,t1]=o.range;
    el.innerHTML="";
    el.style.position="relative";
    if(o.series.length>1){
      const lg=document.createElement("div"); lg.className="chlegend";
      lg.innerHTML=o.series.map(s=>`<span><i style="background:${s.color}"></i>${s.name}</span>`).join("");
      el.appendChild(lg);
    }
    if(!all.length){ const e=document.createElement("div"); e.className="chempty"; e.textContent=t("common.no_data"); el.appendChild(e); return; }
    let y0=o.zero===false? Math.min(...all) : Math.min(0, ...all), y1=Math.max(...all);
    if(o.max!=null) y1=Math.max(y1, o.max);
    if(y1===y0) y1=y0+1;
    const step=nice((y1-y0)/3); y1=Math.ceil(y1/step)*step; y0=Math.floor(y0/step)*step;
    const X=t=>L+(t-t0)/(t1-t0)*(W-L-R), Y=v=>T+(1-(v-y0)/(y1-y0))*(H-T-B);
    const svg=document.createElementNS(NS,"svg");
    svg.setAttribute("width",W); svg.setAttribute("height",H); svg.setAttribute("viewBox",`0 0 ${W} ${H}`);
    let h="";
    for(const [a,b] of (o.down||[])){
      const xa=Math.max(L,X(a)), xb=Math.min(W-R,X(b));
      if(xb>xa) h+=`<rect x="${xa}" y="${T}" width="${Math.max(1,xb-xa)}" height="${H-T-B}" class="chdown"/>`;
    }
    for(let v=y0; v<=y1+step/2; v+=step){
      const y=Y(v).toFixed(1);
      h+=`<line x1="${L}" x2="${W-R}" y1="${y}" y2="${y}" class="chgrid"/>`+
         `<text x="${L-5}" y="${+y+3}" text-anchor="end" class="chax">${o.fmt(v)}</text>`;
    }
    for(let i=0;i<4;i++){
      const t=t0+(t1-t0)*i/3, x=X(t);
      h+=`<text x="${x}" y="${H-4}" text-anchor="${i===0?"start":i===3?"end":"middle"}" class="chax">${fmtT(t,t1-t0)}</text>`;
    }
    const gap=o.gap||600;
    for(const s of o.series){
      let d="", prev=null;
      for(const [t,v] of s.pts){
        if(v==null||!isFinite(v)){ prev=null; continue; }
        d+=(prev==null||t-prev>gap?"M":"L")+X(t).toFixed(1)+","+Y(v).toFixed(1);
        prev=t;
      }
      h+=`<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    }
    h+=`<line class="chx" x1="0" x2="0" y1="${T}" y2="${H-B}" style="display:none"/>`;
    h+=o.series.map((s,i)=>`<circle class="chdot" data-i="${i}" r="4" fill="${s.color}" style="display:none"/>`).join("");
    h+=`<rect class="chhit" x="${L}" y="0" width="${W-L-R}" height="${H}" fill="transparent"/>`;
    svg.innerHTML=h;
    el.appendChild(svg);
    const tip=document.createElement("div"); tip.className="chtip"; tip.style.display="none"; el.appendChild(tip);
    const xs=o.series[0].pts.map(p=>p[0]);
    const line=svg.querySelector(".chx"), dots=[...svg.querySelectorAll(".chdot")];
    const hide=()=>{ line.style.display="none"; dots.forEach(c=>c.style.display="none"); tip.style.display="none"; };
    svg.querySelector(".chhit").addEventListener("mousemove",ev=>{
      const r=svg.getBoundingClientRect(), mx=ev.clientX-r.left;
      const t=t0+(mx-L)/(W-L-R)*(t1-t0);
      let bi=-1, bd=1e18;
      xs.forEach((x,i)=>{ const dd=Math.abs(x-t); if(dd<bd){bd=dd;bi=i;} });
      if(bi<0 || bd>gap){ hide(); return; }
      const tt=xs[bi], x=X(tt);
      line.setAttribute("x1",x); line.setAttribute("x2",x); line.style.display="";
      let rows="";
      o.series.forEach((s,i)=>{ const p=s.pts[bi]; const v=p&&p[1];
        if(v!=null&&isFinite(v)){ dots[i].setAttribute("cx",x); dots[i].setAttribute("cy",Y(v)); dots[i].style.display=""; }
        else dots[i].style.display="none";
        rows+=`<div><i style="background:${s.color}"></i>${s.name}: <b>${v!=null&&isFinite(v)? o.fmt(v)+" "+(o.unit||"") : "–"}</b></div>`; });
      const down=(o.down||[]).some(([a,b])=>tt>=a&&tt<=b);
      tip.innerHTML=`<div class="chtt">${new Date(tt*1000).toLocaleString(I18N.locale,{day:"numeric",month:"numeric",hour:"2-digit",minute:"2-digit"})}</div>${rows}${down?'<div class="chdn">⚠ '+t("common.unreachable")+'</div>':""}`;
      tip.style.display="";
      const tw=tip.offsetWidth;
      tip.style.left=Math.min(Math.max(0,x+10), W-tw)+"px"; tip.style.top=(o.series.length>1?18:0)+"px";
    });
    svg.addEventListener("mouseleave",hide);
  };
})();
