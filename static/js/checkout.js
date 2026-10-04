const form=document.getElementById("checkoutForm"), summary=document.getElementById("checkoutSummary"), msg=document.getElementById("checkoutMsg");
const cart=getCart();
if(!cart.length){location="/cart"}
summary.innerHTML=`<div class="cart-total"><span>Total</span><strong>₹${cart.reduce((s,x)=>s+x.price*x.quantity,0)}</strong></div>`;
form.addEventListener("submit",async e=>{
 e.preventDefault(); const btn=form.querySelector("button[type=submit]"); btn.disabled=true; // avoids double orders on double-tap
 const data=Object.fromEntries(new FormData(form)); data.cart=cart; msg.textContent="Creating order…";
 try{
  const r=await fetch("/checkout",{method:"POST",headers:{"Content-Type":"application/json","X-CSRFToken":window.CSRF},body:JSON.stringify(data)}); const d=await r.json();
  if(!d.ok){msg.textContent=d.message;btn.disabled=false;return}
  localStorage.removeItem("steam_cart"); location=d.redirect;
 }catch(_){msg.textContent="Network problem. Please try again.";btn.disabled=false}
});
