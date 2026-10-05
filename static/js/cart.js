const area=document.getElementById("cartArea"), more=document.getElementById("more");
const esc=s=>String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function funky(c){
 const dip=c.some(x=>x.dip_id), fries=c.some(x=>/fries/i.test(x.name));
 if(!dip&&!fries)return["WAIT… FORGETTING SOMETHING?","Momos without a dip? Bold move. Grab some sauce or fries before you pay."];
 if(!dip)return["NO DIP?! SERIOUSLY?","Your momos feel naked. A sauce is just ₹10."];
 if(!fries)return["FRIES LOOKING LONELY","Hot, crispy fries complete the squad. Add some?"];
 return["SQUAD LOOKING STACKED","Anything else, boss? Add more, or head to checkout."];
}
function renderCart(){
 const c=getCart(); if(more)more.hidden=!c.length;
 if(more&&c.length){const[t,s]=funky(c);document.getElementById("moreTitle").textContent=t;document.getElementById("moreSub").textContent=s}
 if(!c.length){area.innerHTML='<div class="empty"><div class="big-doodle">🥟</div><h2>Your cart is empty</h2><a class="btn primary" href="/">BROWSE MENU</a></div>';return}
 let total=0;
 area.innerHTML=c.map((x,i)=>{const sub=x.price*x.quantity;total+=sub;
  const meta=x.dip_id?`Extra dip · ₹${x.price} each`:x.pieces?`${esc(x.variant)} · ${x.pieces} PCS · ₹${x.price} each`:`₹${x.price} each`;
  return `<div class="cart-row"><div><b>${esc(x.name)}</b><span>${meta}</span></div><div class="step"><button data-i="${i}" data-d="-1" aria-label="Remove one">−</button><b>${x.quantity}</b><button data-i="${i}" data-d="1" aria-label="Add one">+</button></div><strong>₹${sub}</strong><button class="remove" data-r="${i}">REMOVE</button></div>`}).join("")+
 `<div class="cart-total"><span>Total</span><strong>₹${total}</strong></div><a class="btn primary full" href="/checkout">CHECKOUT · PAY BY UPI</a>`;
 area.querySelectorAll("[data-i]").forEach(b=>b.onclick=()=>{let c=getCart(),i=+b.dataset.i;c[i].quantity=Math.min(50,c[i].quantity+ +b.dataset.d);if(c[i].quantity<=0)c.splice(i,1);saveCart(c)});
 area.querySelectorAll("[data-r]").forEach(b=>b.onclick=()=>{let c=getCart();c.splice(+b.dataset.r,1);saveCart(c)});
}
window.onCartChange=renderCart;   // re-draw whenever the cart changes (also from the "Add more" buttons)
renderCart();
