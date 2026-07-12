/* Shared JSON request transport.
 *
 * Callers provide a `json` value and never encode request bodies themselves.
 * Keeping the only HTTP JSON.stringify call here prevents double encoding and
 * gives the login, error reporter, and authenticated API helper one contract.
 */
(function () {
  'use strict';

  var RPI = window.RPI = window.RPI || {};
  var owns = Object.prototype.hasOwnProperty;

  function jsonFetch(path, options) {
    options = options || {};
    if (owns.call(options, 'body')) {
      return Promise.reject(new TypeError('JSON request callers must pass json, not body'));
    }

    var hasJson = owns.call(options, 'json') && options.json !== undefined;
    if (hasJson && (options.json === null || typeof options.json !== 'object')) {
      return Promise.reject(new TypeError('JSON request payload must be an object'));
    }

    var headers = Object.assign({}, options.headers || {});
    if (hasJson) headers['Content-Type'] = 'application/json';

    return window.fetch(path, {
      method: options.method || 'GET',
      headers: headers,
      credentials: options.credentials || 'same-origin',
      keepalive: !!options.keepalive,
      cache: options.cache,
      redirect: options.redirect,
      signal: options.signal,
      body: hasJson ? JSON.stringify(options.json) : undefined
    });
  }

  RPI.jsonFetch = jsonFetch;
})();
