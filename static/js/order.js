// Display only: the server decides whether a cancellation is allowed.
const box = document.getElementById("refundBox");
const el = document.getElementById("countdown");
let left = box ? Number(box.dataset.seconds) : 0;
function tick() {
  if (!box) return;
  if (left <= 0) { box.innerHTML = '<p class="muted">Cancellation window expired. No cancellation/refund is available.</p>'; return; }
  const m = String(Math.floor(left / 60)).padStart(2, "0"), s = String(left % 60).padStart(2, "0");
  el.textContent = `Cancellation available for: ${m}:${s}`;
  left--; setTimeout(tick, 1000);
}
document.getElementById("cancelBtn")?.addEventListener("click", async () => {
  if (!confirm("Are you sure? Your order will be cancelled and a refund will be initiated.")) return;
  try {
    const r = await fetch(`/order/${window.ORDER_ID}/cancel`, { method: "POST", headers: { "X-CSRFToken": window.CSRF } });
    const d = await r.json(); alert(d.message); location.reload();
  } catch (_) { alert("Network problem. Please try again."); }
});
// Reload the page whenever the status changes (token appears, order becomes READY, ...)
function refreshOrder() {
  fetch(`/api/order/${window.ORDER_ID}`).then(r => r.json()).then(o => {
    if (o.order_status && o.order_status !== window.ORDER_STATUS) location.reload();
  }).catch(() => {});
}
tick(); setInterval(refreshOrder, 8000);
