window.CSRF = document.querySelector('meta[name="csrf-token"]')?.content || "";
function getCart(){ try{return JSON.parse(localStorage.getItem("steam_cart2")||"[]")}catch(e){return[]}}
function saveCart(c){localStorage.setItem("steam_cart2",JSON.stringify(c)); updateCartCount();}
function updateCartCount(){
 const c=getCart(), n=c.reduce((s,x)=>s+Number(x.quantity||0),0), t=c.reduce((s,x)=>s+Number(x.quantity||0)*Number(x.price||0),0);
 const el=document.getElementById("cartCount");if(el)el.textContent=n;
 const bar=document.getElementById("cartBar");
 if(bar){document.getElementById("barCount").textContent=n;document.getElementById("barTotal").textContent=t;bar.hidden=!(n>0&&location.pathname==="/");document.body.classList.toggle("has-bar",!bar.hidden)}
 renderOpts(); renderPicks();
 if(window.onCartChange)window.onCartChange();
}
function addToCart(item,n=1){
 const c=getCart(); const found=c.find(x=>(x.item_id||0)===(item.item_id||0)&&(x.dip_id||0)===(item.dip_id||0)&&!!x.fry===!!item.fry&&JSON.stringify(x.choices||null)===JSON.stringify(item.choices||null));
 if(found) found.quantity=Math.min(50,found.quantity+n); else c.push({...item,quantity:Math.min(50,n)}); saveCart(c);
}
// "Fry" / "Steam" for any plate that has a cooking choice, and whether to show "Veg"/"Paneer" next to the name
function styleOf(x){return x.cat&&"fry" in x?(x.fry?"Fry":"Steam"):""}
function showVariant(x){return !x.dip_id&&x.variant&&x.variant!=="Regular"&&x.cat!=="Steam"}
function momoItem(d,fry){return{item_id:+d.itemId,name:d.name,cat:d.cat||"",variant:d.variant,price:+d.price+(fry?+d.extra:0),pieces:+d.pieces,fry}}
const plainName=d=>d.cat==="Steam"?d.name:`${d.name} · ${d.variant}`;
function toast(msg){let t=document.getElementById("toast");if(!t){t=document.createElement("div");t.id="toast";t.className="toast";t.setAttribute("role","status");document.body.appendChild(t)}
 t.textContent=msg;t.classList.add("show");clearTimeout(toast.h);toast.h=setTimeout(()=>t.classList.remove("show"),1800)}

// Fries and dips: ADD turns into a  - 2 +  counter
const mine=o=>x=>o.dataset.dipId?x.dip_id===+o.dataset.dipId:(x.item_id===+o.dataset.itemId&&!x.fry);
function renderOpts(){
 const c=getCart();
 document.querySelectorAll(".opt:not(.soldout)").forEach(o=>{
  const x=c.find(mine(o)), q=x?x.quantity:0, ctl=o.querySelector(".ctl");
  ctl.innerHTML=q?`<div class="step"><button data-d="-1" aria-label="Remove one">−</button><b>${q}</b><button data-d="1" aria-label="Add one">+</button></div>`:'<button class="btn add-btn">ADD</button>';
  o.classList.toggle("on",q>0);
 });
}
// Momo tiles: badge with how many of this item (steam + fry) are in the cart
function renderPicks(){
 const c=getCart();
 document.querySelectorAll(".pick,.combo-pick").forEach(p=>{
  const id=+p.dataset.itemId, n=c.filter(x=>x.item_id===id).reduce((s,x)=>s+x.quantity,0), b=p.querySelector(".cnt");
  b.textContent="×"+n; b.hidden=!n; p.classList.toggle("on",n>0);
 });
}

// Combos: pop-up with the choices (sauce / momos / fries / steam or fry), quantity and live total
const csheet=document.getElementById("comboSheet"); let ccur=null, copts=[], cpick={}, cqty=1, croom=50;
const escHtml=s=>String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const cextra=()=>copts.reduce((s,g)=>{const c=g.choices.find(x=>x.value===cpick[g.key]);return s+(c?c.extra:0)},0);
function paintCombo(){
 const d=ccur.dataset, unit=+d.price+cextra(), ready=copts.every(g=>cpick[g.key]);
 document.getElementById("comboTitle").textContent=d.name+(d.variant!=="Regular"?` · ${d.variant}`:"");
 document.getElementById("comboGroups").innerHTML=copts.map(g=>`<div class="grp"><p class="grp-label">${escHtml(g.label)}</p><div class="choices">${g.choices.map(c=>`<button type="button" class="choice${cpick[g.key]===c.value?" on":""}" data-g="${escHtml(g.key)}" data-v="${escHtml(c.value)}"${c.ok?"":" disabled"}>${escHtml(c.label)}${c.ok?"":" (sold out)"}</button>`).join("")}</div></div>`).join("");
 document.getElementById("comboQty").textContent=cqty;
 const add=document.getElementById("comboAdd"); add.disabled=!ready; add.textContent=ready?`ADD TO CART · ₹${unit*cqty}`:"CHOOSE YOUR OPTIONS";
}
function comboClick(e){
 const pk=e.target.closest(".combo-pick");
 if(pk&&csheet){
  const left=pk.dataset.left===""?Infinity:+pk.dataset.left, have=getCart().filter(x=>x.item_id===+pk.dataset.itemId).reduce((s,x)=>s+x.quantity,0);
  croom=Math.min(50,left-have); if(croom<=0){toast(`Only ${left} left, and they're all in your cart`);return true}
  ccur=pk; copts=JSON.parse(pk.dataset.options||"[]"); cqty=1; cpick={};
  copts.forEach(g=>{if(g.key==="cooking"){const f=g.choices.find(c=>c.ok);if(f)cpick[g.key]=f.value}});   // steamed unless they pick fry
  paintCombo(); csheet.showModal(); return true;
 }
 if(!csheet||!(e.target===csheet||csheet.contains(e.target)))return false;
 const t=e.target;
 if(t===csheet||t.closest("[data-close]")){csheet.close();return true}
 const ch=t.closest(".choice"); if(ch){cpick[ch.dataset.g]=ch.dataset.v;paintCombo();return true}
 const q=t.closest("[data-q]"); if(q){cqty=Math.min(croom,Math.max(1,cqty+ +q.dataset.q));paintCombo();return true}
 const add=t.closest("#comboAdd");
 if(add&&!add.disabled){
  const d=ccur.dataset;
  addToCart({item_id:+d.itemId,name:d.name,cat:"Combo",variant:d.variant,price:+d.price+cextra(),pieces:0,choices:{...cpick}},cqty);
  csheet.close(); toast(`Added ${cqty} × ${d.name}`);
 }
 return true;
}

// Pop-up: Steam ₹0 / Fry +₹10, quantity, live total
const sheet=document.getElementById("sheet"); let cur=null, fry=false, qty=1, room=50;
function paintSheet(){
 const d=cur.dataset, extra=+d.extra, unit=+d.price+(fry?extra:0);
 document.getElementById("sheetTitle").textContent=plainName(d);
 document.getElementById("sheetSub").textContent=`${d.pieces} PCS per plate · how do you want it?`+(room<=5?` · only ${room} left`:"");
 document.getElementById("fryExtra").textContent=`+₹${extra}`;
 sheet.querySelectorAll("[data-style]").forEach(b=>b.classList.toggle("on",(b.dataset.style==="fry")===fry));
 document.getElementById("sheetQty").textContent=qty;
 document.getElementById("sheetAdd").textContent=`ADD TO CART · ₹${unit*qty}`;
}
document.addEventListener("click",e=>{
 if(comboClick(e))return;
 const pk=e.target.closest(".pick");
 if(pk&&sheet){
  const left=pk.dataset.left===""?Infinity:+pk.dataset.left, have=getCart().filter(x=>x.item_id===+pk.dataset.itemId).reduce((s,x)=>s+x.quantity,0);
  room=Math.min(50,left-have);
  if(room<=0){toast(`Only ${left} left, and they're all in your cart`);return}
  cur=pk;fry=false;qty=1;paintSheet();sheet.showModal();return}
 if(sheet&&(e.target===sheet||sheet.contains(e.target))){
  const t=e.target;
  if(t===sheet||t.closest("[data-close]")){sheet.close();return}
  const st=t.closest("[data-style]"); if(st){fry=st.dataset.style==="fry";paintSheet();return}
  const q=t.closest("[data-q]"); if(q){qty=Math.min(room,Math.max(1,qty+ +q.dataset.q));paintSheet();return}
  if(t.closest("#sheetAdd")){addToCart(momoItem(cur.dataset,fry),qty);sheet.close();toast(`Added ${qty} × ${plainName(cur.dataset)} (${fry?"Fry":"Steam"})`)}
  return;
 }
 const o=e.target.closest(".opt"); if(!o||o.classList.contains("soldout"))return;
 if(e.target.closest(".add-btn")){const d=o.dataset;addToCart(d.dipId?{dip_id:+d.dipId,name:d.name,variant:"Extra dip",price:+d.price}:momoItem(d,false));return}
 const s=e.target.closest("[data-d]"); if(!s)return;
 const c=getCart(), i=c.findIndex(mine(o)); if(i<0)return;
 const left=o.dataset.left?+o.dataset.left:50; c[i].quantity=Math.min(50,left,c[i].quantity+ +s.dataset.d); if(c[i].quantity<=0)c.splice(i,1); saveCart(c);
});
updateCartCount();
