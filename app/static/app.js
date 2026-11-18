const form = document.querySelector('#request-form');
const input = document.querySelector('#request-text');
const conversation = document.querySelector('#conversation');
let currentRequest = {};
let latestBatchId = null;
let latestBatchCsv = '';
let pendingPolicy = null;

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const contentType = response.headers.get('content-type') || '';
  const data = contentType.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) {
    const message = typeof data === 'object' ? data.detail : data;
    throw new Error(message || `Request failed (${response.status})`);
  }
  return data;
}

function addMessage(text, kind) {
  const row = document.createElement('div');
  row.className = `message ${kind === 'user' ? 'user-message' : 'assistant-message'}`;
  if (kind === 'assistant') {
    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.textContent = 'C';
    row.append(avatar);
  }
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  const paragraph = document.createElement('p');
  paragraph.textContent = text;
  bubble.append(paragraph);
  row.append(bubble);
  conversation.append(row);
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message) return;
  addMessage(message, 'user');
  input.value = '';
  try {
    const result = await api('/api/intake', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, previous: currentRequest }),
    });
    currentRequest = result.parsed || currentRequest;
    addMessage(result.reply, 'assistant');
  } catch (error) {
    addMessage(`The server returned an error: ${error.message}`, 'assistant');
  }
});

for (const tab of document.querySelectorAll('.tab')) {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((button) => button.classList.remove('active'));
    document.querySelectorAll('.panel').forEach((panel) => panel.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById(tab.dataset.panel).classList.add('active');
  });
}

function renderTable(target, rows, columns, actionColumn = null) {
  target.replaceChildren();
  if (!rows || rows.length === 0) {
    target.textContent = 'No records yet. Process a batch first.';
    return;
  }
  const table = document.createElement('table');
  const head = document.createElement('thead');
  const headerRow = document.createElement('tr');
  for (const column of columns) {
    const th = document.createElement('th');
    th.textContent = column.label;
    headerRow.append(th);
  }
  if (actionColumn) {
    const th = document.createElement('th');
    th.textContent = 'Buyer action';
    headerRow.append(th);
  }
  head.append(headerRow);
  table.append(head);
  const body = document.createElement('tbody');
  for (const row of rows) {
    const tr = document.createElement('tr');
    for (const column of columns) {
      const td = document.createElement('td');
      td.textContent = row[column.key] ?? '';
      if (column.key === 'reason') td.className = 'reason';
      tr.append(td);
    }
    if (actionColumn) {
      const td = document.createElement('td');
      const actions = document.createElement('div');
      actions.className = 'row-actions';
      if (row.status === 'Pending') {
        const approve = document.createElement('button');
        approve.className = 'secondary';
        approve.textContent = 'Approve';
        approve.addEventListener('click', () => takeBuyerAction(row.row_id, 'approve'));
        const override = document.createElement('button');
        override.className = 'secondary';
        override.textContent = 'Override';
        override.addEventListener('click', () => takeBuyerAction(row.row_id, 'override'));
        actions.append(approve, override);
      } else {
        actions.textContent = row.status;
      }
      td.append(actions);
      tr.append(td);
    }
    body.append(tr);
  }
  table.append(body);
  target.append(table);
}

const decisionColumns = [
  { key: 'pr_no', label: 'PR' }, { key: 'pr_line', label: 'Line' },
  { key: 'resolved_item_code', label: 'Item' }, { key: 'group_id', label: 'Requirement' },
  { key: 'decision', label: 'Decision' }, { key: 'route', label: 'Route' },
  { key: 'supplier_id', label: 'Supplier' }, { key: 'unit_price', label: 'Price' },
  { key: 'group_value_inr', label: 'Value (INR)' },
  { key: 'suggested_supplier_ids', label: 'Suggested suppliers' },
  { key: 'reason', label: 'Reason' },
];

function downloadText(text, filename, type = 'text/csv;charset=utf-8') {
  const blob = new Blob([text], { type });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  link.click();
  URL.revokeObjectURL(link.href);
}

document.querySelector('#process-batch').addEventListener('click', async () => {
  const file = document.querySelector('#batch-file').files[0];
  const status = document.querySelector('#batch-status');
  if (!file) { status.textContent = 'Choose the PR batch CSV first.'; return; }
  status.textContent = 'Processing the batch…';
  try {
    const result = await api('/api/process-batch', {
      method: 'POST',
      headers: { 'Content-Type': 'text/csv', 'X-File-Name': file.name },
      body: await file.text(),
    });
    latestBatchId = result.batch_id;
    latestBatchCsv = result.csv;
    status.textContent = `Processed ${result.line_count} PR lines. Batch ${result.batch_id.slice(0, 8)} is saved in the buyer queue.`;
    renderTable(document.querySelector('#batch-results'), result.decisions, decisionColumns);
    document.querySelector('#download-batch').classList.remove('hidden');
    loadBuyerQueue();
  } catch (error) {
    status.textContent = `Batch failed: ${error.message}`;
  }
});

document.querySelector('#download-batch').addEventListener('click', () => {
  if (latestBatchCsv) downloadText(latestBatchCsv, 'decisions.csv');
});

document.querySelector('#download-preferred').addEventListener('click', async () => {
  const date = document.querySelector('#preferred-date').value;
  const status = document.querySelector('#batch-status');
  if (!date) { status.textContent = 'Choose an as-of date first.'; return; }
  try {
    const response = await fetch(`/api/preferred-suppliers.csv?as_of=${encodeURIComponent(date)}`);
    if (!response.ok) throw new Error((await response.json()).detail || 'Export failed');
    downloadText(await response.text(), `preferred_suppliers_${date}.csv`);
    status.textContent = 'Preferred supplier list downloaded.';
  } catch (error) { status.textContent = `Export failed: ${error.message}`; }
});

async function loadBuyerQueue() {
  const status = document.querySelector('#buyer-status');
  try {
    const result = await api('/api/buyer/queue');
    status.textContent = `${result.decisions.length} decision rows in the latest batch.`;
    renderTable(document.querySelector('#buyer-results'), result.decisions, [
      ...decisionColumns, { key: 'status', label: 'Status' },
      { key: 'action_reference', label: 'Created PO / sourcing ref' },
    ], true);
    const audit = await api('/api/buyer/audit');
    renderTable(document.querySelector('#audit-results'), audit.actions, [
      { key: 'entity_type', label: 'Record' }, { key: 'entity_id', label: 'ID' },
      { key: 'action', label: 'Action' }, { key: 'actor', label: 'Who' },
      { key: 'comment', label: 'Reason' }, { key: 'created_at', label: 'When' },
    ]);
  } catch (error) { status.textContent = `Could not load buyer queue: ${error.message}`; }
}

async function takeBuyerAction(rowId, action) {
  const payload = { action, actor: 'Procurement buyer' };
  if (action === 'override') {
    payload.comment = prompt('Why are you overriding this decision?') || '';
    if (!payload.comment.trim()) return;
    payload.decision = prompt('New decision (PO or SOURCING_REQUEST):', 'SOURCING_REQUEST');
    payload.route = prompt('New route (for example RFQ):', 'RFQ');
    if (!payload.decision || !payload.route) return;
  }
  try {
    await api(`/api/buyer/decisions/${rowId}/action`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    await loadBuyerQueue();
  } catch (error) { document.querySelector('#buyer-status').textContent = error.message; }
}

document.querySelector('#refresh-queue').addEventListener('click', loadBuyerQueue);

async function loadPolicy() {
  const status = document.querySelector('#policy-status');
  try {
    const policy = await api('/api/policy');
    pendingPolicy = policy;
    document.querySelector('#policy-json').value = JSON.stringify(policy, null, 2);
    document.querySelector('#activate-policy').disabled = true;
    status.textContent = `Loaded policy v${policy.version}, effective ${policy.effective_date}.`;
  } catch (error) { status.textContent = `Could not load policy: ${error.message}`; }
}
document.querySelector('#load-policy').addEventListener('click', loadPolicy);
document.querySelector('#policy-file').addEventListener('change', async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  document.querySelector('#policy-json').value = await file.text();
  document.querySelector('#policy-status').textContent = `Loaded ${file.name} into the editor. Preview before activating.`;
});

document.querySelector('#preview-policy').addEventListener('click', async () => {
  const status = document.querySelector('#policy-status');
  try {
    const candidate = JSON.parse(document.querySelector('#policy-json').value);
    const preview = await api('/api/policy/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(candidate),
    });
    pendingPolicy = preview.policy;
    const list = document.querySelector('#policy-changes');
    list.replaceChildren();
    for (const change of preview.changes) {
      const item = document.createElement('li'); item.textContent = change; list.append(item);
    }
    document.querySelector('#activate-policy').disabled = false;
    status.textContent = `Preview ready: v${preview.version}, effective ${preview.effective_date}. Review the changes before activating.`;
  } catch (error) { status.textContent = `Policy preview failed: ${error.message}`; }
});

document.querySelector('#activate-policy').addEventListener('click', async () => {
  if (!pendingPolicy) return;
  try {
    const result = await api('/api/policy/activate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ policy: pendingPolicy, actor: 'Procurement admin' }),
    });
    document.querySelector('#policy-status').textContent = `Policy v${result.version} is active from ${result.effective_date}.`;
    document.querySelector('#activate-policy').disabled = true;
  } catch (error) { document.querySelector('#policy-status').textContent = error.message; }
});

document.querySelector('#extract-policy').addEventListener('click', async () => {
  const file = document.querySelector('#policy-pdf').files[0];
  const status = document.querySelector('#policy-status');
  if (!file) { status.textContent = 'Choose a policy PDF first.'; return; }
  try {
    const response = await fetch('/api/policy/extract-pdf', {
      method: 'POST', headers: { 'Content-Type': 'application/pdf' }, body: await file.arrayBuffer(),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Could not extract the PDF');
    document.querySelector('#policy-extracted').value = result.extracted_text;
    status.textContent = 'PDF text extracted. Translate the amendment into policy JSON, preview it, then activate it.';
  } catch (error) { status.textContent = error.message; }
});

document.querySelector('#import-kit').addEventListener('click', async () => {
  const file = document.querySelector('#kit-file').files[0];
  const status = document.querySelector('#data-status');
  if (!file) { status.textContent = 'Choose the supplied task-kit ZIP first.'; return; }
  try {
    const result = await api('/api/data/import', {
      method: 'POST', headers: { 'Content-Type': 'application/zip' }, body: await file.arrayBuffer(),
    });
    status.textContent = `Imported ${result.files} files into this app's local data folder.`;
  } catch (error) { status.textContent = `Import failed: ${error.message}`; }
});

document.querySelector('#upload-contract').addEventListener('click', async () => {
  const file = document.querySelector('#contract-file').files[0];
  const status = document.querySelector('#contract-status');
  if (!file) { status.textContent = 'Choose a signed contract PDF first.'; return; }
  try {
    const result = await api(`/api/contracts/pdf?filename=${encodeURIComponent(file.name)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/pdf' }, body: await file.arrayBuffer(),
    });
    status.textContent = `${result.filename} ${result.note}`;
  } catch (error) { status.textContent = `Contract upload failed: ${error.message}`; }
});

loadBuyerQueue();
loadPolicy();
