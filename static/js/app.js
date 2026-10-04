window.CSRF = document.querySelector('meta[name="csrf-token"]')?.content || "";
function getCart(){ try{return JSON.parse(localStorage.getItem("steam_cart")||"[]")}catch(e){return[]}}
function saveCart(c){localStorage.setItem("steam_cart",JSON.stringify(c)); updateCartCount();}
function updateCartCount(){
 const c=getCart(), n=c.reduce((s,x)=>s+Number(x.quantity||0),0), t=c.reduce((s,x)=>s+Number(x.quantity||0)*Number(x.price||0),0);
 const el=document.getElementById("cartCount");if(el)el.textContent=n;
 const bar=document.getElementById("cartBar");
 if(bar){document.getElementById("barCount").textContent=n;document.getElementById("barTotal").textContent=t;bar.hidden=!(n>0&&location.pathname==="/");document.body.classList.toggle("has-bar",!bar.hidden)}}
function addToCart(item){
 const c=getCart(); const found=c.find(x=>(x.item_id||0)===(item.item_id||0)&&(x.dip_id||0)===(item.dip_id||0));
 if(found) found.quantity++; else c.push({...item,quantity:1}); saveCart(c);
}
function flash(b,txt){const old=b.textContent;b.textContent=txt;setTimeout(()=>b.textContent=old,800)}
document.addEventListener("click",e=>{
 const b=e.target.closest(".add"); if(b){addToCart({item_id:Number(b.dataset.itemId),name:b.dataset.name,variant:b.dataset.variant,price:Number(b.dataset.price)});flash(b,"ADDED ✓");return}
 const d=e.target.closest(".add-dip"); if(d){addToCart({dip_id:Number(d.dataset.dipId),name:d.dataset.name,variant:"Extra dip",price:Number(d.dataset.price)});flash(d,"ADDED ✓")}
});
updateCartCount();
