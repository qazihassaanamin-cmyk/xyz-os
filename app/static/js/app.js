/* ===========================================================================
   Tavryc — application shell behaviour
   Sidebar toggle with a focus trap, user menu, and the notification bell.

   Progressive enhancement only: with JavaScript disabled every page still
   works, because navigation is ordinary links and forms post normally.
   =========================================================================== */

(function () {
  "use strict";

  /* --- Off-canvas sidebar (below 1024px) -------------------------------- */

  var sidebar = document.querySelector("[data-sidebar]");
  var scrim = document.querySelector("[data-sidebar-scrim]");
  var toggle = document.querySelector("[data-sidebar-toggle]");

  var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  function openSidebar() {
    if (!sidebar) return;
    sidebar.setAttribute("data-open", "true");
    if (scrim) scrim.hidden = false;
    if (toggle) toggle.setAttribute("aria-expanded", "true");
    // Focus moves into the menu, and is trapped there until it closes.
    var first = sidebar.querySelector(FOCUSABLE);
    if (first) first.focus();
    document.body.style.overflow = "hidden";
  }

  function closeSidebar() {
    if (!sidebar) return;
    sidebar.setAttribute("data-open", "false");
    if (scrim) scrim.hidden = true;
    if (toggle) {
      toggle.setAttribute("aria-expanded", "false");
      toggle.focus();
    }
    document.body.style.overflow = "";
  }

  if (toggle) {
    toggle.addEventListener("click", function () {
      var isOpen = sidebar && sidebar.getAttribute("data-open") === "true";
      if (isOpen) closeSidebar();
      else openSidebar();
    });
  }

  if (scrim) scrim.addEventListener("click", closeSidebar);

  document.addEventListener("keydown", function (event) {
    if (!sidebar || sidebar.getAttribute("data-open") !== "true") return;

    if (event.key === "Escape") {
      event.preventDefault();
      closeSidebar();
      return;
    }

    if (event.key === "Tab") {
      var items = Array.prototype.filter.call(
        sidebar.querySelectorAll(FOCUSABLE),
        function (el) { return el.offsetParent !== null; }
      );
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

  // Returning to a wide viewport must not leave the menu stranded open.
  window.addEventListener("resize", function () {
    if (window.innerWidth > 1024 && sidebar && sidebar.getAttribute("data-open") === "true") {
      closeSidebar();
    }
  });

  /* --- Dropdowns: user menu and notifications --------------------------- */

  function wireDropdown(triggerSelector, panelSelector) {
    var trigger = document.querySelector(triggerSelector);
    var panel = document.querySelector(panelSelector);
    if (!trigger || !panel) return;

    trigger.addEventListener("click", function (event) {
      event.stopPropagation();
      var isOpen = !panel.hidden;
      document.querySelectorAll("[data-dropdown]").forEach(function (other) {
        other.hidden = true;
      });
      panel.hidden = isOpen;
      trigger.setAttribute("aria-expanded", String(!isOpen));
    });

    panel.addEventListener("click", function (event) {
      event.stopPropagation();
    });
  }

  wireDropdown("[data-user-menu-trigger]", "[data-user-menu]");
  wireDropdown("[data-notifications-trigger]", "[data-notifications-panel]");

  document.addEventListener("click", function () {
    document.querySelectorAll("[data-dropdown]").forEach(function (panel) {
      panel.hidden = true;
    });
  });

  /* --- Notification bell ------------------------------------------------ */

  var bell = document.querySelector("[data-notifications-trigger]");
  var bellUrl = bell && bell.getAttribute("data-notifications-url");

  function refreshNotifications() {
    if (!bellUrl) return;
    fetch(bellUrl, {
      headers: { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" },
      credentials: "same-origin"
    })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (data) {
        if (!data) return;
        var badge = document.querySelector("[data-notifications-count]");
        if (badge) {
          if (data.unread > 0) {
            badge.textContent = data.unread > 99 ? "99+" : String(data.unread);
            badge.hidden = false;
          } else {
            badge.hidden = true;
          }
        }
      })
      .catch(function () { /* silent: the badge is not critical */ });
  }

  if (bell) {
    // Refresh when the menu is opened, and once a minute in the background.
    bell.addEventListener("click", refreshNotifications);
    window.setInterval(refreshNotifications, 60000);
  }

  /* --- Loading state on submit buttons --------------------------------- */

  document.querySelectorAll("form[data-loading-form]").forEach(function (form) {
    form.addEventListener("submit", function () {
      var button = form.querySelector("[type=submit]");
      if (!button || button.disabled) return;
      button.setAttribute("data-loading", "true");
      button.setAttribute("aria-busy", "true");
    });
  });

  /* --- Confirm destructive actions -------------------------------------- */

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      var message = form.getAttribute("data-confirm") || "Are you sure?";
      if (!window.confirm(message)) {
        event.preventDefault();
      }
    });
  });
})();
