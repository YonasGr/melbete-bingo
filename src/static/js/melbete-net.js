/* Connection pill: "Reconnecting..." whenever the phone is offline, the server cannot be reached, or the live
   game socket has dropped. Purely informational - it never retries or changes requests itself.
   Loaded on the hub and in the game room. app.js reports the socket state through MelbeteNet.socket(bool). */
(() => {
  "use strict";
  let pill = null, hideTimer = null;
  let offline = typeof navigator !== "undefined" && navigator.onLine === false;
  let failures = 0;       // consecutive network-level fetch failures
  let socketDown = false; // set by the game room

  function ensure() {
    if (pill) return pill;
    const style = document.createElement("style");
    style.textContent =
      ".mb-net{position:fixed;left:50%;top:calc(env(safe-area-inset-top,0px) + 8px);transform:translate(-50%,-140%);z-index:9999;" +
      "display:flex;align-items:center;gap:8px;padding:7px 14px;border-radius:999px;background:#1b1b22;color:#F8FAFC;" +
      "border:1px solid rgb(255 201 51 / .55);font:700 12px/1 system-ui,sans-serif;box-shadow:0 8px 24px rgb(0 0 0 / .45);" +
      "transition:transform .25s ease;pointer-events:none}.mb-net.show{transform:translate(-50%,0)}" +
      ".mb-net i{width:10px;height:10px;border-radius:50%;border:2px solid #FFC933;border-top-color:transparent;animation:mb-net-spin .8s linear infinite}" +
      "@keyframes mb-net-spin{to{transform:rotate(360deg)}}@media (prefers-reduced-motion:reduce){.mb-net i{animation:none}.mb-net{transition:none}}";
    document.head.appendChild(style);
    pill = document.createElement("div");
    pill.className = "mb-net";
    pill.setAttribute("role", "status");
    pill.setAttribute("aria-live", "polite");
    pill.innerHTML = "<i></i><span></span>";
    document.body.appendChild(pill);
    return pill;
  }

  function update() {
    clearTimeout(hideTimer);
    const bad = offline || failures >= 2 || socketDown;
    if (!bad && !pill) return;
    const el = ensure();
    el.querySelector("span").textContent = offline ? "You're offline - reconnecting…" : "Reconnecting…";
    if (bad) {
      el.classList.add("show");
    } else {
      // brief grace so a flapping connection does not flicker the pill
      hideTimer = setTimeout(() => el.classList.remove("show"), 700);
    }
  }

  window.addEventListener("offline", () => { offline = true; update(); });
  window.addEventListener("online", () => { offline = false; failures = 0; update(); });

  // Count network-level failures (a thrown TypeError - not HTTP errors) on any request the page makes.
  const nativeFetch = window.fetch && window.fetch.bind(window);
  if (nativeFetch) {
    window.fetch = (...args) =>
      nativeFetch(...args).then(
        (res) => { if (failures) { failures = 0; update(); } return res; },
        (err) => { if (err && err.name !== "AbortError") { failures += 1; update(); } throw err; }
      );
  }

  window.MelbeteNet = { socket(up) { socketDown = !up; update(); } };
})();
