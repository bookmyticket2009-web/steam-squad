// On a phone, wide admin tables turn into one card per row: tag every cell with its column name.
document.querySelectorAll(".table-wrap table").forEach(t => {
  const rows = [...t.querySelectorAll("tr")], head = rows.find(r => r.querySelector("th"));
  if (!head) return;
  const labels = [...head.children].map(h => h.textContent.trim());
  head.classList.add("th-row"); t.classList.add("stack");
  rows.forEach(r => { if (r !== head) [...r.children].forEach((td, i) => { td.dataset.label = labels[i] || ""; }); });
});
