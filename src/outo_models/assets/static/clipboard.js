// clipboard.js — shared copy-button handler for every page.
// External file because CSP is script-src 'self' (no inline scripts).
// navigator.clipboard needs a secure context; on plain-http internal
// installs we fall back to a hidden textarea + execCommand('copy').
(function () {
  "use strict";

  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "absolute";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
    } catch (e) {
      /* ignore — the user will copy manually */
    }
    document.body.removeChild(ta);
  }

  function bindCopyButtons() {
    document.querySelectorAll(".copy-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var targetId = btn.getAttribute("data-copy-target");
        if (!targetId) return;
        var node = document.getElementById(targetId);
        if (!node) return;
        var text = node.textContent || "";
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).catch(function () {
            fallbackCopy(text);
          });
        } else {
          fallbackCopy(text);
        }
        var original = btn.textContent;
        btn.textContent = "Copied";
        setTimeout(function () {
          btn.textContent = original;
        }, 1500);
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindCopyButtons);
  } else {
    bindCopyButtons();
  }
})();
