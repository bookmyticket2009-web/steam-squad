(() => {
  const live = document.getElementById("live"); if (!live) return;
  const btn = document.getElementById("enableAlerts"), baseTitle = document.title;
  let sig = null, pending = null, ctx = null, flasher = null;

  function beep() {
    navigator.vibrate?.([250, 100, 250]);
    if (!ctx) return;
    [880, 880, 1175].forEach((f, i) => {
      const t = ctx.currentTime + i * 0.28, o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = f; o.connect(g); g.connect(ctx.destination);
      g.gain.setValueAtTime(0.4, t); g.gain.exponentialRampToValueAtTime(0.001, t + 0.22);
      o.start(t); o.stop(t + 0.24);
    });
  }
  btn.addEventListener("click", async () => {
    ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
    await ctx.resume(); btn.textContent = "🔔 ALERTS ON"; btn.classList.remove("warn"); btn.classList.add("success"); beep();
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
  });
  function alertNew(n) {
    beep();
    if ("Notification" in window && Notification.permission === "granted") new Notification("Steam Squad: new payment", { body: n + " waiting to be verified" });
    clearInterval(flasher); let on = false;
    flasher = setInterval(() => { document.title = (on = !on) ? `🔔 ${n} NEW PAYMENT` : baseTitle; }, 800);
  }
  addEventListener("focus", () => { clearInterval(flasher); document.title = baseTitle; });

  async function refresh() {
    try {
      const html = await (await fetch(location.href, { cache: "no-store" })).text();
      const fresh = new DOMParser().parseFromString(html, "text/html").getElementById("live");
      if (fresh) live.innerHTML = fresh.innerHTML;   // page stays open, so sound keeps working
    } catch (_) {}
  }
  async function poll() {
    try {
      const r = await fetch("/admin/api/queue", { cache: "no-store" }); if (!r.ok) return;
      const d = await r.json();
      if (sig !== null && d.sig !== sig) await refresh();
      if (pending !== null && d.pending > pending) alertNew(d.pending);
      sig = d.sig; pending = d.pending;
    } catch (_) {}
  }
  document.addEventListener("submit", async e => {
    const f = e.target.closest("form.ajax"); if (!f) return;
    e.preventDefault();
    if (f.dataset.confirm && !confirm(f.dataset.confirm)) return;
    f.querySelector("button").disabled = true;
    try { await fetch(f.action, { method: "POST", body: new FormData(f) }); } catch (_) { alert("Network problem. Try again."); }
    await refresh(); poll();
  });
  poll(); setInterval(poll, 4000);
})();
