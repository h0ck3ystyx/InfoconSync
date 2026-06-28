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
      return fetch('/api' + path, opts).then(r => {
        const ct = r.headers.get('content-type') || '';
        const isJson = ct.includes('application/json');
        return (isJson ? r.json() : r.text().then(t => ({ error: t.slice(0, 120) })))
          .then(d => ({ ok: r.ok, status: r.status, data: d }));
      });
    }

    function announce(msg) {
      const el = document.getElementById('live-status');
      el.textContent = msg;
      el.style.display = 'block';
      setTimeout(() => { el.style.display = 'none'; }, 4000);
    }

    function badgeClass(status) {
      const map = {
        'new': 'badge-new',
        'changed_marker': 'badge-changed',
        'changed_manifest': 'badge-changed',
        'verified_current': 'badge-verified',
        'manifest_verified': 'badge-verified',
        'present_unverified': 'badge-unverified',
        'local_only': 'badge-local-only',
        'unknown': 'badge-unknown',
        'pending': 'badge-pending',
        'blocked': 'badge-blocked',
        'complete': 'badge-complete',
        'failed': 'badge-failed',
        'draft': 'badge-pending',
        'preflighted': 'badge-pending',
        'running': 'badge-changed',
        'paused': 'badge-unknown',
      };
      return map[status] || 'badge-unknown';
    }

    function badgeLabel(status) {
      const map = {
        'new': 'New',
        'changed_marker': 'Changed',
        'changed_manifest': 'Changed',
        'verified_current': 'Verified',
        'manifest_verified': 'Verified',
        'present_unverified': 'Unverified',
        'local_only': 'Local only',
        'unknown': 'Unknown',
        'pending': 'Pending',
        'blocked': 'Blocked',
        'complete': 'Complete',
        'failed': 'Failed',
        'draft': 'Draft',
        'preflighted': 'Preflighted',
        'running': 'Running',
        'paused': 'Paused',
      };
      return map[status] || status;
    }

    const _badgeTip = {
      'new':               'No local copy exists — not yet downloaded',
      'changed_marker':    'A newer torrent marker exists upstream — needs re-download',
      'changed_manifest':  'File path or size differs from the torrent manifest',
      'verified_current':  'Piece-checked against the current torrent — fully verified',
      'manifest_verified': 'All files present with correct sizes per the torrent manifest',
      'present_unverified':'Files are present locally but have not been verified against a manifest',
      'local_only':        'No upstream counterpart found — will never be auto-deleted',
      'unknown':           'Not enough upstream evidence to classify cheaply',
      'transfer_incomplete':'A resumable download job exists but is not complete',
      'downloaded_unverified':'HTTPS transfer finished but no cryptographic verification has run',
      'pending':           'Queued and waiting to start',
      'blocked':           'Torrent swarm unreachable — awaiting your approval to use HTTPS instead',
      'complete':          'Transfer finished successfully',
      'failed':            'Transfer encountered an unrecoverable error',
      'draft':             'Plan created, not yet started',
      'preflighted':       'Plan validated and ready to start',
      'running':           'Transfer in progress',
      'paused':            'Transfer paused',
    };

    function badgeHtml(status) {
      const tip = _badgeTip[status] ? ` title="${escHtml(_badgeTip[status])}"` : '';
      return `<span class="badge ${badgeClass(status)}"${tip}>${badgeLabel(status)}</span>`;
    }

    function fmt(n) {
      if (n == null) return '—';
      if (n >= 1e9) return (n / 1e9).toFixed(1) + ' GB';
      if (n >= 1e6) return (n / 1e6).toFixed(1) + ' MB';
      if (n >= 1e3) return (n / 1e3).toFixed(1) + ' KB';
      return n + ' B';
    }

    function escHtml(s) {
      return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }

    function fmtDate(iso) {
      if (!iso) return '—';
      try { return new Date(iso).toLocaleString(); } catch (_) { return iso; }
    }

    // ---- Tab switching ----
    const tabs = document.querySelectorAll('[role="tab"]');
    const panels = document.querySelectorAll('.tab-panel');

    function switchTab(tab) {
      tabs.forEach(t => t.setAttribute('aria-selected', 'false'));
      panels.forEach(p => p.classList.remove('active'));
      tab.setAttribute('aria-selected', 'true');
      document.getElementById(tab.getAttribute('aria-controls')).classList.add('active');
    }

    tabs.forEach(tab => {
      tab.addEventListener('click', () => {
        switchTab(tab);
        // Lazy-load tab content on first visit
        const id = tab.id;
        if (id === 'btn-plans') loadPlans();
        if (id === 'btn-transfers') loadTransfers();
        if (id === 'btn-receipts') loadReceipts();
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
        try { handleSSEEvent(JSON.parse(e.data)); } catch (_) {}
      };
      evtSource.onerror = () => {
        evtSource.close();
        evtSource = null;
        setTimeout(() => { connectSSE(); }, 3000);
      };
    }

    function handleSSEEvent(ev) {
      if (ev.type === 'check_complete') {
        stopCheckPoll();
        loadCheck(ev.check_id);
      } else if (ev.type === 'check_failed') {
        stopCheckPoll();
        const btn = document.getElementById('btn-check-now');
        btn.disabled = false;
        btn.textContent = 'Check upstream';
        announce('Check failed: ' + (ev.error || 'unknown error'));
      } else if (ev.type === 'plan_created') {
        // Refresh plans list if tab is active
        if (document.getElementById('btn-plans').getAttribute('aria-selected') === 'true') {
          loadPlans();
        }
      } else if (ev.type === 'plan_started') {
        announce('Transfer started');
        if (document.getElementById('btn-transfers').getAttribute('aria-selected') === 'true') {
          loadTransfers();
        }
      } else if (ev.type === 'verify_complete') {
        onVerifyFinished(ev.collection_key, ev.level, ev.error, ev.details || {});
      } else if (ev.type === 'verify_failed') {
        onVerifyFinished(ev.collection_key, 'unverified', ev.error, {});
      } else if (ev.type === 'progress') {
        updateProgress(ev);
      }
    }

    connectSSE();

    // ---- Check upstream ----
    let _activeCheckId = null;
    let _checkPollTimer = null;

    function stopCheckPoll() {
      if (_checkPollTimer) { clearInterval(_checkPollTimer); _checkPollTimer = null; }
    }

    function startCheckPoll(checkId) {
      stopCheckPoll();
      _checkPollTimer = setInterval(() => {
        api('GET', '/checks/' + checkId).then(({ ok, data }) => {
          if (!ok) return;
          if (data.state === 'complete' || data.state === 'failed') {
            stopCheckPoll();
            onCheckFinished(data);
          }
        });
      }, 4000);
    }

    function onCheckFinished(data) {
      const btn = document.getElementById('btn-check-now');
      btn.disabled = false;
      btn.textContent = 'Check upstream';
      if (data.state === 'complete' && data.results) {
        announce('Check complete — ' + data.count + ' collections found');
        renderCollections(data.results);
        switchTab(document.getElementById('btn-collections'));
      } else {
        announce('Check failed: ' + (data.error || 'unknown'));
      }
    }

    function loadCheck(checkId) {
      api('GET', '/checks/' + checkId).then(({ ok, data }) => {
        if (ok) onCheckFinished(data);
      });
    }

    document.getElementById('btn-check-now').addEventListener('click', () => {
      const btn = document.getElementById('btn-check-now');
      btn.disabled = true;
      btn.textContent = 'Checking…';
      announce('Fetching upstream listings…');
      api('POST', '/checks', {}).then(({ ok, data }) => {
        if (ok) {
          _activeCheckId = data.check_id;
          announce('Check running — this may take a minute');
          startCheckPoll(data.check_id);
        } else {
          btn.disabled = false;
          btn.textContent = 'Check upstream';
          announce('Check failed: ' + (data.error || 'unknown'));
        }
      });
    });

    // ---- Collections — grouped by section ----
    let _collections = [];

    function groupBySection(list) {
      const map = {};
      for (const item of list) {
        const sec = item.section || 'other';
        if (!map[sec]) map[sec] = [];
        map[sec].push(item);
      }
      // Sort each section's items case-insensitively by display name
      for (const sec of Object.keys(map)) {
        map[sec].sort((a, b) =>
          (a.display_name || a.key).toLowerCase().localeCompare(
            (b.display_name || b.key).toLowerCase()
          )
        );
      }
      return map;
    }

    function renderCollections(list) {
      _collections = list;
      const container = document.getElementById('collections-container');
      const empty = document.getElementById('collections-empty');

      if (!list.length) {
        empty.hidden = false;
        const old = document.getElementById('collections-list');
        if (old) old.remove();
        updatePlanButton();
        return;
      }

      empty.hidden = true;

      const old = document.getElementById('collections-list');
      if (old) old.remove();

      const listEl = document.createElement('div');
      listEl.id = 'collections-list';

      const groups = groupBySection(list);

      for (const section of Object.keys(groups).sort()) {
        const items = groups[section];

        const counts = {};
        for (const item of items) {
          counts[item.status] = (counts[item.status] || 0) + 1;
        }
        const summaryBadges = Object.entries(counts)
          .sort((a, b) => b[1] - a[1])
          .map(([s, n]) => {
            const tip = _badgeTip[s] ? ` title="${escHtml(_badgeTip[s])}"` : '';
            return `<span class="badge ${badgeClass(s)}"${tip}>${n} ${badgeLabel(s)}</span>`;
          })
          .join(' ');

        const details = document.createElement('details');
        details.className = 'section-group';
        details.open = false;
        details.dataset.section = section;

        const summary = document.createElement('summary');
        summary.className = 'section-summary';

        const secCb = document.createElement('input');
        secCb.type = 'checkbox';
        secCb.className = 'section-select';
        secCb.dataset.section = section;
        secCb.setAttribute('aria-label', 'Select all ' + section);
        secCb.addEventListener('click', e => e.stopPropagation());
        secCb.addEventListener('change', () => {
          details.querySelectorAll('.row-select').forEach(r => { r.checked = secCb.checked; });
          secCb.indeterminate = false;
          updatePlanButton();
        });

        summary.appendChild(secCb);
        summary.insertAdjacentHTML('beforeend',
          `<span class="section-name">${escHtml(section)}</span>` +
          `<span class="section-count">${items.length} collections</span>` +
          `<span class="section-badges">${summaryBadges}</span>`
        );

        const table = document.createElement('table');
        table.className = 'section-table';
        table.setAttribute('aria-label', section + ' collections');
        table.innerHTML =
          '<thead><tr>' +
          '<th style="width:2rem"></th>' +
          '<th>Collection</th>' +
          '<th>Status</th>' +
          '<th></th>' +
          '<th>Evidence</th>' +
          '</tr></thead>';

        const tbody = document.createElement('tbody');

        for (const item of items) {
          const tr = document.createElement('tr');
          const rowKey = section + '/' + item.key;
          tr.dataset.key = rowKey;
          tr.dataset.status = item.status;
          tr.dataset.name = (item.display_name || item.key).toLowerCase();

          const canVerify = item.status === 'present_unverified' || item.status === 'changed_marker';
          const verifyBtn = canVerify
            ? `<button class="btn btn-sm btn-verify" data-collection-id="${escHtml(rowKey)}" ` +
              `title="Check local files exist with correct sizes per the InfoCon torrent">Verify</button>`
            : '';

          const evidenceJson = escHtml(JSON.stringify(item.evidence || [], null, 2));
          tr.innerHTML =
            `<td><input type="checkbox" class="row-select" data-key="${escHtml(rowKey)}"` +
            ` aria-label="Select ${escHtml(item.display_name || item.key)}"></td>` +
            `<td>${escHtml(item.display_name || item.key)}</td>` +
            `<td class="status-cell">${badgeHtml(item.status)}</td>` +
            `<td class="action-cell">${verifyBtn}</td>` +
            `<td><details><summary>Evidence</summary>` +
            `<div class="evidence-payload">${evidenceJson}</div></details></td>`;

          if (canVerify) {
            tr.querySelector('.btn-verify').addEventListener('click', () => {
              startVerify(rowKey, tr);
            });
          }

          tbody.appendChild(tr);
        }

        tbody.addEventListener('change', () => {
          const rowCbs = [...tbody.querySelectorAll('.row-select')];
          const checkedCount = rowCbs.filter(r => r.checked).length;
          secCb.checked = checkedCount === rowCbs.length;
          secCb.indeterminate = checkedCount > 0 && checkedCount < rowCbs.length;
          updatePlanButton();
        });

        table.appendChild(tbody);
        details.appendChild(summary);
        details.appendChild(table);
        listEl.appendChild(details);
      }

      container.appendChild(listEl);
      updatePlanButton();
    }

    function updatePlanButton() {
      const any = !!document.querySelector('#collections-list .row-select:checked');
      document.getElementById('btn-plan-selected').disabled = !any;
    }

    // ---- Verify ----
    // Track in-progress verifications: collectionKey → tr element
    const _verifyingRows = new Map();

    function startVerify(collectionId, tr) {
      const btn = tr.querySelector('.btn-verify');
      if (btn) { btn.disabled = true; btn.textContent = 'Verifying…'; }
      _verifyingRows.set(collectionId, tr);
      announce('Verification started for ' + collectionId.split('/').pop());

      api('POST', '/verify', { collection_id: collectionId }).then(({ ok, data }) => {
        if (!ok) {
          if (btn) { btn.disabled = false; btn.textContent = 'Verify'; }
          _verifyingRows.delete(collectionId);
          announce('Verify failed: ' + (data.detail || data.error || 'unknown'));
        }
        // Success: SSE verify_complete will call onVerifyFinished
      });
    }

    function onVerifyFinished(collectionKey, level, error, details) {
      const tr = _verifyingRows.get(collectionKey);
      _verifyingRows.delete(collectionKey);

      const verified = level === 'piece_verified' || level === 'manifest_verified';
      const newStatus = verified ? 'verified_current' : 'changed_manifest';
      announce(verified
        ? 'Verified: ' + collectionKey.split('/').pop()
        : 'Verify found issues in ' + collectionKey.split('/').pop());

      if (!tr) return;

      // Remove any previous result row for this collection
      const prevResult = tr.nextElementSibling;
      if (prevResult && prevResult.classList.contains('verify-result-row')) {
        prevResult.remove();
      }

      tr.dataset.status = newStatus;
      const statusCell = tr.querySelector('.status-cell');
      if (statusCell) statusCell.innerHTML = badgeHtml(newStatus);

      const actionCell = tr.querySelector('.action-cell');
      if (actionCell) {
        if (verified) {
          actionCell.innerHTML = '';
        } else {
          const btn = actionCell.querySelector('.btn-verify');
          if (btn) { btn.disabled = false; btn.textContent = 'Retry verify'; }
        }
      }

      // Inject result row below the collection row
      const resultTr = document.createElement('tr');
      resultTr.className = 'verify-result-row';

      if (verified) {
        const n = details.total_files || '?';
        resultTr.innerHTML =
          `<td></td><td colspan="4"><div class="verify-result verify-result-pass">` +
          `✓ All ${escHtml(String(n))} files present with correct sizes` +
          `</div></td>`;
        tr.after(resultTr);
        // Auto-dismiss after 6s
        setTimeout(() => resultTr.remove(), 6000);
      } else {
        const missing = details.missing || [];
        const wrongSize = details.wrong_size || [];
        const missingTotal = details.missing_total || missing.length;
        const wrongTotal = details.wrong_size_total || wrongSize.length;

        let summary = [];
        if (missingTotal) summary.push(`${missingTotal} missing`);
        if (wrongTotal) summary.push(`${wrongTotal} wrong size`);

        let fileList = '';
        if (missing.length) {
          fileList += '<p class="verify-detail-label">Missing files:</p><ul class="verify-file-list">' +
            missing.map(p => `<li>${escHtml(p)}</li>`).join('') +
            (missingTotal > missing.length ? `<li class="verify-more">… and ${missingTotal - missing.length} more</li>` : '') +
            '</ul>';
        }
        if (wrongSize.length) {
          fileList += '<p class="verify-detail-label">Size mismatches:</p><ul class="verify-file-list">' +
            wrongSize.map(w => `<li>${escHtml(w.path)} <span class="verify-size-info">(expected ${w.expected}, got ${w.actual})</span></li>`).join('') +
            (wrongTotal > wrongSize.length ? `<li class="verify-more">… and ${wrongTotal - wrongSize.length} more</li>` : '') +
            '</ul>';
        }

        resultTr.innerHTML =
          `<td></td><td colspan="4"><div class="verify-result verify-result-fail">` +
          `<strong>Verify found issues:</strong> ${escHtml(summary.join(', '))}` +
          (fileList ? `<details class="verify-detail"><summary>Show details</summary>${fileList}</details>` : '') +
          `</div></td>`;
        tr.after(resultTr);
      }
    }

    // ---- Filter / search ----
    document.getElementById('search-collections').addEventListener('input', applyFilter);
    document.getElementById('filter-status').addEventListener('change', applyFilter);

    function applyFilter() {
      const q = document.getElementById('search-collections').value.toLowerCase();
      const s = document.getElementById('filter-status').value;

      document.querySelectorAll('#collections-list .section-group').forEach(details => {
        let anyVisible = false;
        details.querySelectorAll('tbody tr').forEach(tr => {
          const matchText = !q || tr.dataset.name.includes(q);
          const matchStatus = !s || tr.dataset.status === s;
          const visible = matchText && matchStatus;
          tr.style.display = visible ? '' : 'none';
          if (visible) anyVisible = true;
        });
        details.style.display = anyVisible ? '' : 'none';
      });
    }

    // ---- Plan selected ----
    document.getElementById('btn-plan-selected').addEventListener('click', () => {
      const selected = [...document.querySelectorAll('#collections-list .row-select:checked')]
        .map(cb => cb.dataset.key);
      if (!selected.length) return;

      const btn = document.getElementById('btn-plan-selected');
      btn.disabled = true;
      btn.textContent = 'Planning…';

      api('POST', '/plans', { collection_ids: selected }).then(({ ok, data }) => {
        btn.disabled = false;
        btn.textContent = 'Plan selected';
        if (ok) {
          announce('Plan created — ' + (data.items || []).length + ' items');
          showPlanDetail(data);
          switchTab(document.getElementById('btn-plans'));
        } else {
          announce('Failed to create plan: ' + (data.error || 'unknown'));
        }
      });
    });

    // ---- Plans ----
    let _plansLoaded = false;

    function loadPlans() {
      api('GET', '/plans').then(({ ok, data }) => {
        if (!ok) return;
        _plansLoaded = true;
        renderPlansList(data);
      });
    }

    function renderPlansList(plans) {
      const container = document.getElementById('plans-list');
      if (!plans.length) {
        container.innerHTML = '<p>No plans yet. Select collections and click <strong>Plan selected</strong>.</p>';
        return;
      }
      container.innerHTML = '';
      for (const plan of plans) {
        const card = document.createElement('div');
        card.className = 'card plan-summary-card';
        card.dataset.planId = plan.plan_id;
        card.innerHTML =
          `<div class="plan-summary-row">` +
          `<code class="plan-id">${escHtml(plan.plan_id.slice(0, 8))}…</code>` +
          `${badgeHtml(plan.state)}` +
          `<span class="plan-meta">${plan.item_count} item${plan.item_count !== 1 ? 's' : ''}</span>` +
          `<span class="plan-meta">${fmtDate(plan.created_at)}</span>` +
          `<button class="btn btn-sm" data-plan-id="${escHtml(plan.plan_id)}">View</button>` +
          `</div>`;
        card.querySelector('button').addEventListener('click', () => {
          api('GET', '/plans/' + plan.plan_id).then(({ ok, data }) => {
            if (ok) showPlanDetail(data);
          });
        });
        container.appendChild(card);
      }
    }

    function showPlanDetail(plan) {
      const container = document.getElementById('plans-list');

      // Remove any existing detail view for this plan
      const existing = container.querySelector(`.plan-detail[data-plan-id="${plan.plan_id}"]`);
      if (existing) existing.remove();

      const detail = document.createElement('div');
      detail.className = 'card plan-detail';
      detail.dataset.planId = plan.plan_id;

      const torrentItems = (plan.items || []).filter(i => i.method === 'torrent');
      const httpsItems   = (plan.items || []).filter(i => i.method === 'https');
      const blockedItems = (plan.items || []).filter(i => i.status === 'blocked');

      let privacyHtml = '';
      if (torrentItems.length) {
        privacyHtml =
          `<div class="privacy-notice">` +
          `<strong>Torrent privacy disclosure:</strong> ${torrentItems.length} item(s) will be transferred via BitTorrent. ` +
          `Your IP address will be visible to trackers and peers. DHT, PEX, and LSD are off.` +
          `</div>`;
      }

      const rows = (plan.items || []).map(item =>
        `<tr>` +
        `<td>${escHtml(item.destination_relpath)}</td>` +
        `<td><span class="method-badge method-${escHtml(item.method)}">${escHtml(item.method.toUpperCase())}</span></td>` +
        `<td>${badgeHtml(item.status)}</td>` +
        `<td>${fmt(item.size_bytes)}</td>` +
        `<td>${item.fallback_reason ? escHtml(item.fallback_reason) : '—'}</td>` +
        `<td class="item-actions">` +
        (item.status === 'blocked'
          ? `<button class="btn btn-sm" data-action="approve-fallback" data-id="${escHtml(item.item_id)}">Use HTTPS</button>`
          : '') +
        (item.status === 'pending'
          ? `<button class="btn btn-sm" data-action="pause" data-id="${escHtml(item.item_id)}">Pause</button>`
          : '') +
        (item.status === 'paused'
          ? `<button class="btn btn-sm" data-action="resume" data-id="${escHtml(item.item_id)}">Resume</button>`
          : '') +
        `</td>` +
        `</tr>`
      ).join('');

      const canStart = plan.state === 'draft' || plan.state === 'preflighted';
      detail.innerHTML =
        `${privacyHtml}` +
        `<div class="plan-detail-header">` +
        `<h2>Plan <code>${escHtml(plan.plan_id.slice(0, 8))}…</code> ${badgeHtml(plan.state)}</h2>` +
        `<div class="plan-detail-meta">` +
        `${torrentItems.length} torrent · ${httpsItems.length} HTTPS · ${blockedItems.length} blocked` +
        `</div>` +
        `</div>` +
        `<table class="plan-items-table" aria-label="Plan items">` +
        `<thead><tr><th>Path</th><th>Method</th><th>Status</th><th>Size</th><th>Reason</th><th></th></tr></thead>` +
        `<tbody>${rows}</tbody>` +
        `</table>` +
        `<div class="plan-actions">` +
        `<button class="btn btn-primary" data-action="start-plan" data-id="${escHtml(plan.plan_id)}"` +
        (canStart ? '' : ' disabled') + `>Start transfer</button>` +
        `<button class="btn" data-action="close-detail">Close</button>` +
        `</div>`;

      detail.querySelectorAll('[data-action]').forEach(btn => {
        btn.addEventListener('click', () => handlePlanAction(btn.dataset.action, btn.dataset.id, detail));
      });

      container.appendChild(detail);
      detail.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    function handlePlanAction(action, id, detailEl) {
      if (action === 'start-plan') {
        api('POST', '/plans/' + id + '/start', {}).then(({ ok, data }) => {
          if (ok) {
            announce('Transfer started (torrent engine not yet active — status only)');
            loadPlans();
            loadTransfers();
          } else {
            announce('Error: ' + (data.error || 'unknown'));
          }
        });
      } else if (action === 'pause') {
        api('POST', '/items/' + id + '/pause', {}).then(() => announce('Paused'));
      } else if (action === 'resume') {
        api('POST', '/items/' + id + '/resume', {}).then(() => announce('Resumed'));
      } else if (action === 'approve-fallback') {
        api('POST', '/items/' + id + '/approve-http-fallback', {}).then(({ ok }) => {
          announce(ok ? 'HTTPS fallback approved' : 'Could not approve');
        });
      } else if (action === 'close-detail') {
        if (detailEl) detailEl.remove();
      }
    }

    // ---- Transfers ----
    function loadTransfers() {
      api('GET', '/plans').then(({ ok, data }) => {
        if (!ok) return;
        const running = (data || []).filter(p => p.state === 'running');
        renderTransfers(running);
      });
    }

    function renderTransfers(plans) {
      const container = document.getElementById('transfers-list');
      if (!plans.length) {
        container.innerHTML = '<p>No active transfers. Start a plan from the Plans tab.</p>';
        return;
      }
      container.innerHTML = '';
      for (const plan of plans) {
        api('GET', '/plans/' + plan.plan_id).then(({ ok, data }) => {
          if (!ok) return;
          const card = document.createElement('div');
          card.className = 'card';
          const rows = (data.items || []).map(item =>
            `<tr>` +
            `<td>${escHtml(item.destination_relpath)}</td>` +
            `<td>${escHtml(item.method)}</td>` +
            `<td>${badgeHtml(item.status)}</td>` +
            `<td>${fmt(item.size_bytes)}</td>` +
            `</tr>`
          ).join('');
          card.innerHTML =
            `<h2>Plan <code>${escHtml(plan.plan_id.slice(0, 8))}…</code> ${badgeHtml(plan.state)}</h2>` +
            `<p class="transfer-note">Note: file transfer requires the torrent engine to be active.</p>` +
            `<table><thead><tr><th>Path</th><th>Method</th><th>Status</th><th>Size</th></tr></thead>` +
            `<tbody>${rows}</tbody></table>`;
          container.appendChild(card);
        });
      }
    }

    // ---- Receipts ----
    function loadReceipts() {
      api('GET', '/receipts').then(({ ok, data }) => {
        if (!ok) return;
        renderReceiptsList(data || []);
      });
    }

    function renderReceiptsList(receipts) {
      const container = document.getElementById('receipts-list');
      if (!receipts.length) {
        container.innerHTML = '<p>No receipts yet. Receipts are generated when a transfer completes.</p>';
        return;
      }
      container.innerHTML = '';
      for (const r of receipts) {
        const card = document.createElement('div');
        card.className = 'card receipt-card';
        card.innerHTML =
          `<div class="receipt-row">` +
          `<code>${escHtml(r.receipt_id.slice(0, 8))}…</code>` +
          `<span class="plan-meta">Plan: <code>${escHtml(r.plan_id.slice(0, 8))}…</code></span>` +
          `<span class="plan-meta">${fmtDate(r.completed_at)}</span>` +
          `<button class="btn btn-sm" data-receipt-id="${escHtml(r.receipt_id)}">View JSON</button>` +
          `</div>` +
          `<div class="receipt-json-container" id="receipt-json-${escHtml(r.receipt_id)}" hidden></div>`;
        card.querySelector('button').addEventListener('click', () => {
          const jsonEl = card.querySelector('.receipt-json-container');
          if (!jsonEl.hidden) { jsonEl.hidden = true; return; }
          api('GET', '/receipts/' + r.receipt_id).then(({ ok, data }) => {
            if (ok) {
              jsonEl.innerHTML = `<pre class="receipt-json">${escHtml(JSON.stringify(data, null, 2))}</pre>`;
              jsonEl.hidden = false;
            }
          });
        });
        container.appendChild(card);
      }
    }

    // ---- Progress ----
    function updateProgress(ev) {
      const el = document.getElementById('progress-' + ev.item_id);
      if (!el) return;
      const total = ev.total_bytes || 1;
      el.value = ev.downloaded_bytes / total;
      const label = document.getElementById('progress-label-' + ev.item_id);
      if (label) label.textContent = fmt(ev.downloaded_bytes) + ' / ' + fmt(ev.total_bytes);
    }

    window.connectSSE = connectSSE;

  })();
