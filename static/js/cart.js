const area=document.getElementById("cartArea");
const esc=s=>String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function renderCart(){
 const c=getCart(); if(!c.length){area.innerHTML='<div class="empty"><div class="big-doodle">🥟</div><h2>Your cart is empty</h2><a class="btn primary" href="/">BROWSE MENU</a></div>';return}
 let total=0;
 area.innerHTML=c.map((x,i)=>{const sub=x.price*x.quantity;total+=sub;const meta=x.dip_id?`Extra dip · ₹${x.price}`:`${esc(x.variant)} · 8 PCS · ₹${x.price}`;
 return `<div class="cart-row"><div><b>${esc(x.name)}</b><span>${meta}</span></div><div class="qty"><button data-i="${i}" data-d="-1">−</button><b>${x.quantity}</b><button data-i="${i}" data-d="1">+</button></div><strong>₹${sub}</strong><button class="remove" data-r="${i}">REMOVE</button></div>`}).join("")+
 `<div class="cart-total"><span>Total</span><strong>₹${total}</strong></div><a class="btn primary full" href="/checkout">CHECKOUT · PAY ONLINE</a>`;
 area.querySelectorAll("[data-i]").forEach(b=>b.onclick=()=>{let c=getCart(),i=+b.dataset.i;c[i].quantity=Math.min(50,c[i].quantity+ +b.dataset.d);if(c[i].quantity<=0)c.splice(i,1);saveCart(c);renderCart()});
 area.querySelectorAll("[data-r]").forEach(b=>b.onclick=()=>{let c=getCart();c.splice(+b.dataset.r,1);saveCart(c);renderCart()});
}
renderCart();
