// Live (SSE) updates replace whole cards. That would close a dropdown or menu the user has open,
// so a swap into an element that holds an open <details> or a focused <select> waits until it
// is closed (or 30 s), then the newest message is applied.
(function () {
  var MAX_HOLD_MS = 30000;
  var held = new Map(); // swap target element -> {data, since}

  function busy(elt) {
    var active = document.activeElement;
    return !!(elt.querySelector("details[open]") ||
      (active && active.tagName === "SELECT" && elt.contains(active)));
  }

  function apply(elt, data) {
    var target = elt.getAttribute("hx-target") ? document.querySelector(elt.getAttribute("hx-target")) : elt;
    htmx.swap(target || elt, data, { swapStyle: elt.getAttribute("hx-swap") || "innerHTML" });
  }

  function flush(force) {
    held.forEach(function (item, elt) {
      if (!document.body.contains(elt)) { held.delete(elt); return; }
      if (force || !busy(elt) || Date.now() - item.since > MAX_HOLD_MS) {
        held.delete(elt);
        apply(elt, item.data);
      }
    });
  }

  document.addEventListener("htmx:sseBeforeMessage", function (e) {
    var elt = e.target;
    var prior = held.get(elt);
    if (!prior && !busy(elt)) return;
    held.set(elt, { data: e.detail.data, since: prior ? prior.since : Date.now() });
    e.preventDefault();
  });

  // The user's own request replaced the card: a held live message is older than that.
  document.addEventListener("htmx:afterSwap", function (e) { held.delete(e.target); });

  ["focusout", "change", "toggle"].forEach(function (name) {
    document.addEventListener(name, function (e) {
      // A chosen option leaves the <select> focused but closed; let go of it so updates resume.
      if (name === "change" && e.target.tagName === "SELECT") e.target.blur();
      setTimeout(function () { flush(false); }, 0);
    }, true);
  });
  setInterval(function () { if (held.size) flush(false); }, 2000);
})();
