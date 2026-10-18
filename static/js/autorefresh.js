// Reload the page every 10 seconds while it is visible and nobody is typing (used by Live Tokens).
setInterval(() => {
  if (!document.hidden && !document.querySelector("input:focus, textarea:focus")) location.reload();
}, 10000);
