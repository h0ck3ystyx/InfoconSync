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
        'has_older_version': 'badge-legacy',
        'present_unverified': 'badge-unverified',
        'local_only': 'badge-local-only',
        'unknown': 'badge-unknown',
        'pending': 'badge-pending',
        'blocked': 'badge-blocked',
        'complete': 'badge-complete',
        'failed': 'badge-failed',
        'draft': 'badge-pending',
        'preflighted': 'badge-pending',
        'downloading': 'badge-changed',
        'running': 'badge-changed',
        'paused': 'badge-unknown',
      };
      return map[status] || 'badge-unknown';
    }

    function badgeLabel(status) {
      const map = {
        'new': 'New',
        'changed_marker': 'Needs update',
        'changed_manifest': 'Needs update',
        'verified_current': 'Verified',
        'manifest_verified': 'Verified',
        'has_older_version': 'Legacy',
        'present_unverified': 'Unverified',
        'local_only': 'Local only',
        'unknown': 'Unknown',
        'pending': 'Pending',
        'downloading': 'Downloading',
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
      'has_older_version': 'Files are present but are larger than the current torrent expects — likely pre-re-encoding originals (v1 content). Content is probably still valid.',
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

    // --- Verify result helpers -----------------------------------------------

    function verifyResultSummary(vr) {
      if (!vr || !vr.level) return '';
      const {level, error, total_files, missing_dirs_total, missing_total, size_larger_total, size_smaller_total} = vr;
      if (level === 'manifest_verified' || level === 'piece_verified') {
        return `✓ Verified${total_files ? ` (${total_files} files)` : ''}`;
      }
      if (level === 'has_older_version') {
        return `⚠ Legacy (${size_larger_total || '?'} files older)`;
      }
      if (level === 'no_torrent') {
        return '— No torrent';
      }
      if (level === 'unverified') {
        const parts = [];
        if (missing_dirs_total) parts.push(`${missing_dirs_total} new folder${missing_dirs_total !== 1 ? 's' : ''}`);
        if (missing_total) parts.push(`${missing_total} missing`);
        if (size_smaller_total) parts.push(`${size_smaller_total} truncated`);
        if (size_larger_total) parts.push(`${size_larger_total} larger`);
        return `✗ Issues: ${parts.join(', ') || error || 'unknown'}`;
      }
      return '';
    }

    // Set a details cell to show a collapsible summary that expands into a
    // full-width row below the parent <tr>.
    function attachExpandingDetails(detailsEl, tr, summaryText, bodyText, extraClass) {
      detailsEl.querySelector('summary').textContent = summaryText;
      detailsEl.addEventListener('toggle', () => {
        const existing = tr.nextElementSibling;
        if (existing && existing.classList.contains('details-expansion-row')) {
          existing.remove();
        }
        if (detailsEl.open) {
          const cols = tr.cells.length;
          const expTr = document.createElement('tr');
          expTr.className = 'details-expansion-row';
          const cls = 'evidence-payload expansion-body' + (extraClass ? ' ' + extraClass : '');
          expTr.innerHTML =
            `<td colspan="${cols}"><div class="${cls}">${escHtml(bodyText)}</div></td>`;
          tr.after(expTr);
        }
      });
    }

    // Build the expansion body text for an issues-type verify result.
    function buildIssuesBody(vr) {
      const missingDirs = vr.missing_dirs || [];
      const missing = vr.missing || [];
      const smaller = vr.size_smaller || [];
      const larger  = vr.size_larger  || [];
      const missingDirsTotal = vr.missing_dirs_total || missingDirs.length;
      const missingTotal = vr.missing_total || missing.length;
      const smallerTotal = vr.size_smaller_total || smaller.length;
      const largerTotal  = vr.size_larger_total  || larger.length;
      let body = '';
      if (missingDirs.length) {
        body += `New folders:\n` + missingDirs.map(d => `  ${d}`).join('\n') +
          (missingDirsTotal > missingDirs.length ? `\n  … and ${missingDirsTotal - missingDirs.length} more` : '') + '\n\n';
      }
      if (missing.length) {
        body += `Missing files:\n` + missing.map(p => `  ${p}`).join('\n') +
          (missingTotal > missing.length ? `\n  … and ${missingTotal - missing.length} more` : '') + '\n\n';
      }
      if (smaller.length) {
        body += `Truncated / wrong-size:\n` +
          smaller.map(w => `  ${w.path}\n    expected ${w.expected.toLocaleString()} B, got ${w.actual.toLocaleString()} B`).join('\n') +
          (smallerTotal > smaller.length ? `\n  … and ${smallerTotal - smaller.length} more` : '') + '\n\n';
      }
      if (larger.length) {
        body += `Larger than expected:\n` +
          larger.map(w => `  ${w.path}\n    expected ${w.expected.toLocaleString()} B, got ${w.actual.toLocaleString()} B`).join('\n') +
          (largerTotal > larger.length ? `\n  … and ${largerTotal - larger.length} more` : '');
      }
      return body.trim();
    }

    // Populate a .details-cell from a verify_result object.
    function renderVerifyResultCell(cell, tr, vr) {
      const summary = verifyResultSummary(vr);
      if (!summary) return;
      cell.innerHTML = `<details class="details-expandable"><summary></summary></details>`;
      const detailsEl = cell.querySelector('details');
      if (vr.level === 'unverified') {
        const body = buildIssuesBody(vr) || vr.error || summary;
        attachExpandingDetails(detailsEl, tr, summary, body, 'expansion-body-issues');
      } else if (vr.level === 'has_older_version') {
        const larger = vr.size_larger || [];
        const largerTotal = vr.size_larger_total || larger.length;
        const total = vr.total_files || '?';
        let body = `All ${total} files present. ${largerTotal} are larger than the current torrent expects — original pre-re-encoding files.\n`;
        if (larger.length) {
          body += `\nLarger-than-expected files:\n` +
            larger.map(w => `  ${w.path}\n    torrent: ${w.expected.toLocaleString()} B  local: ${w.actual.toLocaleString()} B`).join('\n');
          if (largerTotal > larger.length) body += `\n  … and ${largerTotal - larger.length} more`;
        }
        attachExpandingDetails(detailsEl, tr, summary, body);
      } else {
        attachExpandingDetails(detailsEl, tr, summary, vr.error || summary);
      }
    }

    // -------------------------------------------------------------------------

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

    // Load the last completed check on startup so collections are visible immediately
    api('GET', '/checks/latest').then(({ ok, data }) => {
      if (ok && data.results && data.results.length) {
        renderCollections(data.results);
      }
    });

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
        hideCheckProgress();
        loadCheck(ev.check_id);
      } else if (ev.type === 'check_failed') {
        stopCheckPoll();
        hideCheckProgress();
        const btn = document.getElementById('btn-check-now');
        btn.disabled = false;
        btn.textContent = 'Check infocon.org';
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
      } else if (ev.type === 'check_progress') {
        updateCheckProgress(ev);
      } else if (ev.type === 'verify_complete') {
        onVerifyFinished(ev.collection_key, ev.level, ev.error, ev.details || {});
      } else if (ev.type === 'verify_failed') {
        onVerifyFinished(ev.collection_key, 'unverified', ev.error, {});
      } else if (ev.type === 'item_status') {
        onItemStatusChanged(ev);
      } else if (ev.type === 'plan_status') {
        onPlanStatusChanged(ev);
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

    function updateCheckProgress(ev) {
      const bar = document.getElementById('check-progress-bar');
      const label = document.getElementById('check-progress-label');
      const prog = document.getElementById('check-progress-el');
      if (!bar) return;
      bar.hidden = false;
      if (ev.phase === 'fetch') {
        if (label) label.textContent = `Fetching ${ev.section}… (${ev.current}/${ev.total})`;
        if (prog) prog.removeAttribute('value');
      } else if (ev.phase === 'verify') {
        if (label) label.textContent = `Checking manifests… (${ev.current}/${ev.total})`;
        if (prog && ev.total > 0) prog.value = ev.current / ev.total;
      }
    }

    function hideCheckProgress() {
      const bar = document.getElementById('check-progress-bar');
      if (bar) bar.hidden = true;
    }

    function onCheckFinished(data) {
      const btn = document.getElementById('btn-check-now');
      btn.disabled = false;
      btn.textContent = 'Check infocon.org';
      hideCheckProgress();
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
          btn.textContent = 'Check infocon.org';
          hideCheckProgress();
          announce('Check failed: ' + (data.error || 'unknown'));
        }
      });
    });

    // ---- Clear cache ----
    let _resetPending = false;
    let _resetTimer = null;

    document.getElementById('btn-reset-db').addEventListener('click', () => {
      const btn = document.getElementById('btn-reset-db');
      if (!_resetPending) {
        _resetPending = true;
        btn.textContent = 'Confirm clear?';
        btn.classList.add('btn-reset-confirm');
        _resetTimer = setTimeout(() => {
          _resetPending = false;
          btn.textContent = 'Clear cache';
          btn.classList.remove('btn-reset-confirm');
        }, 4000);
      } else {
        clearTimeout(_resetTimer);
        _resetPending = false;
        btn.textContent = 'Clearing…';
        btn.disabled = true;
        api('POST', '/admin/reset').then(({ ok }) => {
          btn.disabled = false;
          btn.textContent = 'Clear cache';
          btn.classList.remove('btn-reset-confirm');
          if (ok) {
            renderCollections([]);
            announce('Cache cleared — click Check infocon.org to refresh');
          } else {
            announce('Clear failed');
          }
        });
      }
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
      // Sort each section's items: new (entire directory missing) first, then alphabetical
      for (const sec of Object.keys(map)) {
        map[sec].sort((a, b) => {
          const aNew = a.status === 'new' ? 0 : 1;
          const bNew = b.status === 'new' ? 0 : 1;
          if (aNew !== bNew) return aNew - bNew;
          return (a.display_name || a.key).toLowerCase().localeCompare(
            (b.display_name || b.key).toLowerCase()
          );
        });
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
          '<th class="col-cb"></th>' +
          '<th>Collection</th>' +
          '<th>Status</th>' +
          '<th></th>' +
          '<th>Details</th>' +
          '<th>Torrent</th>' +
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
            `<td class="details-cell"></td>` +
            `<td><details><summary>Torrent</summary>` +
            `<div class="evidence-payload">${evidenceJson}</div></details></td>`;

          if (item.verify_result) {
            renderVerifyResultCell(tr.querySelector('.details-cell'), tr, item.verify_result);
          }

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
      if (btn) { btn.disabled = true; btn.textContent = 'Verifying…'; btn.classList.add('verifying'); }
      _verifyingRows.set(collectionId, tr);
      announce('Verification started for ' + collectionId.split('/').pop());

      api('POST', '/verify', { collection_id: collectionId }).then(({ ok, data }) => {
        if (!ok) {
          if (btn) { btn.disabled = false; btn.textContent = 'Verify'; btn.classList.remove('verifying'); }
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
      const legacy   = level === 'has_older_version';
      const noTorrent = level === 'no_torrent';
      const newStatus = verified ? 'verified_current' : legacy ? 'has_older_version' : noTorrent ? tr.dataset.status : 'changed_manifest';

      announce(verified   ? 'Verified: ' + collectionKey.split('/').pop()
             : legacy     ? 'Legacy version: ' + collectionKey.split('/').pop()
             : noTorrent  ? 'No torrent found for ' + collectionKey.split('/').pop()
             :               'Verify found issues in ' + collectionKey.split('/').pop());

      if (!tr) return;

      if (!noTorrent) {
        tr.dataset.status = newStatus;
        const statusCell = tr.querySelector('.status-cell');
        if (statusCell) statusCell.innerHTML = badgeHtml(newStatus);
      }

      const actionCell = tr.querySelector('.action-cell');
      if (actionCell) {
        if (verified) {
          actionCell.innerHTML = '';
        } else {
          const btn = actionCell.querySelector('.btn-verify');
          if (btn) { btn.disabled = false; btn.textContent = 'Retry verify'; btn.classList.remove('verifying'); }
        }
      }

      // Update the Details cell with a collapsible summary that expands full-width
      const detailsCell = tr.querySelector('.details-cell');
      if (!detailsCell) return;
      detailsCell.innerHTML = `<details class="details-expandable"><summary></summary></details>`;
      const detailsEl = detailsCell.querySelector('details');

      if (verified) {
        const n = details.total_files || '?';
        attachExpandingDetails(detailsEl, tr,
          `✓ Verified (${n} files)`,
          `All ${n} files present with correct sizes.`);

      } else if (legacy) {
        const larger = details.size_larger || [];
        const largerTotal = details.size_larger_total || larger.length;
        const total = details.total_files || '?';
        let body = `All ${total} files present. ${largerTotal} are larger than the current torrent expects — original pre-re-encoding files. Content is likely still valid.\n`;
        if (larger.length) {
          body += `\nLarger-than-expected files:\n` +
            larger.map(w => `  ${w.path}\n    torrent: ${w.expected.toLocaleString()} B  local: ${w.actual.toLocaleString()} B`).join('\n');
          if (largerTotal > larger.length) body += `\n  … and ${largerTotal - larger.length} more`;
        }
        attachExpandingDetails(detailsEl, tr,
          `⚠ Legacy (${largerTotal}/${total} larger)`, body);

      } else if (noTorrent) {
        attachExpandingDetails(detailsEl, tr,
          '— No torrent',
          'No torrent file found for this collection. Cannot verify.');

      } else {
        const missingDirsTotal = details.missing_dirs_total || (details.missing_dirs || []).length;
        const missingTotal = details.missing_total || (details.missing || []).length;
        const smallerTotal = details.size_smaller_total || (details.size_smaller || []).length;
        const largerTotal  = details.size_larger_total  || (details.size_larger  || []).length;
        const summaryParts = [];
        if (missingDirsTotal) summaryParts.push(`${missingDirsTotal} new folder${missingDirsTotal !== 1 ? 's' : ''}`);
        if (missingTotal) summaryParts.push(`${missingTotal} missing`);
        if (smallerTotal) summaryParts.push(`${smallerTotal} truncated`);
        if (largerTotal)  summaryParts.push(`${largerTotal} larger`);
        attachExpandingDetails(detailsEl, tr,
          `✗ Issues: ${summaryParts.join(', ')}`,
          buildIssuesBody(details), 'expansion-body-issues');
      }
    }

    // ---- Transfer SSE handlers ----

    function onItemStatusChanged(ev) {
      // Refresh any open plan detail that contains this item
      const detail = document.querySelector(`.plan-detail[data-plan-id="${CSS.escape(ev.plan_id)}"]`);
      if (detail) {
        api('GET', '/plans/' + ev.plan_id).then(({ ok, data }) => {
          if (ok) showPlanDetail(data);
        });
      }
    }

    function onPlanStatusChanged(ev) {
      const msg = ev.state === 'complete'
        ? 'Transfer complete!'
        : 'Transfer failed' + (ev.error ? ': ' + ev.error : '');
      announce(msg);
      loadPlans();
      if (document.getElementById('btn-transfers').getAttribute('aria-selected') === 'true') {
        loadTransfers();
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
        container.innerHTML = '<p>No plans yet. Select collections and click <strong>Create Plan</strong>.</p>';
        return;
      }
      container.innerHTML = '';
      for (const plan of plans) {
        const card = document.createElement('div');
        card.className = 'card plan-summary-card';
        card.dataset.planId = plan.plan_id;
        const canDelete = plan.state !== 'running';
        card.innerHTML =
          `<div class="plan-summary-row">` +
          `<code class="plan-id">${escHtml(plan.plan_id.slice(0, 8))}…</code>` +
          `${badgeHtml(plan.state)}` +
          `<span class="plan-meta">${plan.item_count} item${plan.item_count !== 1 ? 's' : ''}</span>` +
          `<span class="plan-meta">${fmtDate(plan.created_at)}</span>` +
          `<button class="btn btn-sm btn-view-plan" data-plan-id="${escHtml(plan.plan_id)}">View</button>` +
          (canDelete ? `<button class="btn btn-sm btn-delete-plan" data-plan-id="${escHtml(plan.plan_id)}">Delete</button>` : '') +
          `</div>`;
        card.querySelector('.btn-view-plan').addEventListener('click', () => {
          api('GET', '/plans/' + plan.plan_id).then(({ ok, data }) => {
            if (ok) showPlanDetail(data);
          });
        });
        if (canDelete) {
          card.querySelector('.btn-delete-plan').addEventListener('click', () => {
            api('DELETE', '/plans/' + plan.plan_id).then(({ ok }) => {
              if (ok) {
                card.remove();
                announce('Plan deleted');
                if (!document.querySelector('#plans-list .plan-summary-card')) {
                  document.getElementById('plans-list').innerHTML =
                    '<p>No plans yet. Select collections and click <strong>Create Plan</strong>.</p>';
                }
              } else {
                announce('Could not delete plan');
              }
            });
          });
        }
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
            announce('Transfer started');
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
