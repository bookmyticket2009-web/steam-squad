window.CSRF = document.querySelector('meta[name="csrf-token"]')?.content || "";
function getCart(){ try{return JSON.parse(localStorage.getItem("steam_cart")||"[]")}catch(e){return[]}}
function saveCart(c){localStorage.setItem("steam_cart",JSON.stringify(c)); updateCartCount();}
function updateCartCount(){
 const c=getCart(), n=c.reduce((s,x)=>s+Number(x.quantity||0),0), t=c.reduce((s,x)=>s+Number(x.quantity||0)*Number(x.price||0),0);
 const el=document.getElementById("cartCount");if(el)el.textContent=n;
 const bar=document.getElementById("cartBar");
 if(bar){document.getElementById("barCount").textContent=n;document.getElementById("barTotal").textContent=t;bar.hidden=!(n>0&&location.pathname==="/");document.body.classList.toggle("has-bar",!bar.hidden)}
 renderOpts(); renderPicks();
 if(window.onCartChange)window.onCartChange();
}
function addToCart(item,n=1){
 const c=getCart(); const found=c.find(x=>(x.item_id||0)===(item.item_id||0)&&(x.dip_id||0)===(item.dip_id||0)&&!!x.fry===!!item.fry);
 if(found) found.quantity=Math.min(50,found.quantity+n); else c.push({...item,quantity:Math.min(50,n)}); saveCart(c);
}
// " · Fry" / " · Steam" label for flavours (plain momos are named Steam Momos / Fry Momos instead)
function styleText(x){return x.cat&&x.cat!=="Steam"&&"fry" in x?(x.fry?" · Fry":" · Steam"):""}
function momoItem(d,fry){return{item_id:+d.itemId,name:d.cat==="Steam"&&fry?"Fry Momos":d.name,cat:d.cat||"",variant:d.variant,price:+d.price+(fry?+d.extra:0),pieces:+d.pieces,fry}}
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
 document.querySelectorAll(".pick").forEach(p=>{
  const id=+p.dataset.itemId, n=c.filter(x=>x.item_id===id).reduce((s,x)=>s+x.quantity,0), b=p.querySelector(".cnt");
  b.textContent="×"+n; b.hidden=!n; p.classList.toggle("on",n>0);
 });
}

// Pop-up: Steam ₹0 / Fry +₹10, quantity, live total
const sheet=document.getElementById("sheet"); let cur=null, fry=false, qty=1;
function paintSheet(){
 const d=cur.dataset, extra=+d.extra, unit=+d.price+(fry?extra:0);
 document.getElementById("sheetTitle").textContent=`${d.name} · ${d.variant}`;
 document.getElementById("sheetSub").textContent=`${d.pieces} PCS per plate · how do you want it?`;
 document.getElementById("fryExtra").textContent=`+₹${extra}`;
 sheet.querySelectorAll("[data-style]").forEach(b=>b.classList.toggle("on",(b.dataset.style==="fry")===fry));
 document.getElementById("sheetQty").textContent=qty;
 document.getElementById("sheetAdd").textContent=`ADD TO CART · ₹${unit*qty}`;
}
document.addEventListener("click",e=>{
 const pk=e.target.closest(".pick");
 if(pk&&sheet){cur=pk;fry=false;qty=1;paintSheet();sheet.showModal();return}
 if(sheet&&(e.target===sheet||sheet.contains(e.target))){
  const t=e.target;
  if(t===sheet||t.closest("[data-close]")){sheet.close();return}
  const st=t.closest("[data-style]"); if(st){fry=st.dataset.style==="fry";paintSheet();return}
  const q=t.closest("[data-q]"); if(q){qty=Math.min(50,Math.max(1,qty+ +q.dataset.q));paintSheet();return}
  if(t.closest("#sheetAdd")){addToCart(momoItem(cur.dataset,fry),qty);sheet.close();toast(`Added ${qty} × ${fry&&cur.dataset.cat==="Steam"?"Fry Momos":cur.dataset.name} (${cur.dataset.variant}${fry?", Fry":", Steam"})`)}
  return;
 }
 const o=e.target.closest(".opt"); if(!o||o.classList.contains("soldout"))return;
 if(e.target.closest(".add-btn")){const d=o.dataset;addToCart(d.dipId?{dip_id:+d.dipId,name:d.name,variant:"Extra dip",price:+d.price}:momoItem(d,false));return}
 const s=e.target.closest("[data-d]"); if(!s)return;
 const c=getCart(), i=c.findIndex(mine(o)); if(i<0)return;
 c[i].quantity=Math.min(50,c[i].quantity+ +s.dataset.d); if(c[i].quantity<=0)c.splice(i,1); saveCart(c);
});
updateCartCount();
