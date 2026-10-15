// Runs app.js + cart.js against a tiny fake browser. Usage: node test_cart.js [path-to-app.js]
const fs = require("fs"), vm = require("vm"), assert = require("assert");
function run(appSrc) {
  const els = {}, el = id => els[id] || (els[id] = { id, innerHTML: "", textContent: "", hidden: false, querySelectorAll: () => [], classList: { toggle() {} } });
  const store = {};
  const ctx = { document: { getElementById: id => id === "sheet" ? null : el(id), querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, body: { classList: { toggle() {} } } },
    localStorage: { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); }, removeItem: k => delete store[k] }, location: { pathname: "/cart" }, console };
  ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(appSrc, ctx); vm.runInContext(fs.readFileSync("static/js/cart.js", "utf8"), ctx);
  return { ctx, area: el("cartArea") };
}
for (const [label, src] of [["current app.js", fs.readFileSync(process.argv[2] || "static/js/app.js", "utf8")],
                            ["app.js WITHOUT the old onCartChange hook", fs.readFileSync("static/js/app.js", "utf8").replace("if(window.onCartChange)window.onCartChange();", "")]]) {
  const { ctx, area } = run(src);
  vm.runInContext(`addToCart({item_id:5,name:"Peri Peri Momos",cat:"Peri Peri",variant:"Veg",price:119,pieces:8,fry:false})`, ctx);
  assert(area.innerHTML.includes("₹119"), label + ": first item missing");
  vm.runInContext(`addToCart({item_id:12,name:"Peri Peri Fries",cat:"Fries",variant:"Regular",price:99,pieces:0,fry:false})`, ctx);
  assert(area.innerHTML.includes("Peri Peri Fries"), label + ": added fries did not appear in the cart list");
  assert(area.innerHTML.includes("<strong>₹218</strong>"), label + ": total did not update to 218");
  vm.runInContext(`addToCart({item_id:5,name:"Peri Peri Momos",cat:"Peri Peri",variant:"Veg",price:129,pieces:8,fry:true},2)`, ctx);
  assert(area.innerHTML.includes("Fry") && area.innerHTML.includes("<strong>₹476</strong>"), label + ": fried x2 line / total wrong");   // 119+99+2*129
  vm.runInContext(`addToCart({item_id:1,name:"Veg Momos",cat:"Steam",variant:"Veg",price:109,pieces:8,fry:true})`, ctx);
  const plain = area.innerHTML.split('<div class="cart-row">').find(r => r.includes("Veg Momos"));
  assert(plain && plain.includes("Fry · 8 PCS") && !plain.includes("Veg ·"), label + ": plain momo line should read 'Veg Momos' + 'Fry · 8 PCS' (no repeated Veg)");
  assert(area.innerHTML.includes("Peri Peri Momos") && area.innerHTML.includes("Veg · Fry · 8 PCS"), label + ": flavour line should read 'Veg · Fry · 8 PCS'");
  console.log("OK:", label);
}
