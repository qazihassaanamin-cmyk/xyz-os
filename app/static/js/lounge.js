/* ===========================================================================
   Tavryc — The Lounge
   Short polling with incremental fetch. No long-lived connection, no infinite
   server loop: the client asks for messages newer than the highest id it has
   and stops when the tab is hidden.
   =========================================================================== */

(function () {
  "use strict";

  var root = document.querySelector("[data-lounge]");
  if (!root) return;

  var listEl = root.querySelector("[data-lounge-list]");
  var apiUrl = root.getAttribute("data-api-url");
  var topic = root.getAttribute("data-topic") || "general";
  var pollSeconds = parseInt(root.getAttribute("data-poll-seconds") || "15", 10);
  var intervalMs = Math.max(10, Math.min(pollSeconds, 60)) * 1000;

  var lastId = parseInt(root.getAttribute("data-latest-id") || "0", 10);
  var handle = null;
  var inFlight = false;

  function escapeText(value) {
    // The server also escapes; this keeps textContent assignment honest.
    return value == null ? "" : String(value);
  }

  function buildMessage(message) {
    var wrapper = document.createElement("article");
    wrapper.className = "chat-msg";
    wrapper.id = "m" + message.id;
    if (message.parent_id) wrapper.classList.add("is-reply");
    if (message.is_hidden) wrapper.classList.add("removed");

    var head = document.createElement("div");
    head.className = "chat-head";

    var author = document.createElement("span");
    author.className = "chat-author";
    author.textContent = escapeText(message.author_name);
    head.appendChild(author);

    if (message.author_is_ceo) {
      var tag = document.createElement("span");
      tag.className = "pill no-dot";
      tag.textContent = "CEO";
      head.appendChild(tag);
    }

    var when = document.createElement("time");
    when.className = "chat-when";
    when.dateTime = escapeText(message.created_at);
    when.textContent = "just now";
    head.appendChild(when);

    if (message.is_edited) {
      var edited = document.createElement("span");
      edited.className = "chat-when";
      edited.textContent = "· edited";
      head.appendChild(edited);
    }

    wrapper.appendChild(head);

    var body = document.createElement("p");
    body.className = "chat-body";
    body.textContent = message.is_hidden ? "Message removed." : escapeText(message.body);
    wrapper.appendChild(body);

    return wrapper;
  }

  function appendMessages(messages) {
    if (!listEl || !messages.length) return;
    var empty = listEl.querySelector("[data-lounge-empty]");
    if (empty) empty.remove();

    messages.forEach(function (message) {
      if (document.getElementById("m" + message.id)) return;
      listEl.appendChild(buildMessage(message));
      if (message.id > lastId) lastId = message.id;
    });

    // Newest at the bottom; keep the view pinned there when we were already there.
    var nearBottom =
      window.innerHeight + window.scrollY >= document.body.offsetHeight - 240;
    if (nearBottom) {
      var latest = document.getElementById("m" + lastId);
      if (latest && latest.scrollIntoView) {
        latest.scrollIntoView({ block: "end" });
      }
    }
  }

  function poll() {
    if (!apiUrl || inFlight || document.hidden) return;
    inFlight = true;

    var url = apiUrl + "?topic=" + encodeURIComponent(topic) + "&after=" + lastId;
    fetch(url, {
      headers: { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" },
      credentials: "same-origin"
    })
      .then(function (response) {
        if (response.status === 401) {
          window.location.href = "/login";
          return null;
        }
        if (response.status === 429) return null;   // rate limited; try next tick
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        if (data && data.ok && data.messages && data.messages.length) {
          appendMessages(data.messages);
        }
      })
      .catch(function () { /* a missed poll is harmless */ })
      .then(function () { inFlight = false; });
  }

  function start() {
    if (handle) return;
    handle = window.setInterval(poll, intervalMs);
  }

  function stop() {
    if (handle) {
      window.clearInterval(handle);
      handle = null;
    }
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      stop();
    } else {
      poll();
      start();
    }
  });

  window.addEventListener("online", poll);

  // Only poll while the page is visible, so a background tab costs nothing.
  if (!document.hidden) {
    start();
    poll();
  }
})();
