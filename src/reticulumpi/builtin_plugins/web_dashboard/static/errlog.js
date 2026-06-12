/*
 * errlog.js -- minimal client-side error reporter.
 *
 * Purpose: pipe uncaught browser exceptions and unhandled promise
 * rejections back to the Pi (journalctl) so client-only failures -- which
 * never reach the server on their own -- become diagnosable. Also exposes
 * window.__rpiReportError(err, context) for safeWire() and manual testing.
 *
 * CONSTRAINT: the dashboard is cookie-auth + CSRF-protected, and the CSRF
 * middleware requires an X-Requested-With header on POSTs. That rules out
 * navigator.sendBeacon (it cannot set custom headers), so we use
 * fetch(keepalive:true) with the header set instead. The page is auth-gated,
 * so the session cookie already exists at load -- no queue/retry needed.
 *
 * Listener-only, ES5, must load (defer) BEFORE app.js so it catches app.js's
 * own eval-time errors. Never throws: the whole send path is try/catch'd.
 */
(function () {
  "use strict";
  if (typeof window.fetch !== "function") {
    return; // very old browser: skip silently
  }
  var MAX_REPORTS = 5;
  var sent = 0;
  var seen = {};

  function trunc(s, n) {
    s = s == null ? "" : String(s);
    return s.length > n ? s.slice(0, n) : s;
  }

  function report(payload) {
    try {
      if (sent >= MAX_REPORTS) {
        return;
      }
      var sig =
        (payload.message || "") + "@" + (payload.source || "") + ":" + (payload.line || "");
      if (seen[sig]) {
        return;
      }
      seen[sig] = true;
      sent += 1;
      var body = {
        message: trunc(payload.message, 1024),
        source: trunc(payload.source, 512),
        line: payload.line || 0,
        col: payload.col || 0,
        stack: trunc(payload.stack, 4096),
        url: trunc(location.href, 512),
        ua: trunc(navigator.userAgent, 512)
      };
      window
        .fetch("/api/client_error", {
          method: "POST",
          keepalive: true,
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            Accept: "application/json"
          },
          body: JSON.stringify(body)
        })
        .catch(function () {});
    } catch (e) {
      /* the reporter must never throw */
    }
  }

  window.__rpiReportError = function (errOrMsg, context) {
    if (errOrMsg && typeof errOrMsg === "object") {
      report({
        message: errOrMsg.message || String(errOrMsg),
        source: context || "",
        line: 0,
        col: 0,
        stack: errOrMsg.stack || ""
      });
    } else {
      report({ message: String(errOrMsg), source: context || "", line: 0, col: 0, stack: "" });
    }
  };

  window.addEventListener("error", function (event) {
    report({
      message: event.message || "uncaught error",
      source: event.filename || "",
      line: event.lineno || 0,
      col: event.colno || 0,
      stack: event.error && event.error.stack ? event.error.stack : ""
    });
  });

  window.addEventListener("unhandledrejection", function (event) {
    var reason = event.reason;
    report({
      message: "unhandledrejection: " + String(reason),
      source: "",
      line: 0,
      col: 0,
      stack: reason && reason.stack ? reason.stack : ""
    });
  });
})();
