/* Spooldesk widget loader.
 *
 * One <script> tag on someone else's page, so the rules are strict: touch
 * nothing outside our own corner, break nothing if we fail, and stay small
 * enough that nobody notices the extra request.
 *
 * The launcher lives in a shadow root and the panel lives in an iframe. Both
 * are isolation, in opposite directions. A shadow root stops the host page's
 * CSS from reaching the button — every reset stylesheet on the web has an
 * opinion about buttons. The iframe stops us from reaching *them*, which
 * matters more: a widget that leaks a stray `* { }` rule into a customer's
 * checkout page is a widget that gets removed the same afternoon.
 *
 * __SPOOL_BASE__ is filled in when this is served, not configured by hand.
 * Pointing an embed at the wrong host is the commonest way one silently
 * fails, and it fails in a way nobody sees: the button appears and nothing
 * behind it works.
 */
(function () {
  "use strict";

  var BASE = "__SPOOL_BASE__";
  var MOUNT_ID = "spooldesk-widget-root";

  // Twice on one page means two conversations and two unread counts.
  if (document.getElementById(MOUNT_ID)) return;

  var host = document.createElement("div");
  host.id = MOUNT_ID;
  host.style.cssText = "position:fixed;bottom:0;right:0;z-index:2147483000;";
  var root = host.attachShadow ? host.attachShadow({ mode: "open" }) : host;

  var style = document.createElement("style");
  style.textContent = [
    ":host,*{box-sizing:border-box}",
    ".btn{position:fixed;bottom:20px;right:20px;width:56px;height:56px;",
    "border-radius:28px;border:0;cursor:pointer;background:#7c5cff;color:#fff;",
    "box-shadow:0 6px 24px rgba(0,0,0,.28);display:flex;align-items:center;",
    "justify-content:center;transition:transform .15s ease,box-shadow .15s ease}",
    ".btn:hover{transform:translateY(-1px);box-shadow:0 10px 28px rgba(0,0,0,.34)}",
    ".btn:focus-visible{outline:3px solid #b9a7ff;outline-offset:2px}",
    ".btn svg{width:26px;height:26px;display:block}",
    // The unread dot is a dot. A number would need the panel to tell the
    // launcher how many, and "someone replied" is the whole message.
    ".dot{position:absolute;top:12px;right:12px;width:12px;height:12px;",
    "border-radius:6px;background:#f43f5e;border:2px solid #7c5cff;display:none}",
    ".dot.on{display:block}",
    ".panel{position:fixed;bottom:88px;right:20px;width:380px;height:min(600px,calc(100vh - 120px));",
    "border:0;border-radius:16px;box-shadow:0 18px 60px rgba(0,0,0,.35);",
    "background:transparent;display:none;color-scheme:normal}",
    ".panel.on{display:block}",
    // Below this a floating panel is worse than a full-height one: it covers
    // the page anyway and loses room to the keyboard.
    "@media (max-width:480px){.panel{right:0;bottom:0;width:100vw;height:100vh;border-radius:0}",
    ".panel.on ~ .btn{display:none}}",
  ].join("");

  var btn = document.createElement("button");
  btn.className = "btn";
  btn.type = "button";
  btn.setAttribute("aria-label", "Support chat");
  btn.innerHTML =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M8 9h8M8 13h5"/>' +
    '<path d="M3 20l1.3-3.9A8.5 8.5 0 1 1 7.9 19.7L3 20z"/></svg>';

  var dot = document.createElement("span");
  dot.className = "dot";
  btn.appendChild(dot);

  var panel = document.createElement("iframe");
  panel.className = "panel";
  panel.title = "Support chat";
  // Enough to run our own page and no more. Notably absent: allow-top-navigation,
  // which would let a compromised panel redirect the whole host page.
  panel.setAttribute("sandbox", "allow-scripts allow-same-origin allow-forms");
  // Loaded lazily so a visitor who never opens it costs one request, not two.
  var loaded = false;

  function open() {
    if (!loaded) {
      panel.src = BASE + "/widget/frame";
      loaded = true;
    }
    panel.classList.add("on");
    btn.setAttribute("aria-expanded", "true");
    dot.classList.remove("on");
    try {
      panel.contentWindow.postMessage({ spooldesk: "opened" }, BASE);
    } catch (e) {
      /* not loaded yet; the frame asks for focus itself once it is */
    }
  }

  function close() {
    panel.classList.remove("on");
    btn.setAttribute("aria-expanded", "false");
  }

  // Held rather than sent, because the panel does not exist until someone
  // opens it. A site calls identify() on page load; the frame may not be
  // built for another ten minutes, or ever.
  var claimed = null;

  function handOver() {
    if (!claimed) return;
    try {
      panel.contentWindow.postMessage({ spooldesk: "identify", identity: claimed }, BASE);
    } catch (e) {
      /* not up yet — it says "ready" when it is, and we try again then */
    }
  }

  btn.setAttribute("aria-expanded", "false");
  btn.addEventListener("click", function () {
    if (panel.classList.contains("on")) close();
    else open();
  });

  // Escape closes it. On a mobile full-screen panel there is otherwise
  // nothing to click past, and the host page is unreachable.
  window.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && panel.classList.contains("on")) close();
  });

  window.addEventListener("message", function (e) {
    // Anyone can postMessage at a window. Everything below acts on the
    // host page, so the origin check is the only thing making that safe.
    if (e.origin !== BASE) return;
    var msg = e.data || {};
    if (msg.spooldesk === "ready") handOver();
    if (msg.spooldesk === "close") close();
    if (msg.spooldesk === "unread" && !panel.classList.contains("on")) dot.classList.add("on");
  });

  // The public API. Small on purpose: a site should be able to read the
  // whole thing in the time it takes to decide whether to paste the snippet.
  //
  //   Spooldesk.identify({ name, email, userId, hash })
  //
  // `hash` is HMAC-SHA256(secret, userId || email), computed on the site's
  // *server*. Without it the identity still reaches the operator, marked as
  // claimed rather than verified — which is the honest label for anything a
  // page can be made to say by opening devtools.
  var prior = window.Spooldesk;
  var api = {
    identify: function (data) {
      if (!data || typeof data !== "object") return;
      claimed = claimed || {};
      if (data.name) claimed.name = data.name;
      if (data.email) claimed.email = data.email;
      if (data.userId) claimed.userId = data.userId;
      if (data.hash) claimed.hash = data.hash;
      handOver();
    },
    open: open,
    close: close,
    // Ends the visitor's session on this browser: their next message starts
    // a new conversation as a new person. What a site calls on log out, and
    // the reason it matters is a shared computer.
    reset: function () {
      claimed = null;
      try {
        panel.contentWindow.postMessage({ spooldesk: "reset" }, BASE);
      } catch (e) { /* nothing loaded, nothing to forget */ }
    },
  };
  // This script is async, so a page that calls identify() straight after the
  // tag would otherwise throw. A stub collecting calls into `q` is the usual
  // answer; honour it if one is there.
  if (prior && prior.q && prior.q.length) {
    prior.q.forEach(function (call) {
      var fn = api[call && call[0]];
      if (fn) fn(call[1]);
    });
  }
  window.Spooldesk = api;

  root.appendChild(style);
  root.appendChild(panel);
  root.appendChild(btn);
  document.body.appendChild(host);
})();
