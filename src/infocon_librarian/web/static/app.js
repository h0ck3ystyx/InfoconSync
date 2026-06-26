  (function () {
    'use strict';

    // ---- CSRF token ----
    // Read from the csrftoken cookie set by /bootstrap (double-submit pattern).
    function _getCsrf() {
      const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
      return m ? decodeURIComponent(m[1]) : '';
    }
    const csrf = _getCsrf();

    // ---- Utility ----
    function api(method, path, body) {
      const opts = {
        method,
        headers: { 'Content-Type': 'application/json' }
      };
      if (csrf) opts.headers['X-Csrf-Token'] = csrf;
      if (body !== undefined) opts.body = JSON.stringify(body);
      return fetch('/api' + path, opts).then(r => r.json().then(d => ({ ok: r.ok, status: r.status, data: d })));
    }

    function announce(msg) {
      const el = document.getElementById('live-status');
      el.textContent = msg;
      el.style.display = 'block';
      setTimeout(() => { el.style.display = 'none'; }, 3500);
    }

    function badgeHtml(status) {
      const map = {
        'new': ['badge-new', 'New'],
        'changed_marker': ['badge-changed', 'Changed'],
        'changed_manifest': ['badge-changed', 'Changed'],
        'verified_current': ['badge-verified', 'Verified'],
        'present_unverified': ['badge-unverified', 'Present, unverified'],
        'local_only': ['badge-local-only', 'Local only'],
        'unknown': ['badge-unknown', 'Unknown'],
        'pending': ['badge-pending', 'Pending'],
        'blocked': ['badge-blocked', 'Blocked'],
        'complete': ['badge-complete', 'Complete'],
        'failed': ['badge-failed', 'Failed'],
      };
      const [cls, label] = map[status] || ['badge-unknown', status];
      return `<span class="badge ${cls}">${label}</span>`;
    }

    function fmt(n) {
      if (n == null) return '—';
      if (n >= 1e9) return (n / 1e9).toFixed(1) + ' GB';
      if (n >= 1e6) return (n / 1e6).toFixed(1) + ' MB';
      if (n >= 1e3) return (n / 1e3).toFixed(1) + ' KB';
      return n + ' B';
    }

    // ---- Tab switching ----
    const tabs = document.querySelectorAll('[role="tab"]');
    const panels = document.querySelectorAll('.tab-panel');
    tabs.forEach(tab => {
      tab.addEventListener('click', () => {
        tabs.forEach(t => t.setAttribute('aria-selected', 'false'));
        panels.forEach(p => p.classList.remove('active'));
        tab.setAttribute('aria-selected', 'true');
        document.getElementById(tab.getAttribute('aria-controls')).classList.add('active');
      });
      tab.addEventListener('keydown', e => {
        const list = [...tabs];
        const idx = list.indexOf(tab);
        let next = null;
        if (e.key === 'ArrowRight') next = list[(idx + 1) % list.length];
        if (e.key === 'ArrowLeft')  next = list[(idx - 1 + list.length) % list.length];
        if (next) { next.click(); next.focus(); e.preventDefault(); }
      });
    });

    // ---- Health / connection status ----
    function updateConnStatus(ok) {
      const dot   = document.getElementById('conn-dot');
      const label = document.getElementById('conn-label');
      dot.className   = 'dot ' + (ok ? 'dot-ok' : 'dot-err');
      label.textContent = ok ? 'connected' : 'error';
    }

    api('GET', '/health').then(({ ok, data }) => {
      updateConnStatus(ok && data.status === 'ok');
    }).catch(() => updateConnStatus(false));

    // ---- SSE ----
    let evtSource = null;
    function connectSSE() {
      if (evtSource) return;
      evtSource = new EventSource('/api/events');
      evtSource.onmessage = e => {
        try {
          const ev = JSON.parse(e.data);
          handleSSEEvent(ev);
        } catch (_) {}
      };
      evtSource.onerror = () => {
        evtSource.close();
        evtSource = null;
        // Reconnect after delay — reload state from API first
        setTimeout(() => { refreshAllState(); connectSSE(); }, 3000);
      };
    }

    function handleSSEEvent(ev) {
      if (ev.type === 'check_complete') {
        announce('Check complete — ' + ev.count + ' collections found');
        loadCheck(ev.check_id);
      } else if (ev.type === 'check_failed') {
        announce('Check failed: ' + (ev.error || 'unknown error'));
      } else if (ev.type === 'plan_created') {
        announce('Plan created');
        loadPlans();
      } else if (ev.type === 'plan_started') {
        announce('Transfer started');
        loadTransfers();
      } else if (ev.type === 'item_paused') {
        announce('Item paused');
        loadTransfers();
      } else if (ev.type === 'item_resumed') {
        announce('Item resumed');
        loadTransfers();
      } else if (ev.type === 'progress') {
        updateProgress(ev);
      }
    }

    function refreshAllState() {
      loadPlans();
      loadTransfers();
      loadReceipts();
    }

    connectSSE();

    // ---- Collections ----
    let _collections = [];
    function renderCollections(list) {
      _collections = list;
      const empty  = document.getElementById('collections-empty');
      const table  = document.getElementById('collections-table');
      const tbody  = document.getElementById('collections-body');
      const planBtn = document.getElementById('btn-plan-selected');
      if (!list.length) { empty.hidden = false; table.hidden = true; return; }
      empty.hidden = true;
      table.hidden = false;
      tbody.innerHTML = '';
      list.forEach(c => {
        const tr = document.createElement('tr');
        tr.dataset.key = c.key;
        tr.innerHTML = `
          <td><input type="checkbox" aria-label="Select ${c.display_name || c.key}" class="row-select" data-key="${c.key}"></td>
          <td>${escHtml(c.display_name || c.key)}</td>
          <td>${badgeHtml(c.status)}</td>
          <td>${fmt(c.size_bytes)}</td>
          <td><details><summary>Evidence</summary>
            <div class="evidence-payload">${escHtml(JSON.stringify(c.evidence || {}, null, 2))}</div>
          </details></td>`;
        tbody.appendChild(tr);
      });
      tbody.querySelectorAll('.row-select').forEach(cb => {
        cb.addEventListener('change', updatePlanButton);
      });
      document.getElementById('select-all').addEventListener('change', e => {
        tbody.querySelectorAll('.row-select').forEach(cb => { cb.checked = e.target.checked; });
        updatePlanButton();
      });
      updatePlanButton();
    }

    function updatePlanButton() {
      const any = !!document.querySelector('#collections-body .row-select:checked');
      document.getElementById('btn-plan-selected').disabled = !any;
    }

    function escHtml(s) {
      return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }

    // Filter
    document.getElementById('search-collections').addEventListener('input', applyFilter);
    document.getElementById('filter-status').addEventListener('change', applyFilter);
    function applyFilter() {
      const q = document.getElementById('search-collections').value.toLowerCase();
      const s = document.getElementById('filter-status').value;
      document.querySelectorAll('#collections-body tr').forEach(tr => {
        const key = tr.dataset.key || '';
        const cells = tr.textContent.toLowerCase();
        const matchText = !q || cells.includes(q);
        const matchStatus = !s || tr.innerHTML.includes('badge-' + s.replace('_', '-').replace(/_/g, '-'));
        tr.style.display = (matchText && matchStatus) ? '' : 'none';
      });
    }

    // Check upstream
    let _activeCheckId = null;
    document.getElementById('btn-check-now').addEventListener('click', () => {
      const btn = document.getElementById('btn-check-now');
      btn.disabled = true;
      btn.textContent = 'Checking…';
      announce('Fetching upstream listings…');
      api('POST', '/checks', {}).then(({ ok, data }) => {
        if (ok) {
          _activeCheckId = data.check_id;
          announce('Check running — this may take a minute');
        } else {
          announce('Check failed: ' + (data.error || 'unknown'));
          btn.disabled = false;
          btn.textContent = 'Check upstream';
        }
      });
    });

    function loadCheck(checkId) {
      api('GET', '/checks/' + checkId).then(({ ok, data }) => {
        const btn = document.getElementById('btn-check-now');
        btn.disabled = false;
        btn.textContent = 'Check upstream';
        if (ok && data.results) {
          renderCollections(data.results);
          // Switch to Collections tab
          document.getElementById('btn-collections').click();
        }
      });
    }

    // Plan selected
    document.getElementById('btn-plan-selected').addEventListener('click', () => {
      const selected = [...document.querySelectorAll('#collections-body .row-select:checked')]
        .map(cb => cb.dataset.key);
      if (!selected.length) return;
      api('POST', '/plans', { collection_ids: selected }).then(({ ok, data }) => {
        if (ok) {
          announce('Plan created: ' + data.plan_id);
          document.getElementById('btn-plans').click();
        } else {
          announce('Failed to create plan');
        }
      });
    });

    // ---- Plans ----
    function loadPlans() {
      // In a full implementation this would call GET /api/plans
      // For now just show the current plan list from a stub
    }

    function renderPlan(plan) {
      const container = document.getElementById('plans-list');
      container.innerHTML = '';
      const card = document.createElement('div');
      card.className = 'card';
      const torrentItems = (plan.items || []).filter(i => i.method === 'torrent');
      const httpsItems   = (plan.items || []).filter(i => i.method === 'https');
      const blocked      = (plan.items || []).filter(i => i.status === 'blocked');

      let privacyHtml = '';
      if (torrentItems.length) {
        privacyHtml = `<div id="privacy-disclosure" style="display:block" role="region" aria-label="Privacy notice">
          <h3 id="privacy-title">Torrent privacy disclosure</h3>
          <p>${torrentItems.length} item(s) will be transferred via BitTorrent. Your IP address will be visible to trackers and peers.</p>
          <p><strong>DHT:</strong> off &nbsp; <strong>PEX:</strong> off &nbsp; <strong>LSD:</strong> off</p>
          <p><strong>Upload cap:</strong> unlimited &nbsp; <strong>Seeding after completion:</strong> off</p>
        </div>`;
      }

      card.innerHTML = `${privacyHtml}
        <h2>Plan <code>${escHtml(plan.plan_id)}</code> &mdash; ${badgeHtml(plan.state)}</h2>
        <p>
          ${torrentItems.length} torrent item(s),
          ${httpsItems.length} HTTPS fallback item(s),
          ${blocked.length} blocked item(s)
        </p>
        <table aria-label="Plan items">
          <thead><tr><th>Path</th><th>Method</th><th>Status</th><th>Size</th><th>Reason</th><th>Action</th></tr></thead>
          <tbody>
            ${(plan.items || []).map(item => `
              <tr>
                <td>${escHtml(item.destination_relpath)}</td>
                <td>${escHtml(item.method)}</td>
                <td>${badgeHtml(item.status)}</td>
                <td>${fmt(item.size_bytes)}</td>
                <td>${item.fallback_reason ? escHtml(item.fallback_reason) : '—'}</td>
                <td>
                  ${item.status === 'blocked' ? `<button class="btn" data-action="approve-fallback" data-id="${escHtml(item.item_id)}">Use HTTPS</button>` : ''}
                  ${item.status === 'pending' ? `<button class="btn" data-action="pause" data-id="${escHtml(item.item_id)}">Pause</button>` : ''}
                  ${item.status === 'paused'  ? `<button class="btn" data-action="resume" data-id="${escHtml(item.item_id)}">Resume</button>` : ''}
                </td>
              </tr>`).join('')}
          </tbody>
        </table>
        <div style="margin-top:1rem; display:flex; gap:.5rem;">
          <button class="btn btn-primary" data-action="start-plan" data-id="${escHtml(plan.plan_id)}">Start transfer</button>
        </div>`;

      card.querySelectorAll('[data-action]').forEach(btn => {
        btn.addEventListener('click', () => handlePlanAction(btn.dataset.action, btn.dataset.id));
      });
      container.appendChild(card);
    }

    function handlePlanAction(action, id) {
      if (action === 'start-plan') {
        api('POST', '/plans/' + id + '/start', {}).then(({ ok, data }) => {
          announce(ok ? 'Transfer started' : ('Error: ' + (data.error || 'unknown')));
          if (ok) loadTransfers();
        });
      } else if (action === 'pause') {
        api('POST', '/items/' + id + '/pause', {}).then(() => announce('Paused'));
      } else if (action === 'resume') {
        api('POST', '/items/' + id + '/resume', {}).then(() => announce('Resumed'));
      } else if (action === 'approve-fallback') {
        api('POST', '/items/' + id + '/approve-http-fallback', {}).then(({ ok }) => {
          announce(ok ? 'HTTPS fallback approved' : 'Could not approve');
        });
      }
    }

    // ---- Transfers ----
    function loadTransfers() {}

    function updateProgress(ev) {
      const el = document.getElementById('progress-' + ev.item_id);
      if (!el) return;
      const total = ev.total_bytes || 1;
      el.value = ev.downloaded_bytes / total;
      el.setAttribute('aria-valuenow', Math.round((ev.downloaded_bytes / total) * 100));
      const label = document.getElementById('progress-label-' + ev.item_id);
      if (label) {
        label.textContent = fmt(ev.downloaded_bytes) + ' / ' + fmt(ev.total_bytes);
      }
    }

    // ---- Receipts ----
    function loadReceipts() {}

    // Expose for testability
    window.connectSSE = connectSSE;

  })();
