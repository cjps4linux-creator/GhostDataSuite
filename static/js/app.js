/*
 * Ghost-Data Suite — Frontend Application
 * Features: SocketIO real-time updates, pagination, search, bulk actions.
 */

const API = window.location.origin + '/';
let token = window.GHOST_AUTH_TOKEN || '';
let socket = null;
let currentPage = 1;
let perPage = 20;
let currentSearch = '';
let currentStatus = '';
let selectedIds = new Set();

const authHeaders = () => token ? { 'X-Ghost-Token': token } : {};

async function fetchAuthToken() {
  try {
    const res = await fetch('/static/auth-config.js', { cache: 'no-cache' });
    if (res.ok) {
      const text = await res.text();
      const match = text.match(/window\.GHOST_AUTH_TOKEN\s*=\s*['"]([^'"]+)['"]/);
      if (match && match[1]) {
        token = match[1];
        window.GHOST_AUTH_TOKEN = token;
      }
    }
  } catch (e) {
    console.warn('Could not fetch auth token:', e);
  }
}

function $(id) { return document.getElementById(id); }

function toast(msg, type = 'ok') {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast show';
  setTimeout(() => el.className = 'toast', type === 'err' ? 4000 : 2200);
}

function setButton(btn, loading) {
  if (!btn) return;
  btn.disabled = loading;
  btn.dataset.loading = loading ? '1' : '0';
}

function statusPill(status) {
  const s = (status || '').toLowerCase();
  if (s === 'ok') return '<span class="pill ok">OK</span>';
  if (s === 'error') return '<span class="pill err">Error</span>';
  return `<span class="pill">${status ? status.toUpperCase() : ' - '}</span>`;
}

function initSocketIO() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = protocol + '//' + window.location.host;
  try {
    socket = io(wsUrl, { transports: ['websocket', 'polling'] });
    socket.on('connect', () => {
      console.log('SocketIO connected');
      if (token) {
        socket.emit('auth', { token });
      }
    });
    socket.on('disconnect', () => {
      console.log('SocketIO disconnected');
    });
    socket.on('capture_created', (data) => {
      console.log('New capture via SocketIO:', data);
      loadCaptures();
      toast('New capture received', 'ok');
    });
    socket.on('capture_error', (data) => {
      console.log('Capture error via SocketIO:', data);
      toast('Scrape error: ' + (data.url || 'unknown'), 'err');
    });
    socket.on('log_entry', (data) => {
      appendLog(data.line || data);
    });
  } catch (e) {
    console.warn('SocketIO unavailable, falling back to polling:', e);
  }
}

async function authedGet(url) {
  const res = await fetch(url, { headers: authHeaders() });
  if (res.status === 401) {
    // Re-fetch the token from the server (it may have been auto-generated)
    await fetchAuthToken();
    if (token) {
      return authedGet(url);
    }
    token = prompt('Enter auth token:') || '';
    if (token) {
      await fetch(url, { headers: { 'X-Ghost-Token': token } });
      return authedGet(url);
    }
    throw new Error('Authentication required');
  }
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function renderCaptures(captures, meta) {
  const tbody = $('capturesBody');
  if (!captures || !captures.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">No captures yet.</td></tr>';
    selectedIds.clear();
    updateBulkActions();
    return;
  }

  const api = API.replace(/\/$/, '');
  tbody.innerHTML = captures.map(c => {
    const date = c.created_at ? new Date(c.created_at).toLocaleString() : ' - ';
    const shortUrl = (c.url || c.url_hash || '').toString();
    const displayUrl = shortUrl.length > 90 ? shortUrl.slice(0, 90) + '...' : shortUrl;
    const capId = c.id;
    const isSelected = selectedIds.has(capId);
    return `<tr>
      <td><input type="checkbox" class="row-select" value="${capId}" ${isSelected ? 'checked' : ''} /></td>
      <td class="mono">${capId}</td>
      <td>${date}</td>
      <td title="${shortUrl}"><a href="${shortUrl}" target="_blank">${displayUrl || ' - '}</a></td>
      <td>${statusPill(c.status)}</td>
      <td class="mono">${c.url_hash ? c.url_hash.slice(0, 16) + '...' : '-'}</td>
      <td>
        <div class="actions">
          <a class="button-link ghost" href="${api}/export/json/${capId}" target="_blank" title="JSON">JSON</a>
          <a class="button-link ghost" href="${api}/export/csv/${capId}" target="_blank" title="CSV">CSV</a>
          <a class="button-link ghost" href="${api}/download/all/${capId}" target="_blank" title="Download">DL</a>
        </div>
      </td>
    </tr>`;
  }).join('');

  // Wire up row checkboxes
  tbody.querySelectorAll('.row-select').forEach(cb => {
    cb.addEventListener('change', (e) => {
      const id = parseInt(e.target.value, 10);
      if (e.target.checked) selectedIds.add(id);
      else selectedIds.delete(id);
      updateBulkActions();
    });
  });

  updateBulkActions();
  renderPagination(meta);
}

function renderPagination(meta) {
  const container = $('paginationControls');
  const total = meta ? meta.total : 0;
  const page = meta ? meta.page : currentPage;
  const per = meta ? meta.per_page : perPage;
  const totalPages = Math.ceil(total / per) || 1;

  if (totalPages <= 1) {
    container.innerHTML = '';
    return;
  }

  const pages = [];
  for (let i = 1; i <= totalPages; i++) {
    if (i === 1 || i === totalPages || (i >= page - 2 && i <= page + 2)) {
      pages.push(i);
    } else if (i === page - 3 || i === page + 3) {
      pages.push('...');
    }
  }

  let html = `<span class="muted">Page ${page} of ${totalPages} (${total} total)</span> `;
  html += `<button class="ghost" onclick="goPage(1)" ${page === 1 ? 'disabled' : ''}>First</button> `;
  html += `<button class="ghost" onclick="goPage(${page - 1})" ${page === 1 ? 'disabled' : ''}>Prev</button> `;
  pages.forEach(p => {
    if (p === '...') {
      html += `<span class="muted">…</span> `;
    } else {
      html += `<button class="ghost ${p === page ? 'primary' : ''}" onclick="goPage(${p})">${p}</button> `;
    }
  });
  html += `<button class="ghost" onclick="goPage(${page + 1})" ${page === totalPages ? 'disabled' : ''}>Next</button> `;
  html += `<button class="ghost" onclick="goPage(${totalPages})" ${page === totalPages ? 'disabled' : ''}>Last</button>`;
  container.innerHTML = html;
}

window.goPage = function (page) {
  currentPage = page;
  loadCaptures();
};

function updateBulkActions() {
  const bulkActions = $('bulkActions');
  const selectAll = $('selectAll');
  const count = selectedIds.size;

  if (count === 0) {
    bulkActions.style.display = 'none';
    if (selectAll) selectAll.checked = false;
    return;
  }

  bulkActions.style.display = 'flex';
  if (selectAll) {
    // Check if all visible rows are selected
    const visibleBoxes = $('capturesBody').querySelectorAll('.row-select');
    selectAll.checked = visibleBoxes.length > 0 && visibleBoxes.length === [...visibleBoxes].filter(cb => cb.checked).length;
  }

  $('bulkDeleteBtn').textContent = `Delete ${count}`;
}

function toggleSelectAll() {
  const cb = $('selectAll');
  const boxes = $('capturesBody').querySelectorAll('.row-select');
  boxes.forEach(b => {
    b.checked = cb.checked;
    if (cb.checked) selectedIds.add(parseInt(b.value, 10));
    else selectedIds.delete(parseInt(b.value, 10));
  });
  updateBulkActions();
}

async function loadCaptures() {
  try {
    const params = new URLSearchParams({
      page: currentPage,
      per_page: perPage,
    });
    if (currentSearch) params.set('q', currentSearch);
    if (currentStatus) params.set('status', currentStatus);

    const data = await authedGet(API + 'captures?' + params.toString());
    renderCaptures(data.captures || data, data.meta || { total: data.length, page: currentPage, per_page: perPage });
  } catch (e) {
    toast('Failed to load captures: ' + e.message, 'err');
  }
}

async function triggerScrape() {
  const url = $('url').value.trim();
  const btn = $('scrapeBtn');
  if (!url) { toast('Enter a URL', 'err'); return; }
  setButton(btn, true);
  try {
    const res = await fetch(API + 'scrape', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ url, once: true }),
    });
    if (res.status === 401) {
      await fetchAuthToken();
      return triggerScrape(); // retry with new token
    }
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Scrape failed');
    toast('Scrape started');
    $('url').value = '';
    // SocketIO will push the result; also poll as fallback
    setTimeout(loadCaptures, 3000);
  } catch (e) {
    toast('Scrape error: ' + e.message, 'err');
  } finally {
    setButton(btn, false);
  }
}

async function checkHealth() {
  try {
    const res = await fetch(API + 'health');
    const data = await res.json();
    const dot = $('healthDot');
    const txt = $('healthText');
    if (res.ok && data.status === 'ok') {
      dot.className = 'dot ok';
      txt.textContent = 'Ready';
    } else {
      dot.className = 'dot err';
      txt.textContent = 'Unhealthy';
    }
  } catch {
    $('healthDot').className = 'dot err';
    $('healthText').textContent = 'Offline';
  }
}

async function loadLogs() {
  try {
    let res = await fetch(API + 'logs', { headers: authHeaders() });
    if (res.status === 401) {
      await fetchAuthToken();
      res = await fetch(API + 'logs', { headers: authHeaders() });
    }
    if (!res.ok) throw new Error('Logs unavailable');
    const data = await res.json();
    const text = (data.logs || []).slice(-200).map(l => l.line || l).join('\n');
    $('logs').textContent = text || 'No logs yet.';
    $('logs').scrollTop = $('logs').scrollHeight;
  } catch (e) {
    $('logs').textContent = 'Logs unavailable.';
  }
}

function appendLog(line) {
  const el = $('logs');
  const current = el.textContent || '';
  el.textContent = current ? current + '\n' + line : line;
  el.scrollTop = el.scrollHeight;
}

async function clearLogs() {
  try {
    let res = await fetch(API + 'logs/clear', { method: 'POST', headers: { ...authHeaders(), 'Content-Type': 'application/json' } });
    if (res.status === 401) {
      await fetchAuthToken();
      res = await fetch(API + 'logs/clear', { method: 'POST', headers: { ...authHeaders(), 'Content-Type': 'application/json' } });
    }
    if (!res.ok) throw new Error('Clear failed');
    $('logs').textContent = 'Logs cleared.';
    toast('Logs cleared');
  } catch { toast('Failed to clear logs', 'err'); }
}

async function exportCaptures(fmt) {
  try {
    const res = await fetch(API + 'export/' + fmt, { headers: authHeaders() });
    if (!res.ok) throw new Error('Export failed');
    const blob = await res.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'ghost-captures.' + (fmt === 'json' ? 'json' : 'csv');
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);
    toast('Export downloaded');
  } catch (e) {
    toast('Export error: ' + e.message, 'err');
  }
}

async function clearDatabase() {
  if (!confirm('Delete all captures and media? This cannot be undone.')) return;
  try {
    const res = await fetch(API + 'db/clear', { method: 'POST', headers: { ...authHeaders(), 'Content-Type': 'application/json' } });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Clear failed');
    toast('Database cleared');
    selectedIds.clear();
    loadCaptures();
  } catch (e) {
    toast('Clear error: ' + e.message, 'err');
  }
}

async function bulkDelete() {
  if (selectedIds.size === 0) return;
  if (!confirm(`Delete ${selectedIds.size} capture(s)? This cannot be undone.`)) return;
  try {
    const res = await fetch(API + 'captures/bulk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ ids: Array.from(selectedIds) }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Bulk delete failed');
    toast(`Deleted ${selectedIds.size} capture(s)`);
    selectedIds.clear();
    loadCaptures();
  } catch (e) {
    toast('Bulk delete error: ' + e.message, 'err');
  }
}

// Wire up event listeners
document.addEventListener('DOMContentLoaded', () => {
  $('scrapeBtn').onclick = triggerScrape;
  $('url').addEventListener('keydown', e => { if (e.key === 'Enter') triggerScrape(); });
  $('stopBtn').onclick = async () => {
    try {
      setButton($('stopBtn'), true);
      await fetch(API + 'scrape/stop', { method: 'POST', headers: { ...authHeaders(), 'Content-Type': 'application/json' } });
      toast('Stop requested');
    } catch { toast('Stop failed', 'err'); } finally { setButton($('stopBtn'), false); }
  };
  $('exportJsonBtn').onclick = () => exportCaptures('json');
  $('exportCsvBtn').onclick = () => exportCaptures('csv');
  $('clearDbBtn').onclick = clearDatabase;
  $('bulkDeleteBtn').onclick = bulkDelete;
  $('selectAll').addEventListener('change', toggleSelectAll);
  $('refreshLogsBtn').onclick = () => { loadLogs(); };
  $('clearLogsBtn').onclick = clearLogs;

  // Search
  $('searchInput').addEventListener('input', (e) => {
    currentSearch = e.target.value.trim();
    currentPage = 1;
    loadCaptures();
  });
  $('statusFilter').addEventListener('change', (e) => {
    currentStatus = e.target.value;
    currentPage = 1;
    loadCaptures();
  });

  // Init — check health immediately (no auth needed), then load token and data
  checkHealth();
  (async () => {
    await fetchAuthToken();
    loadCaptures();
    loadLogs();
    initSocketIO();
  })();

  // Fallback polling (SocketIO may not be available in all deployments)
  setInterval(loadCaptures, 15000);
  setInterval(loadLogs, 20000);
  setInterval(checkHealth, 30000);
});
