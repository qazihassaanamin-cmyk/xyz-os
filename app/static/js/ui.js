/* ===========================================================================
   Tavryc — small page behaviours
   Password reveal, vote buttons, checklist toggles, character counters.
   All optional: every control works as a normal form submission without this.
   =========================================================================== */

(function () {
  "use strict";

  /* --- Password reveal -------------------------------------------------- */

  document.querySelectorAll("[data-password-toggle]").forEach(function (button) {
    var field = document.getElementById(button.getAttribute("data-password-toggle"));
    if (!field) return;

    button.addEventListener("click", function () {
      var showing = field.type === "text";
      field.type = showing ? "password" : "text";
      button.textContent = showing ? "Show" : "Hide";
      button.setAttribute("aria-pressed", String(!showing));
      field.focus();
    });
  });

  /* --- Vote toggle ------------------------------------------------------ */

  document.querySelectorAll("[data-vote-url]").forEach(function (button) {
    button.addEventListener("click", function () {
      var url = button.getAttribute("data-vote-url");
      var csrf = document.querySelector("meta[name='csrf-token']");
      button.disabled = true;

      fetch(url, {
        method: "POST",
        headers: {
          "X-Requested-With": "XMLHttpRequest",
          "Accept": "application/json",
          "X-CSRFToken": csrf ? csrf.getAttribute("content") : ""
        },
        credentials: "same-origin"
      })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) {
          if (!data || !data.ok) return;
          var count = button.querySelector("[data-vote-count]");
          if (count) count.textContent = data.votes;
          button.setAttribute("aria-pressed", String(data.voted));
          button.classList.toggle("btn-primary", data.voted);
        })
        .catch(function () { /* the page still works on reload */ })
        .then(function () { button.disabled = false; });
    });
  });

  /* --- Checklist toggle ------------------------------------------------- */

  document.querySelectorAll("[data-checklist-url]").forEach(function (box) {
    box.addEventListener("change", function () {
      var url = box.getAttribute("data-checklist-url");
      var csrf = document.querySelector("meta[name='csrf-token']");
      box.disabled = true;

      fetch(url, {
        method: "POST",
        headers: {
          "X-Requested-With": "XMLHttpRequest",
          "Accept": "application/json",
          "X-CSRFToken": csrf ? csrf.getAttribute("content") : ""
        },
        credentials: "same-origin"
      })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) {
          if (!data || !data.ok) return;
          var bar = document.querySelector("[data-checklist-progress]");
          if (bar) bar.style.width = data.progress + "%";
          var label = document.querySelector("[data-checklist-label]");
          if (label) label.textContent = data.done + " of " + data.total + " complete";
        })
        .catch(function () { /* falls back to a normal reload */ })
        .then(function () { box.disabled = false; });
    });
  });

  /* --- Character counters ----------------------------------------------- */

  document.querySelectorAll("[data-counter-for]").forEach(function (counter) {
    var field = document.getElementById(counter.getAttribute("data-counter-for"));
    if (!field) return;
    var max = parseInt(field.getAttribute("maxlength") || "0", 10);

    function update() {
      var used = field.value.length;
      counter.textContent = max ? used + " / " + max : String(used);
    }
    field.addEventListener("input", update);
    update();
  });

  /* --- Reveal a section that is collapsed by default -------------------- */

  document.querySelectorAll("[data-reveal-toggle]").forEach(function (button) {
    button.addEventListener("click", function () {
      var target = document.getElementById(button.getAttribute("data-reveal-toggle"));
      if (!target) return;
      var hidden = target.hidden;
      target.hidden = !hidden;
      button.setAttribute("aria-expanded", String(hidden));
      button.textContent = hidden
        ? button.getAttribute("data-label-hide") || "Hide"
        : button.getAttribute("data-label-show") || "Show";
    });
  });

  /* --- Dismiss an inline alert ------------------------------------------ */

  document.querySelectorAll("[data-dismiss]").forEach(function (button) {
    button.addEventListener("click", function () {
      var alert = button.closest(".alert");
      if (alert) alert.remove();
    });
  });
})();
