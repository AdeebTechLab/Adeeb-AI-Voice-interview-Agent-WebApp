const $ = (selector) => document.querySelector(selector);
let questionConfig = { questions: [] };

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  }[character]));
}
function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.remove("hidden");
  clearTimeout(window.toastTimer);
  window.toastTimer = window.setTimeout(() => element.classList.add("hidden"), 5200);
}
async function api(url, options = {}) {
  const response = await fetch(url, options);
  let data = {};
  try { data = await response.json(); } catch (_) { /* no JSON */ }
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
  return data;
}
function formatDate(value) { return value ? new Date(value).toLocaleString() : "—"; }
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast("Universal candidate link copied."); }
  catch (_) { window.prompt("Copy this candidate link:", text); }
}

function questionTemplate(question = {}, position) {
  const followups = Number(question.max_followups ?? 1);
  return `
    <article class="question-editor">
      <div class="editor-top"><h3>Question ${position}</h3><button type="button" class="remove-question">Remove</button></div>
      <div class="editor-grid four">
        <label>ID<input class="q-id" value="${escapeHtml(question.id || `question_${position}`)}" maxlength="64" /></label>
        <label>Category<input class="q-category" value="${escapeHtml(question.category || "General")}" maxlength="80" /></label>
        <label>Maximum answer seconds<input class="q-seconds" type="number" min="15" max="900" value="${Number(question.max_seconds || 90)}" /></label>
        <label>Adaptive follow-ups<select class="q-followups"><option value="0" ${followups === 0 ? "selected" : ""}>No follow-up</option><option value="1" ${followups === 1 ? "selected" : ""}>Up to 1</option><option value="2" ${followups === 2 ? "selected" : ""}>Up to 2</option></select></label>
      </div>
      <label>Question spoken in English<textarea class="q-text" rows="3" required>${escapeHtml(question.text || "")}</textarea></label>
    </article>`;
}
function attachQuestionEvents(card) {
  card.querySelector(".remove-question").addEventListener("click", () => {
    if (document.querySelectorAll(".question-editor").length === 1) return toast("Keep at least one interview question.");
    card.remove(); numberQuestionCards();
  });
}
function renderQuestions() {
  const box = $("#questions");
  box.innerHTML = questionConfig.questions.map((question, index) => questionTemplate(question, index + 1)).join("");
  box.querySelectorAll(".question-editor").forEach(attachQuestionEvents);
}
function numberQuestionCards() { document.querySelectorAll(".question-editor h3").forEach((heading, index) => { heading.textContent = `Question ${index + 1}`; }); }
function readConfigFromForm() {
  return {
    meeting_title: $("#meetingTitle").value.trim() || "Adeeb AI Meeting",
    interview_title: $("#meetingTitle").value.trim() || "Interview",
    bot_name: $("#botName").value.trim() || "Adeeb AI Meeting Agent",
    welcome_message: $("#welcomeMessage").value.trim(),
    closing_message: $("#closingMessage").value.trim(),
    default_max_followups: 1,
    transcription_hints: $("#transcriptionHints").value.trim(),
    questions: [...document.querySelectorAll(".question-editor")].map((card) => {
      const followups = Number(card.querySelector(".q-followups").value || 0);
      return {
        id: card.querySelector(".q-id").value.trim(),
        category: card.querySelector(".q-category").value.trim() || "General",
        text: card.querySelector(".q-text").value.trim(),
        required: true,
        max_seconds: Number(card.querySelector(".q-seconds").value || 90),
        adaptive_follow_up: followups > 0,
        max_followups: followups,
      };
    }),
  };
}

async function loadQuestions() {
  questionConfig = await api("/api/admin/questions");
  $("#meetingTitle").value = questionConfig.meeting_title || questionConfig.interview_title || "";
  $("#botName").value = questionConfig.bot_name || "Adeeb AI Meeting Agent";
  $("#welcomeMessage").value = questionConfig.welcome_message || "";
  $("#closingMessage").value = questionConfig.closing_message || "";
  $("#transcriptionHints").value = Array.isArray(questionConfig.transcription_hints)
    ? questionConfig.transcription_hints.join(", ") : (questionConfig.transcription_hints || "");
  renderQuestions();
}
async function loadKnowledge() { const data = await api("/api/admin/knowledge"); $("#knowledgeBase").value = data.content || ""; }
async function loadRag() {
  const data = await api("/api/admin/rag");
  const { settings } = data;
  $("#ragSourceMode").value = settings.source_mode || "local_csv";
  $("#googleSheetCsvUrl").value = settings.google_sheet_csv_url || "";
  $("#ragSyncMinutes").value = settings.auto_sync_minutes || 30;
  $("#ragStatus").textContent = `${data.available_records} searchable records · ${data.blocked_sensitive_records} sensitive rows blocked · ${settings.last_sync_status || ""}`;
}

async function loadPdfRag() {
  const data = await api("/api/admin/rag/pdf");
  const status = $("#pdfRagStatus");
  const docsBox = $("#pdfRagDocs");
  if (!status || !docsBox) return;
  status.textContent = `${data.document_count} PDF document(s) · ${data.chunk_count} searchable chunk(s) · ${data.characters || 0} characters`;
  if (!data.documents.length) {
    docsBox.innerHTML = "No PDF uploaded yet.";
    return;
  }
  docsBox.innerHTML = data.documents.map((doc) => `
    <div class="pdf-doc-row">
      <strong>${escapeHtml(doc.filename)}</strong>
      <span>${escapeHtml(doc.pages)} pages · ${escapeHtml(doc.chunks)} chunks · ${escapeHtml(formatDate(doc.uploaded_at))}</span>
      <button class="danger-link remove-pdf" type="button" data-doc-id="${escapeHtml(doc.doc_id)}">Remove</button>
    </div>`).join("");
  docsBox.querySelectorAll(".remove-pdf").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!window.confirm("Remove this PDF from Adeeb knowledge?")) return;
      await api(`/api/admin/rag/pdf/${encodeURIComponent(button.dataset.docId)}`, { method: "DELETE" });
      toast("PDF knowledge removed.");
      await Promise.all([loadPdfRag(), loadRag()]);
    });
  });
}
async function loadQuality() {
  const data = await api("/api/admin/quality");
  const metrics = data.metrics;
  const score = metrics.estimated_word_accuracy == null ? "Not measured" : `${metrics.estimated_word_accuracy}%`;
  $("#qualityMetrics").innerHTML = `
    <div class="quality-stat"><span>Reviewed turns</span><strong>${escapeHtml(metrics.reviewed_turns)}</strong></div>
    <div class="quality-stat"><span>Measured word accuracy</span><strong>${escapeHtml(score)}</strong></div>
    <div class="quality-stat"><span>90% target</span><strong>${metrics.evaluation_ready && Number(metrics.estimated_word_accuracy) >= 90 ? "Reached in sample" : "Not yet verified"}</strong></div>
    <div class="quality-stat"><span>Learned recognition hints</span><strong>${escapeHtml(data.learned_hint_count)}</strong></div>
    <p class="quality-message">${escapeHtml(metrics.message)}</p>`;
}
async function loadHealth() {
  try {
    const health = await api("/api/health");
    const pill = $("#healthPill");
    const speech = health.speech_model_loaded ? "Speech ready" : "Speech warming";
    const models = health.ollama_reachable ? "Ollama ready" : "Ollama unavailable";
    pill.textContent = `${speech} · ${models}`;
    pill.className = `health-pill ${health.speech_model_loaded && health.ollama_reachable ? "ok" : "problem"}`;
  } catch (_) { $("#healthPill").textContent = "Could not check local services"; $("#healthPill").classList.add("problem"); }
}
async function loadUniversalLink() {
  const data = await api("/api/admin/universal-link");
  $("#universalJoinLink").value = new URL(data.join_path || "/join", window.location.origin).href;
}

async function loadSessions() {
  const sessions = await api("/api/admin/sessions");
  const body = $("#sessionsBody");
  if (!sessions.length) {
    body.innerHTML = '<tr><td colspan="9" class="muted">No candidate records yet. Share the universal candidate link to begin.</td></tr>';
    return;
  }
  body.innerHTML = sessions.map((session) => {
    const resultUrl = new URL(session.results_url || `/results/${session.id}`, window.location.origin).href;
    const resumeCode = session.resume_code || "Unavailable";
    return `
      <tr data-session-id="${escapeHtml(session.id)}">
        <td>${escapeHtml(session.candidate_display_name || "Waiting for candidate")}</td>
        <td>${escapeHtml(session.father_name || "—")}</td>
        <td><span class="cnic-full">${escapeHtml(session.cnic_full || "—")}</span></td>
        <td><code class="resume-code">${escapeHtml(resumeCode)}</code>${!session.resume_code && session.status !== "completed" ? '<button class="table-action reset-resume" type="button">Generate</button>' : ""}</td>
        <td><span class="status ${escapeHtml(session.status)}">${escapeHtml(session.status.replaceAll("_", " "))}</span></td>
        <td>${escapeHtml(session.progress || "0/0")}</td>
        <td>${escapeHtml(formatDate(session.created_at))}</td>
        <td><a href="${escapeHtml(resultUrl)}">Open record</a></td>
        <td><button class="danger-button delete-session" type="button">Delete</button></td>
      </tr>`;
  }).join("");
}


$("#sessionsBody").addEventListener("click", async (event) => {
  const row = event.target.closest("tr[data-session-id]");
  if (!row) return;
  const sessionId = row.dataset.sessionId;
  if (event.target.closest(".delete-session")) {
    const name = row.children[0]?.textContent?.trim() || "this candidate";
    const cnic = row.children[2]?.textContent?.trim() || "this CNIC";
    if (!window.confirm(`Delete ${name} (${cnic})? This permanently removes the record, transcripts, and saved audio. The CNIC can then be used again.`)) return;
    try {
      await api(`/api/admin/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
      toast("Candidate record deleted. The CNIC can be used again.");
      await loadSessions();
    } catch (error) { toast(error.message); }
  }
  if (event.target.closest(".reset-resume")) {
    try {
      const result = await api(`/api/admin/sessions/${encodeURIComponent(sessionId)}/resume-code/reset`, { method: "POST" });
      toast(`New resume code: ${result.resume_code}`);
      await loadSessions();
    } catch (error) { toast(error.message); }
  }
});

$("#saveQuestions").addEventListener("click", async () => {
  try { const payload = readConfigFromForm(); await api("/api/admin/questions", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); questionConfig = payload; toast("Meeting setup saved. New links use this configuration."); }
  catch (error) { toast(error.message); }
});
$("#saveKnowledge").addEventListener("click", async () => {
  try { await api("/api/admin/knowledge", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: $("#knowledgeBase").value }) }); toast("Approved Markdown knowledge saved."); }
  catch (error) { toast(error.message); }
});
$("#saveRag").addEventListener("click", async () => {
  try {
    const payload = { source_mode: $("#ragSourceMode").value, google_sheet_csv_url: $("#googleSheetCsvUrl").value.trim(), auto_sync_minutes: Number($("#ragSyncMinutes").value || 30) };
    await api("/api/admin/rag", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    toast("RAG source saved."); await loadRag();
  } catch (error) { toast(error.message); }
});
$("#syncRag").addEventListener("click", async () => { try { const result = await api("/api/admin/rag/sync", { method: "POST" }); toast(`Google Sheet synchronized: ${result.records} records.`); await loadRag(); } catch (error) { toast(error.message); } });
$("#uploadPdfRag")?.addEventListener("click", async () => {
  try {
    const fileInput = $("#pdfRagFile");
    if (!fileInput?.files?.length) return toast("Choose a PDF file first.");
    const form = new FormData();
    form.append("file", fileInput.files[0]);
    const result = await api("/api/admin/rag/pdf/upload", { method: "POST", body: form });
    fileInput.value = "";
    toast(`PDF imported: ${result.chunks} searchable chunks.`);
    await Promise.all([loadPdfRag(), loadRag()]);
  } catch (error) { toast(error.message); }
});
$("#generatePdfQuestions")?.addEventListener("click", async () => {
  try {
    const data = await api("/api/admin/rag/pdf/questions");
    const box = $("#pdfRagQuestions");
    if (!box) return;
    if (!data.questions.length) {
      box.innerHTML = '<p class="muted">Upload a readable PDF first, then generate questions.</p>';
      return;
    }
    box.innerHTML = `<h3>Generated PDF-based questions</h3>${data.questions.map((item, index) => `
      <article class="generated-question">
        <strong>${index + 1}. ${escapeHtml(item.question)}</strong>
        <p class="helper">Expected keywords: <code>${escapeHtml(item.expected_keywords)}</code></p>
        <p class="helper">Source: ${escapeHtml(item.pdf_filename || item.source || "PDF")}</p>
      </article>`).join("")}`;
    toast("Generated 5 PDF-based question prompts.");
  } catch (error) { toast(error.message); }
});
$("#addQuestion").addEventListener("click", () => { const position = document.querySelectorAll(".question-editor").length + 1; $("#questions").insertAdjacentHTML("beforeend", questionTemplate({}, position)); const card = $("#questions").lastElementChild; attachQuestionEvents(card); card.querySelector(".q-text").focus(); });
$("#copyUniversalLink").addEventListener("click", () => copyText($("#universalJoinLink").value));

$("#refreshSessions").addEventListener("click", () => loadSessions().catch((error) => toast(error.message)));
Promise.all([loadQuestions(), loadKnowledge(), loadRag(), loadPdfRag(), loadQuality(), loadHealth(), loadUniversalLink(), loadSessions()]).catch((error) => toast(error.message));
