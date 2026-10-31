/* "Share my win" image card (bingo room). Draws a 1080x1350 PNG on a canvas from what the results
   screen already shows; nothing is sent to a server. */
(() => {
  "use strict";
  const ov = document.getElementById("winner-overlay");
  if (!ov) return;
  const loadImg = (src) => new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = src; });

  async function makeCard(amount, cartela) {
    try { await document.fonts.ready; } catch (e) { /* ignore */ }
    const W = 1080, H = 1350, c = document.createElement("canvas");
    c.width = W; c.height = H;
    const g = c.getContext("2d");
    let bg = g.createLinearGradient(0, 0, 0, H); bg.addColorStop(0, "#0B0B0F"); bg.addColorStop(1, "#1F1F27");
    g.fillStyle = bg; g.fillRect(0, 0, W, H);
    [[180, 160, "rgba(255,184,0,.28)"], [900, 1150, "rgba(224,138,0,.25)"]].forEach(([x, y, col]) => {
      const r = g.createRadialGradient(x, y, 0, x, y, 620); r.addColorStop(0, col); r.addColorStop(1, "rgba(0,0,0,0)");
      g.fillStyle = r; g.fillRect(0, 0, W, H);
    });
    const logo = await loadImg("/static/img/melbete-bingo-logo.png");
    const lw = 900, lh = lw * logo.height / logo.width;
    g.drawImage(logo, (W - lw) / 2, 200, lw, lh);
    g.textAlign = "center";
    const gold = g.createLinearGradient(0, 640, 0, 800); gold.addColorStop(0, "#FFE9A8"); gold.addColorStop(1, "#D99A1C");
    g.fillStyle = gold; g.font = "800 170px Sora, Inter, system-ui, sans-serif"; g.fillText("I WON!", W / 2, 780);
    g.fillStyle = "#F8FAFC"; g.font = "800 120px Sora, Inter, system-ui, sans-serif"; g.fillText(`${amount} ETB`, W / 2, 940);
    if (cartela) { g.fillStyle = "#B3AEA2"; g.font = "600 52px Inter, system-ui, sans-serif"; g.fillText(`Cartela #${cartela}`, W / 2, 1020); }
    g.fillStyle = "#B3AEA2"; g.font = "600 44px Inter, system-ui, sans-serif"; g.fillText("Melbete Bingo", W / 2, 1250);
    return new Promise((res) => c.toBlob(res, "image/png"));
  }

  function preview(url) {
    const m = document.createElement("div");
    m.className = "melbete-share-modal";
    m.innerHTML = `<img src="${url}" alt="My Melbete Bingo win"><p>Press and hold the image to save or share it.</p><button type="button">Close</button>`;
    m.querySelector("button").addEventListener("click", () => { m.remove(); URL.revokeObjectURL(url); });
    document.body.appendChild(m);
  }

  async function share(btn) {
    const p = document.getElementById("winner-payout"), sub = document.getElementById("winner-subtitle");
    const am = p && /You won\s+([\d.,]+)/i.exec(p.textContent || "");
    if (!am) return;
    const ca = sub && /#(\d+)/.exec(sub.textContent || "");
    btn.disabled = true;
    try {
      const blob = await makeCard(am[1], ca && ca[1]);
      const file = new File([blob], "melbete-bingo-win.png", { type: "image/png" });
      if (navigator.canShare && navigator.canShare({ files: [file] })) {
        try { await navigator.share({ files: [file], title: "Melbete Bingo" }); return; }
        catch (e) { if (e && e.name === "AbortError") return; }
      }
      preview(URL.createObjectURL(blob));
    } finally { btn.disabled = false; }
  }

  new MutationObserver(() => {
    if (!ov.classList.contains("melbete-win") || document.getElementById("melbete-share-btn")) return;
    const p = document.getElementById("winner-payout");
    if (!p) return;
    const b = document.createElement("button");
    b.type = "button"; b.id = "melbete-share-btn"; b.className = "melbete-share-btn"; b.textContent = "\ud83c\udfc6 Share my win";
    b.addEventListener("click", () => share(b));
    p.insertAdjacentElement("afterend", b);
  }).observe(ov, { attributes: true, attributeFilter: ["class"] });
})();
