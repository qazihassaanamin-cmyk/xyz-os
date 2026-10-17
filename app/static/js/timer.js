/* ===========================================================================
   Tavryc — focus timer
   The server owns the clock. This file only renders it.

   Responsibilities:
     * poll GET /focus/state for the authoritative elapsed time
     * render a local countdown between polls so the display does not stutter
     * re-sync immediately when the tab becomes visible again
     * never decide on its own that a session is complete

   The timer cannot be spoofed by editing this file: elapsed time is computed
   from stored timestamps, and finishing is validated server-side.
   =========================================================================== */

(function () {
  "use strict";

  var root = document.querySelector("[data-timer]");
  if (!root) return;

  var clockEl = root.querySelector("[data-timer-clock]");
  var statusEl = root.querySelector("[data-timer-status]");
  var stateUrl = root.getAttribute("data-state-url");
  var finishUrl = root.getAttribute("data-finish-url");

  var POLL_MS = 30000;        // authoritative re-sync
  var TICK_MS = 1000;         // local render only

  var state = {
    active: false,
    status: null,
    sessionId: null,
    elapsed: 0,
    minimum: 1200,
    canFinish: false,
    paused: false
  };

  var lastSyncAt = Date.now();
  var tickHandle = null;
  var pollHandle = null;

  function pad(n) {
    return n < 10 ? "0" + n : String(n);
  }

  function format(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    var h = Math.floor(seconds / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    var s = seconds % 60;
    if (h > 0) return h + ":" + pad(m) + ":" + pad(s);
    return pad(m) + ":" + pad(s);
  }

  function render() {
    if (!clockEl) return;

    var elapsed = state.elapsed;
    if (state.active && !state.paused) {
      // Purely a display interpolation between server confirmations.
      elapsed += Math.floor((Date.now() - lastSyncAt) / 1000);
    }

    clockEl.textContent = format(elapsed);
    clockEl.classList.toggle("paused", !!state.paused);

    if (statusEl) {
      if (!state.active) {
        statusEl.textContent = "No session running.";
      } else if (state.paused) {
        statusEl.textContent = "Paused. Paused time is not counted.";
      } else if (elapsed >= state.minimum) {
        statusEl.textContent = "Past the minimum. You can finish whenever you are ready.";
      } else {
        var remaining = state.minimum - elapsed;
        statusEl.textContent =
          "Minimum " + Math.round(state.minimum / 60) + " minutes — " +
          Math.floor(remaining / 60) + "m " + pad(remaining % 60) + "s to go.";
      }
    }

    // The finish control reflects the server's view, not the local one.
    document.querySelectorAll("[data-timer-finish]").forEach(function (btn) {
      var allowed = state.active && state.canFinish;
      btn.disabled = !allowed;
      btn.setAttribute("aria-disabled", allowed ? "false" : "true");
    });
  }

  function applyState(payload) {
    state.active = !!payload.active;
    state.status = payload.status;
    state.sessionId = payload.session_id;
    state.elapsed = payload.elapsed_seconds || 0;
    state.minimum = payload.minimum_seconds || 1200;
    state.canFinish = !!payload.can_finish;
    state.paused = !!payload.is_paused;
    lastSyncAt = Date.now();
    render();
  }

  function sync() {
    if (!stateUrl) return;
    fetch(stateUrl, {
      method: "GET",
      headers: { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" },
      credentials: "same-origin"
    })
      .then(function (response) {
        if (response.status === 401) {
          window.location.reload();
          return null;
        }
        return response.ok ? response.json() : null;
      })
      .then(function (payload) {
        if (payload && payload.ok) applyState(payload);
      })
      .catch(function () {
        /* A dropped poll is not fatal: the display keeps its last known state
           and the next poll corrects it. */
      });
  }

  function startTicking() {
    if (tickHandle) return;
    tickHandle = window.setInterval(render, TICK_MS);
  }

  // Re-sync the moment the member returns to the tab. A phone that was locked
  // for ten minutes must not show a stale countdown.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
      sync();
    }
  });

  window.addEventListener("online", sync);

  // Finish is submitted as a form post so it works without JavaScript too; this
  // handler only prevents a double submit.
  document.querySelectorAll("form[data-timer-form]").forEach(function (form) {
    form.addEventListener("submit", function () {
      var button = form.querySelector("[type=submit]");
      if (button) {
        button.setAttribute("data-loading", "true");
        button.disabled = true;
      }
    });
  });

  if (state.active) startTicking();
  render();
  sync();
  pollHandle = window.setInterval(sync, POLL_MS);

  window.addEventListener("beforeunload", function () {
    if (tickHandle) window.clearInterval(tickHandle);
    if (pollHandle) window.clearInterval(pollHandle);
  });
})();
