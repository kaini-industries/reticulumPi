(function() {
  'use strict';
  var params = new URLSearchParams(window.location.search);
  var err = params.get('error');
  if (err) {
    var el = document.getElementById('error');
    if (err === 'rate_limited') el.textContent = 'Too many attempts. Please wait and try again.';
    else if (err === 'empty') el.textContent = 'Password is required.';
    else el.textContent = 'Invalid password.';
    el.hidden = false;
  }

  // AJAX login — avoids full page reload, uses X-Requested-With for CSRF
  var form = document.getElementById('login-form');
  if (form) {
    form.addEventListener('submit', function(e) {
      e.preventDefault();
      var pw = document.getElementById('password');
      var btn = document.getElementById('login-btn');
      var errEl = document.getElementById('error');
      if (!pw || !pw.value) {
        errEl.textContent = 'Password is required.';
        errEl.hidden = false;
        return;
      }
      btn.disabled = true;
      btn.textContent = 'Logging in...';
      errEl.hidden = true;

      fetch('/api/auth/login', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
          'X-Requested-With': 'XMLHttpRequest'
        },
        credentials: 'same-origin',
        body: JSON.stringify({ password: pw.value })
      }).then(function(r) {
        if (r.ok) {
          window.location.href = '/';
          return;
        }
        return r.json().catch(function() { return {}; }).then(function(data) {
          if (r.status === 429) {
            errEl.textContent = 'Too many attempts. Please wait and try again.';
          } else {
            errEl.textContent = (data && data.error) || 'Invalid password.';
          }
          errEl.hidden = false;
          btn.disabled = false;
          btn.textContent = 'Login';
        });
      }).catch(function() {
        errEl.textContent = 'Connection error. Please try again.';
        errEl.hidden = false;
        btn.disabled = false;
        btn.textContent = 'Login';
      });
    });
  }
})();
