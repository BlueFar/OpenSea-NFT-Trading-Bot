// ==========================================================================
// OpenSea NFT Monitor Dashboard — Client Application
// ==========================================================================

const API = {
  status: '/api/status',
  startBot: '/api/bot/start',
  stopBot: '/api/bot/stop',
  collections: '/api/collections',
  candidates: '/api/candidates',
  dossier: '/api/candidate/dossier',
  inspect: '/api/inspect',
  logs: '/api/logs',
};

// State
let pollInterval = null;
let currentTab = 'candidatesTab';

// DOM Elements
const el = {
  statusDot: document.getElementById('statusDot'),
  statusText: document.getElementById('statusText'),
  statusMeta: document.getElementById('statusMeta'),
  btnStartBot: document.getElementById('btnStartBot'),
  btnStopBot: document.getElementById('btnStopBot'),
  dryRunToggle: document.getElementById('dryRunToggle'),

  cardStatus: document.getElementById('cardStatus'),
  cardUptime: document.getElementById('cardUptime'),
  cardCandidates: document.getElementById('cardCandidates'),
  cardMonitored: document.getElementById('cardMonitored'),
  cardCursor: document.getElementById('cardCursor'),
  cardCheckpointTime: document.getElementById('cardCheckpointTime'),

  tabCandidatesCount: document.getElementById('tabCandidatesCount'),
  tabUniverseCount: document.getElementById('tabUniverseCount'),
  candidatesTableBody: document.getElementById('candidatesTableBody'),
  universeTableBody: document.getElementById('universeTableBody'),
  universeSearchInput: document.getElementById('universeSearchInput'),
  btnRefreshUniverse: document.getElementById('btnRefreshUniverse'),

  inspectForm: document.getElementById('inspectForm'),
  inspectInput: document.getElementById('inspectInput'),
  btnInspect: document.getElementById('btnInspect'),
  inspectResults: document.getElementById('inspectResults'),
  resSlug: document.getElementById('resSlug'),
  resOverallBadge: document.getElementById('resOverallBadge'),
  resRejectionAlert: document.getElementById('resRejectionAlert'),
  criteriaGrid: document.getElementById('criteriaGrid'),

  logsTerminal: document.getElementById('logsTerminal'),
  btnRefreshLogs: document.getElementById('btnRefreshLogs'),
  autoScrollLogs: document.getElementById('autoScrollLogs'),

  dossierModal: document.getElementById('dossierModal'),
  modalProjectTitle: document.getElementById('modalProjectTitle'),
  modalDateSubtitle: document.getElementById('modalDateSubtitle'),
  modalMarkdownBody: document.getElementById('modalMarkdownBody'),
  btnModalClose: document.getElementById('btnModalClose'),
};

// ==========================================================================
// INITIALIZATION
// ==========================================================================
document.addEventListener('DOMContentLoaded', () => {
  setupEventListeners();
  fetchStatus();
  fetchCandidates();
  fetchUniverse();
  fetchLogs();

  // Polling every 3 seconds
  pollInterval = setInterval(() => {
    fetchStatus();
    if (currentTab === 'logsTab') {
      fetchLogs();
    }
  }, 3000);
});

function setupEventListeners() {
  // Start / Stop Bot
  el.btnStartBot.addEventListener('click', handleStartBot);
  el.btnStopBot.addEventListener('click', handleStopBot);

  // Tabs
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      btn.classList.add('active');
      const targetId = btn.dataset.tab;
      document.getElementById(targetId).classList.add('active');
      currentTab = targetId;

      if (currentTab === 'universeTab') fetchUniverse();
      if (currentTab === 'candidatesTab') fetchCandidates();
      if (currentTab === 'logsTab') fetchLogs();
    });
  });

  // Inspect Form
  el.inspectForm.addEventListener('submit', handleInspectSubmit);

  // Universe search & refresh
  el.btnRefreshUniverse.addEventListener('click', () => fetchUniverse(el.universeSearchInput.value));
  let searchDebounce = null;
  el.universeSearchInput.addEventListener('input', (e) => {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => fetchUniverse(e.target.value), 300);
  });

  // Logs refresh
  el.btnRefreshLogs.addEventListener('click', fetchLogs);

  // Dossier Modal
  el.btnModalClose.addEventListener('click', () => el.dossierModal.close());
  el.dossierModal.addEventListener('click', (e) => {
    if (e.target === el.dossierModal) el.dossierModal.close();
  });
}

// ==========================================================================
// STATUS & CONTROLS
// ==========================================================================
async function fetchStatus() {
  try {
    const res = await fetch(API.status);
    if (!res.ok) return;
    const data = await res.json();

    const isRunning = data.is_running;

    // Status Capsule
    el.statusDot.className = 'status-dot ' + (isRunning ? 'running' : 'stopped');
    el.statusText.textContent = data.status;
    el.statusMeta.textContent = isRunning ? `PID ${data.pid}` : '';

    // Buttons
    el.btnStartBot.disabled = isRunning;
    el.btnStopBot.disabled = !isRunning;

    // Metric Cards
    el.cardStatus.textContent = data.status;
    el.cardStatus.style.color = isRunning ? 'var(--color-primary)' : 'var(--color-danger)';
    el.cardUptime.textContent = isRunning && data.uptime_seconds != null
      ? `Uptime: ${formatUptime(data.uptime_seconds)}`
      : 'Bot is offline';

    el.cardCandidates.textContent = data.total_candidates;
    el.cardMonitored.textContent = data.total_monitored;

    // Checkpoint
    const cp = data.checkpoints?.collections;
    if (cp && cp.cursor) {
      el.cardCursor.textContent = cp.cursor.length > 18 ? cp.cursor.substring(0, 18) + '...' : cp.cursor;
      el.cardCheckpointTime.textContent = 'Updated ' + formatDate(cp.updated_at);
    } else {
      el.cardCursor.textContent = 'START';
      el.cardCheckpointTime.textContent = 'Initial crawl pending';
    }

    el.tabCandidatesCount.textContent = data.total_candidates;
    el.tabUniverseCount.textContent = data.total_monitored;
  } catch (err) {
    console.error('Failed to fetch status:', err);
  }
}

async function handleStartBot() {
  el.btnStartBot.disabled = true;
  el.btnStartBot.querySelector('span').textContent = 'Starting...';

  try {
    const dryRun = el.dryRunToggle.checked;
    const res = await fetch(API.startBot, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dry_run: dryRun }),
    });
    const result = await res.json();
    if (!result.success) {
      alert('Error starting bot: ' + (result.error || 'Unknown error'));
    }
  } catch (err) {
    alert('Failed to send start command: ' + err.message);
  } finally {
    el.btnStartBot.querySelector('span').textContent = 'Start Bot';
    fetchStatus();
    fetchLogs();
  }
}

async function handleStopBot() {
  el.btnStopBot.disabled = true;
  el.btnStopBot.querySelector('span').textContent = 'Stopping...';

  try {
    const res = await fetch(API.stopBot, { method: 'POST' });
    const result = await res.json();
    if (!result.success) {
      alert('Error stopping bot: ' + (result.error || 'Unknown error'));
    }
  } catch (err) {
    alert('Failed to send stop command: ' + err.message);
  } finally {
    el.btnStopBot.querySelector('span').textContent = 'Stop Bot';
    fetchStatus();
    fetchLogs();
  }
}

// ==========================================================================
// 7-CONDITION INSPECTOR
// ==========================================================================
async function handleInspectSubmit(e) {
  e.preventDefault();
  const slug = el.inspectInput.value.trim();
  if (!slug) return;

  const btnText = el.btnInspect.querySelector('.btn-text');
  const btnSpinner = el.btnInspect.querySelector('.btn-spinner');
  btnText.textContent = 'Inspecting...';
  btnSpinner.classList.remove('hidden');
  el.btnInspect.disabled = true;

  try {
    const res = await fetch(`${API.inspect}?slug=${encodeURIComponent(slug)}`);
    const data = await res.json();

    el.inspectResults.classList.remove('hidden');
    el.resSlug.textContent = data.slug;

    if (!data.evaluated) {
      el.resOverallBadge.className = 'summary-status-badge fail';
      el.resOverallBadge.textContent = 'ERROR';
      el.resRejectionAlert.textContent = data.error || 'Inspection failed';
      el.resRejectionAlert.classList.remove('hidden');
      el.criteriaGrid.innerHTML = '';
      return;
    }

    const isPass = data.is_overall_pass;
    el.resOverallBadge.className = `summary-status-badge ${isPass ? 'pass' : 'fail'}`;
    el.resOverallBadge.textContent = isPass ? 'ALL 7 FILTERS PASSED' : 'REJECTED (FAIL)';

    if (data.rejection_reasons && data.rejection_reasons.length > 0) {
      el.resRejectionAlert.innerHTML = '<strong>Rejection Reasons:</strong><ul>' +
        data.rejection_reasons.map(r => `<li>${escapeHtml(r)}</li>`).join('') + '</ul>';
      el.resRejectionAlert.classList.remove('hidden');
    } else {
      el.resRejectionAlert.classList.add('hidden');
    }

    // Render 7 Criteria Cards
    renderCriteriaGrid(data.criteria);
  } catch (err) {
    alert('Inspection error: ' + err.message);
  } finally {
    btnText.textContent = 'Inspect 7 Conditions';
    btnSpinner.classList.add('hidden');
    el.btnInspect.disabled = false;
  }
}

function renderCriteriaGrid(criteria) {
  if (!criteria) return;

  const labels = {
    project_age: '1. OpenSea Collection Age',
    verification: '2. Verification Status',
    listed_items: '3. Listed Items %',
    trading_frequency: '4. Trading Frequency (7d)',
    floor_change_1d: '5. Floor Price Change (1D)',
    floor_change_7d: '6. Floor Price Change (7D)',
    offer_to_floor: '7. Offer to Floor (Advisory)',
  };

  el.criteriaGrid.innerHTML = Object.entries(criteria).map(([key, item]) => {
    const title = labels[key] || key.replace('_', ' ').toUpperCase();
    const resultLower = (item.result || 'UNKNOWN').toLowerCase();
    const badgeClass = resultLower === 'pass' ? 'pass' : (resultLower === 'observe' ? 'observe' : 'fail');

    return `
      <div class="criterion-card ${badgeClass}">
        <div class="criterion-header">
          <span class="criterion-title">${escapeHtml(title)}</span>
          <span class="crit-badge ${badgeClass}">${item.result}</span>
        </div>
        <div class="crit-value-row">
          <div class="crit-actual">${escapeHtml(item.actual_value)}${item.unit || ''}</div>
          <div class="crit-threshold">Req: ${escapeHtml(item.threshold)}</div>
        </div>
        <div class="crit-formula" title="${escapeHtml(item.formula)}">${escapeHtml(item.formula)}</div>
        ${item.notes ? `<div class="crit-notes">${escapeHtml(item.notes)}</div>` : ''}
      </div>
    `;
  }).join('');
}

// ==========================================================================
// QUALIFIED CANDIDATES & DOSSIER MODAL
// ==========================================================================
async function fetchCandidates() {
  try {
    const res = await fetch(API.candidates);
    if (!res.ok) return;
    const data = await res.json();

    const candidates = data.candidates || [];
    el.tabCandidatesCount.textContent = candidates.length;

    if (candidates.length === 0) {
      el.candidatesTableBody.innerHTML = `
        <tr>
          <td colspan="6" class="text-center py-6 text-muted">
            No candidate dossiers found yet. They will appear here automatically when collections pass all 7 filters.
          </td>
        </tr>
      `;
      return;
    }

    el.candidatesTableBody.innerHTML = candidates.map(c => `
      <tr>
        <td><strong class="text-mono">${c.date}</strong></td>
        <td><strong>${escapeHtml(c.project_name)}</strong></td>
        <td><span class="text-mono text-sm">${c.date}/${c.folder_name}/Info.md</span></td>
        <td class="text-muted">${(c.file_size / 1024).toFixed(1)} KB</td>
        <td class="text-muted">${formatDate(new Date(c.modified_at * 1000).toISOString())}</td>
        <td>
          <button class="btn btn-accent btn-sm" onclick="openDossierModal('${c.date}', '${c.folder_name}')">
            View Info.md
          </button>
        </td>
      </tr>
    `).join('');
  } catch (err) {
    console.error('Error fetching candidates:', err);
  }
}

window.openDossierModal = async function(date, project) {
  el.modalProjectTitle.textContent = project.replace('-', ' ');
  el.modalDateSubtitle.textContent = `Candidate Dossier: ${date}/${project}/Info.md`;
  el.modalMarkdownBody.innerHTML = '<div class="text-muted">Loading dossier content...</div>';
  el.dossierModal.showModal();

  try {
    const res = await fetch(`${API.dossier}?date=${encodeURIComponent(date)}&project=${encodeURIComponent(project)}`);
    const data = await res.json();
    if (data.error) {
      el.modalMarkdownBody.innerHTML = `<div class="rejection-alert">${escapeHtml(data.error)}</div>`;
      return;
    }
    el.modalMarkdownBody.innerHTML = renderSimpleMarkdown(data.content);
  } catch (err) {
    el.modalMarkdownBody.innerHTML = `<div class="rejection-alert">Failed to load dossier: ${err.message}</div>`;
  }
};

// ==========================================================================
// MONITORED UNIVERSE
// ==========================================================================
async function fetchUniverse(searchQuery = '') {
  try {
    const url = searchQuery ? `${API.collections}?q=${encodeURIComponent(searchQuery)}` : API.collections;
    const res = await fetch(url);
    if (!res.ok) return;
    const data = await res.json();
    const list = data.collections || [];

    if (list.length === 0) {
      el.universeTableBody.innerHTML = `
        <tr>
          <td colspan="5" class="text-center py-6 text-muted">
            No collections found${searchQuery ? ` matching "${escapeHtml(searchQuery)}"` : ''}.
          </td>
        </tr>
      `;
      return;
    }

    el.universeTableBody.innerHTML = list.map(item => `
      <tr>
        <td><strong>${escapeHtml(item.slug)}</strong></td>
        <td><span class="text-mono text-sm">${escapeHtml(item.discovery_source)}</span></td>
        <td>${item.evaluation_count}</td>
        <td class="text-muted">${item.last_evaluated_at ? formatDate(item.last_evaluated_at) : '<span class="text-muted">Pending</span>'}</td>
        <td>
          <button class="btn btn-secondary btn-sm" onclick="inspectSlugDirect('${item.slug}')">
            Inspect
          </button>
        </td>
      </tr>
    `).join('');
  } catch (err) {
    console.error('Error fetching universe:', err);
  }
}

window.inspectSlugDirect = function(slug) {
  el.inspectInput.value = slug;
  el.inspectForm.dispatchEvent(new Event('submit'));
  window.scrollTo({ top: el.inspectForm.offsetTop - 40, behavior: 'smooth' });
};

// ==========================================================================
// ACTIVITY LOGS
// ==========================================================================
async function fetchLogs() {
  try {
    const res = await fetch(API.logs);
    if (!res.ok) return;
    const data = await res.json();
    el.logsTerminal.textContent = (data.lines || []).join('\n');
    if (el.autoScrollLogs.checked) {
      el.logsTerminal.scrollTop = el.logsTerminal.scrollHeight;
    }
  } catch (err) {
    console.error('Error fetching logs:', err);
  }
}

// ==========================================================================
// HELPERS & MARKDOWN RENDERER
// ==========================================================================
function formatUptime(seconds) {
  if (seconds == null) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return `${h}h ${m}m ${s}s`;
}

function formatDate(isoStr) {
  if (!isoStr) return '—';
  try {
    const d = new Date(isoStr);
    return d.toLocaleString();
  } catch (e) {
    return isoStr;
  }
}

function escapeHtml(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function renderSimpleMarkdown(md) {
  if (!md) return '';
  let html = '';
  const lines = md.split('\n');
  let inTable = false;
  let inList = false;

  for (let line of lines) {
    // Headers
    if (line.startsWith('# ')) {
      if (inTable) { html += '</table>'; inTable = false; }
      if (inList) { html += '</ul>'; inList = false; }
      html += `<h1>${escapeHtml(line.substring(2))}</h1>`;
      continue;
    }
    if (line.startsWith('## ')) {
      if (inTable) { html += '</table>'; inTable = false; }
      if (inList) { html += '</ul>'; inList = false; }
      html += `<h2>${escapeHtml(line.substring(3))}</h2>`;
      continue;
    }
    if (line.startsWith('### ')) {
      if (inTable) { html += '</table>'; inTable = false; }
      if (inList) { html += '</ul>'; inList = false; }
      html += `<h3>${escapeHtml(line.substring(4))}</h3>`;
      continue;
    }
    // Blockquote
    if (line.startsWith('> ')) {
      if (inTable) { html += '</table>'; inTable = false; }
      if (inList) { html += '</ul>'; inList = false; }
      html += `<blockquote>${escapeHtml(line.substring(2))}</blockquote>`;
      continue;
    }
    // Table rows
    if (line.trim().startsWith('|') && line.trim().endsWith('|')) {
      if (line.includes('---')) continue; // skip divider
      const cells = line.split('|').slice(1, -1).map(c => c.trim());
      if (!inTable) {
        html += '<table><thead><tr>' + cells.map(c => `<th>${escapeHtml(c)}</th>`).join('') + '</tr></thead><tbody>';
        inTable = true;
      } else {
        html += '<tr>' + cells.map(c => `<td>${escapeHtml(c)}</td>`).join('') + '</tr>';
      }
      continue;
    } else if (inTable) {
      html += '</tbody></table>';
      inTable = false;
    }

    // Unordered lists
    if (line.trim().startsWith('- ')) {
      if (!inList) {
        html += '<ul>';
        inList = true;
      }
      let content = line.trim().substring(2);
      // bold
      content = content.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
      content = content.replace(/`(.*?)`/g, '<code>$1</code>');
      html += `<li>${content}</li>`;
      continue;
    } else if (inList) {
      html += '</ul>';
      inList = false;
    }

    // Regular paragraphs
    if (line.trim().length > 0) {
      let content = escapeHtml(line);
      content = content.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
      content = content.replace(/`(.*?)`/g, '<code>$1</code>');
      html += `<p>${content}</p>`;
    }
  }

  if (inTable) html += '</tbody></table>';
  if (inList) html += '</ul>';
  return html;
}
