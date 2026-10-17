/* ===========================================================================
   Tavryc — uploads and the evidence lightbox

   Client-side checks here exist only to fail fast and give a better message.
   The server re-validates real file content, so bypassing this file gains
   nothing.
   =========================================================================== */

(function () {
  "use strict";

  /* --- File input feedback --------------------------------------------- */

  document.querySelectorAll("[data-upload-input]").forEach(function (input) {
    var maxFiles = parseInt(input.getAttribute("data-max-files") || "3", 10);
    var maxMb = parseFloat(input.getAttribute("data-max-mb") || "5");
    var target = document.querySelector(input.getAttribute("data-upload-list"));
    var submit = document.querySelector(input.getAttribute("data-upload-submit"));

    function describe() {
      if (!target) return;
      target.textContent = "";
      var files = Array.prototype.slice.call(input.files || []);
      if (!files.length) return;

      var list = document.createElement("ul");
      list.className = "tag-list";

      files.forEach(function (file) {
        var item = document.createElement("li");
        item.className = "tag";
        var size = file.size < 1024 * 1024
          ? Math.max(1, Math.round(file.size / 1024)) + " KB"
          : (file.size / (1024 * 1024)).toFixed(1) + " MB";
        item.textContent = file.name + " · " + size;
        if (file.size > maxMb * 1024 * 1024) {
          item.classList.add("tone-critical");
          item.textContent += " — too large";
        }
        list.appendChild(item);
      });

      target.appendChild(list);

      var overCount = files.length > maxFiles;
      var overSize = files.some(function (file) { return file.size > maxMb * 1024 * 1024; });

      if (overCount) {
        var warning = document.createElement("p");
        warning.className = "field-error";
        warning.textContent = "Attach at most " + maxFiles + " files.";
        target.appendChild(warning);
      }
      if (overSize) {
        var sizeWarning = document.createElement("p");
        sizeWarning.className = "field-error";
        sizeWarning.textContent = "Each file must be " + maxMb + " MB or smaller.";
        target.appendChild(sizeWarning);
      }
      if (submit) {
        submit.disabled = overCount || overSize;
      }
    }

    input.addEventListener("change", describe);
  });

  /* --- Lightbox --------------------------------------------------------- */

  var lightbox = document.querySelector("[data-lightbox]");
  if (!lightbox) return;

  var image = lightbox.querySelector("[data-lightbox-image]");
  var caption = lightbox.querySelector("[data-lightbox-caption]");
  var counter = lightbox.querySelector("[data-lightbox-counter]");
  var triggers = Array.prototype.slice.call(document.querySelectorAll("[data-lightbox-trigger]"));

  var index = 0;
  var lastFocused = null;

  function show(i) {
    if (!triggers.length) return;
    index = (i + triggers.length) % triggers.length;
    var trigger = triggers[index];
    var full = trigger.getAttribute("data-full-url");
    var label = trigger.getAttribute("data-caption") || "";

    if (image) {
      image.src = full;
      image.alt = label || "Evidence image";
    }
    if (caption) caption.textContent = label;
    if (counter) counter.textContent = (index + 1) + " of " + triggers.length;
  }

  function open(i) {
    lastFocused = document.activeElement;
    show(i);
    lightbox.hidden = false;
    document.body.style.overflow = "hidden";
    var closeButton = lightbox.querySelector("[data-lightbox-close]");
    if (closeButton) closeButton.focus();
  }

  function close() {
    lightbox.hidden = true;
    document.body.style.overflow = "";
    if (lastFocused) lastFocused.focus();
  }

  triggers.forEach(function (trigger, i) {
    trigger.addEventListener("click", function (event) {
      event.preventDefault();
      open(i);
    });
  });

  lightbox.addEventListener("click", function (event) {
    if (event.target === lightbox) close();
  });

  var closeButton = lightbox.querySelector("[data-lightbox-close]");
  if (closeButton) closeButton.addEventListener("click", close);

  var prev = lightbox.querySelector("[data-lightbox-prev]");
  var next = lightbox.querySelector("[data-lightbox-next]");
  if (prev) prev.addEventListener("click", function () { show(index - 1); });
  if (next) next.addEventListener("click", function () { show(index + 1); });

  document.addEventListener("keydown", function (event) {
    if (lightbox.hidden) return;
    if (event.key === "Escape") {
      event.preventDefault();
      close();
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      show(index - 1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      show(index + 1);
    } else if (event.key === "Tab") {
      // Keep focus inside the dialog while it is open.
      var items = lightbox.querySelectorAll("button, a[href]");
      if (!items.length) return;
      var first = items[0];
      var last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  });
})();
