/* UI translations. Texts live in app/locales/<language>.json; the server sends them via /api/i18n
   (already filled with English for missing keys).
   t(key, {param}) · tn(key, n) = plural (key.one / key.few / key.other ...) ·
   tOr(key, fallback) = for codes from the backend that may have no translation.
   Static HTML: data-i18n (text), data-i18n-html, data-i18n-placeholder, data-i18n-title.
   A page starts its own code only after I18N.ready. */
(function(){
  const S={lang:"en", locale:"en-GB", messages:{}, languages:[]};
  const fmt=(s,p)=>p? String(s).replace(/\{(\w+)\}/g,(m,k)=>p[k]!=null? p[k] : m) : String(s);

  window.t=function(key,p){
    const s=S.messages[key];
    if(s==null){ console.warn("[i18n] missing key:",key); return key; }
    return fmt(s,p);
  };
  window.tn=function(key,n,p){
    let cat="other";
    try{ cat=new Intl.PluralRules(S.locale).select(n); }catch(e){}
    const k=S.messages[key+"."+cat]!=null? key+"."+cat : key+".other";
    return t(k,Object.assign({n},p||{}));
  };
  window.tOr=(key,fallback,p)=>S.messages[key]!=null? fmt(S.messages[key],p) : fallback;
  window.applyI18n=function(root){
    const r=root||document;
    r.querySelectorAll("[data-i18n]").forEach(e=>e.textContent=t(e.dataset.i18n));
    r.querySelectorAll("[data-i18n-html]").forEach(e=>e.innerHTML=t(e.dataset.i18nHtml));
    r.querySelectorAll("[data-i18n-placeholder]").forEach(e=>e.placeholder=t(e.dataset.i18nPlaceholder));
    r.querySelectorAll("[data-i18n-title]").forEach(e=>e.title=t(e.dataset.i18nTitle));
  };
  const domReady=new Promise(res=>document.readyState==="loading"
    ? document.addEventListener("DOMContentLoaded",res) : res());
  S.ready=fetch("/api/i18n").then(r=>r.json()).then(j=>{
    S.lang=j.lang; S.messages=j.messages||{}; S.languages=j.languages||[];
    S.locale=S.messages["_meta.locale"]||j.lang;
  }).catch(e=>console.error("[i18n] cannot load translations",e))
    .then(()=>domReady).then(()=>{ document.documentElement.lang=S.lang; applyI18n(); });
  window.I18N=S;
})();
