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
})();
