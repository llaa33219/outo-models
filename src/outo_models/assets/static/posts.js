/* Posts modals — close handlers for the pure-CSS <details> overlays.
 *
 * The overlays are native <details data-modal> disclosures. Opening
 * needs no script, but a same-page fragment link cannot close one
 * (no navigation happens), so closing is wired here: the close button,
 * a click on the backdrop itself, and Escape all shut the nearest open
 * modal. No dependencies; CSP: script-src 'self'.
 */
(function () {
  "use strict";

  function closeModal(el) {
    var details = el.closest("details[data-modal]");
    if (details) {
      details.open = false;
    }
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!(target instanceof Element)) {
      return;
    }
    if (target.closest("[data-modal-close]")) {
      closeModal(target);
      return;
    }
    if (target.matches(".post-modal__overlay, .post-picker__overlay")) {
      closeModal(target);
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") {
      return;
    }
    document.querySelectorAll("details[data-modal][open]").forEach(function (d) {
      d.open = false;
    });
  });
})();
