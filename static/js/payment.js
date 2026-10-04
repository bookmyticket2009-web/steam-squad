const form = document.getElementById("famPaymentForm");
const msg = document.getElementById("paymentMsg");
document.getElementById("copyFam")?.addEventListener("click", async e => {
  try { await navigator.clipboard.writeText(document.getElementById("famId").textContent.trim()); e.target.textContent = "COPIED ✓"; }
  catch (_) { e.target.textContent = "Select the ID and copy it"; }
});
form.addEventListener("submit", async e => {
  e.preventDefault();
  const btn = form.querySelector("button[type=submit]");
  btn.disabled = true; msg.textContent = "Submitting…";
  try {
    const r = await fetch("/payment/submit", { method: "POST", headers: { "X-CSRFToken": window.CSRF }, body: new FormData(form) });
    const d = await r.json();
    msg.textContent = d.message;
    if (d.ok) { setTimeout(() => location = d.redirect, 600); return; }
  } catch (_) { msg.textContent = "Network problem. Please try again."; }
  btn.disabled = false;
});
