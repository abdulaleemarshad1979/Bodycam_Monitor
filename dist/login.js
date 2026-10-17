'use strict';
const $ = (id) => document.getElementById(id);
const hosted = location.hostname.endsWith('.chatgpt.site');
let defaultBase = localStorage.getItem('bodycam_api') || (hosted ? '' : (location.origin !== 'null' ? location.origin : ''));
if ($('server-url')) $('server-url').value = defaultBase;

$('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const errorEl = $('login-error');
  const submitBtn = $('login-submit');
  errorEl.textContent = '';
  submitBtn.disabled = true;

  try {
    const rawVal = ($('server-url').value || defaultBase || (location.origin !== 'null' ? location.origin : '')).trim();
    if (!rawVal) throw new Error('Enter a server URL.');
    const targetUrl = /^https?:\/\//i.test(rawVal) ? rawVal : (location.protocol === 'https:' ? 'https://' : 'http://') + rawVal;
    const u = new URL(targetUrl);
    if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password || u.search || u.hash) {
      throw new Error('Enter a server URL without credentials, query parameters or fragments.');
    }
    if (location.protocol === 'https:' && u.protocol !== 'https:') {
      throw new Error('This hosted dashboard needs an HTTPS backend address.');
    }
    const cleanBase = u.href.replace(/\/+$/, '');

    const res = await fetch(cleanBase + '/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: $('username').value.trim(),
        password: $('password').value
      })
    });

    if (!res.ok) {
      let msg = 'Authentication failed (' + res.status + ')';
      try {
        const body = await res.json();
        msg = Array.isArray(body.detail) ? body.detail.map(x => x.msg).join('; ') : (body.detail || msg);
      } catch {}
      throw new Error(msg);
    }

    const data = await res.json();
    localStorage.setItem('bodycam_api', cleanBase);
    sessionStorage.setItem('bodycam_token', data.token);
    sessionStorage.setItem('bodycam_role', data.role);
    window.location.replace('/');
  } catch (err) {
    errorEl.textContent = err.message || 'Login failed';
  } finally {
    submitBtn.disabled = false;
  }
});
