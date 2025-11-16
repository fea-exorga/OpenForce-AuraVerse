// docs/app.js — robust binding, defensive checks, and clear console logs
(function () {
  const $ = id => document.getElementById(id);
  const pretty = obj => JSON.stringify(obj, null, 2);

  // elements (may be null until DOMContentLoaded)
  let fileInput, fileNameDisplay, statusMessage, schemaDisplay, schemaDiffDisplay, dataDisplay, recordsDisplay;
  let uploadBtn, diffBtn, recordsBtn;

  const API_URL = ''; // same origin by default

  function updateStatus(text, kind = '') {
    if (!statusMessage) return;
    statusMessage.textContent = text;
    statusMessage.classList.remove('success','error','pending');
    if (kind) statusMessage.classList.add(kind);
  }

  function safeJSONParse(s) {
    try { return JSON.parse(s); } catch (e) { return null; }
  }

  async function uploadSelectedFile() {
    try {
      if (!fileInput) { console.error('uploadSelectedFile: fileInput missing'); updateStatus('Internal error', 'error'); return; }
      const f = fileInput.files && fileInput.files[0];
      if (!f) { updateStatus('Please select a file first.', 'error'); return; }

      const sourceId = localStorage.getItem('openforce_source_id') || 'test_site';
      const fd = new FormData();
      fd.append('file', f);
      fd.append('source_id', sourceId);

      updateStatus('Uploading…', 'pending');
      uploadBtn.disabled = true;

      const res = await fetch(API_URL + '/upload', { method: 'POST', body: fd });
      const text = await res.text();
      const js = safeJSONParse(text) || { raw: text };

      if (!res.ok) {
        updateStatus(`Upload failed (${res.status})`, 'error');
        if (dataDisplay) dataDisplay.textContent = pretty(js);
        console.error('Upload failed response:', res.status, js);
        return;
      }

      updateStatus('Upload succeeded', 'success');
      if (dataDisplay) dataDisplay.textContent = pretty(js.processed || js);
      if (js.schema && schemaDisplay) schemaDisplay.textContent = pretty(js.schema);
      else await fetchAndShowSchema(sourceId);

      // clear file input only now (after successful upload)
      fileInput.value = '';
      if (fileNameDisplay) fileNameDisplay.textContent = 'No file selected';
    } catch (err) {
      console.error('upload error', err);
      updateStatus('Upload error', 'error');
      if (dataDisplay) dataDisplay.textContent = String(err);
    } finally {
      if (uploadBtn) uploadBtn.disabled = false;
    }
  }

  async function fetchAndShowSchema(sourceId) {
    if (!schemaDisplay) return;
    try {
      const res = await fetch(API_URL + '/schema?source_id=' + encodeURIComponent(sourceId));
      const js = await res.json();
      schemaDisplay.textContent = pretty(js || {});
    } catch (e) {
      console.error('fetch schema error', e);
      schemaDisplay.textContent = 'Could not load schema: ' + String(e);
    }
  }

  async function fetchAndShowSchemaHistory(sourceId) {
    if (!schemaDisplay || !schemaDiffDisplay) return;
    try {
      const res = await fetch(API_URL + '/schema/history?source_id=' + encodeURIComponent(sourceId));
      const docs = await res.json();
      schemaDisplay.textContent = JSON.stringify(docs.slice(0,5), null, 2);

      const latest = docs[0] || {};
      const prev = docs[1] || {};
      const latestSchema = latest.schema || {};
      const prevSchema = prev.schema || {};

      const added = {}, removed = {}, changed = {};
      for (const k of Object.keys(latestSchema)) {
        if (!(k in prevSchema)) added[k] = latestSchema[k];
        else if (prevSchema[k] !== latestSchema[k]) changed[k] = { from: prevSchema[k], to: latestSchema[k] };
      }
      for (const k of Object.keys(prevSchema)) if (!(k in latestSchema)) removed[k] = prevSchema[k];

      schemaDiffDisplay.textContent = JSON.stringify({ added, removed, changed }, null, 2);
    } catch (e) {
      console.error('schema history error', e);
      schemaDisplay.textContent = 'Error loading schema history: ' + String(e);
      schemaDiffDisplay.textContent = 'Error computing diff: ' + String(e);
    }
  }

  async function fetchAndShowRecords(sourceId) {
    if (!recordsDisplay) return;
    recordsDisplay.textContent = 'Loading records...';
    try {
      const res = await fetch(API_URL + '/records?source_id=' + encodeURIComponent(sourceId));
      if (!res.ok) {
        const t = await res.text();
        recordsDisplay.textContent = `Error: ${res.status}\n${t}`;
        return;
      }
      const docs = await res.json();
      recordsDisplay.textContent = JSON.stringify(docs, null, 2);
    } catch (e) {
      console.error('records fetch error', e);
      recordsDisplay.textContent = 'Error fetching records: ' + String(e);
    }
  }

  // Attach everything after DOM is ready — prevents race / null elements
  document.addEventListener('DOMContentLoaded', () => {
    // grab elements
    fileInput = $('file-input');
    fileNameDisplay = $('file-name');
    statusMessage = $('status-message');
    schemaDisplay = $('schema-display');
    schemaDiffDisplay = $('schema-diff-display');
    dataDisplay = $('data-display');
    recordsDisplay = $('records-display');
    uploadBtn = $('submit-button');
    diffBtn = $('view-schema-diff');
    recordsBtn = $('show-records-btn');

    // defensive: make sure elements exist
    if (!fileInput) console.warn('file-input not found in DOM');
    if (!uploadBtn) console.warn('submit-button not found in DOM');

    // file input change listener (safe)
    if (fileInput) {
      fileInput.addEventListener('change', (e) => {
        try {
          const f = e.target.files && e.target.files[0];
          if (f) {
            if (fileNameDisplay) fileNameDisplay.textContent = f.name;
            console.log('File selected:', f.name, f.size, f.type);
          } else {
            if (fileNameDisplay) fileNameDisplay.textContent = 'No file selected';
            console.log('File selection cleared');
          }
        } catch (err) {
          console.error('file change handler error', err);
        }
      });
    }

    // upload button
    if (uploadBtn) {
      uploadBtn.addEventListener('click', (ev) => {
        ev.preventDefault();
        uploadSelectedFile();
      });
    }

    // schema diff button
    if (diffBtn) {
      diffBtn.addEventListener('click', (ev) => {
        ev.preventDefault();
        const sid = localStorage.getItem('openforce_source_id') || 'test_site';
        fetchAndShowSchemaHistory(sid);
      });
    }

    // records button
    if (recordsBtn) {
      recordsBtn.addEventListener('click', (ev) => {
        ev.preventDefault();
        const sid = localStorage.getItem('openforce_source_id') || 'test_site';
        fetchAndShowRecords(sid);
      });
    }

    // initial load of schemas (safe)
    try {
      const sid = localStorage.getItem('openforce_source_id') || 'test_site';
      fetchAndShowSchema(sid);
    } catch (e) {
      console.error('initial schema load error', e);
    }

  }); // DOMContentLoaded
})();
