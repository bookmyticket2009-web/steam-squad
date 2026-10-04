window.CSRF = document.querySelector('meta[name="csrf-token"]')?.content || "";
function getCart(){ try{return JSON.parse(localStorage.getItem("steam_cart")||"[]")}catch(e){return[]}}
function saveCart(c){localStorage.setItem("steam_cart",JSON.stringify(c)); updateCartCount();}
function updateCartCount(){
 const c=getCart(), n=c.reduce((s,x)=>s+Number(x.quantity||0),0), t=c.reduce((s,x)=>s+Number(x.quantity||0)*Number(x.price||0),0);
 const el=document.getElementById("cartCount");if(el)el.textContent=n;
 const bar=document.getElementById("cartBar");
 if(bar){document.getElementById("barCount").textContent=n;document.getElementById("barTotal").textContent=t;bar.hidden=!(n>0&&location.pathname==="/");document.body.classList.toggle("has-bar",!bar.hidden)}
 renderOpts();
}
function addToCart(item){
 const c=getCart(); const found=c.find(x=>(x.item_id||0)===(item.item_id||0)&&(x.dip_id||0)===(item.dip_id||0));
 if(found) found.quantity++; else c.push({...item,quantity:1}); saveCart(c);
}
const mine=o=>x=>o.dataset.dipId?x.dip_id===+o.dataset.dipId:x.item_id===+o.dataset.itemId;
function itemFrom(o){const d=o.dataset;return d.dipId?{dip_id:+d.dipId,name:d.name,variant:"Extra dip",price:+d.price}:{item_id:+d.itemId,name:d.name,variant:d.variant,price:+d.price,pieces:+d.pieces}}
// Each ADD button turns into a  − 2 +  counter once the item is in the cart
function renderOpts(){
 const c=getCart();
 document.querySelectorAll(".opt:not(.soldout)").forEach(o=>{
  const x=c.find(mine(o)), q=x?x.quantity:0, ctl=o.querySelector(".ctl");
  ctl.innerHTML=q?`<div class="step"><button data-d="-1" aria-label="Remove one">−</button><b>${q}</b><button data-d="1" aria-label="Add one">+</button></div>`:'<button class="btn add-btn">ADD</button>';
  o.classList.toggle("on",q>0);
 });
}
document.addEventListener("click",e=>{
 const o=e.target.closest(".opt"); if(!o||o.classList.contains("soldout"))return;
 if(e.target.closest(".add-btn")){addToCart(itemFrom(o));return}
 const s=e.target.closest("[data-d]"); if(!s)return;
 const c=getCart(), i=c.findIndex(mine(o)); if(i<0)return;
 c[i].quantity=Math.min(50,c[i].quantity+ +s.dataset.d); if(c[i].quantity<=0)c.splice(i,1); saveCart(c);
});
updateCartCount();
