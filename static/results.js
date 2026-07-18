const $ = (selector) => document.querySelector(selector);
const sessionId = document.querySelector(".results-wrap").dataset.sessionId;
let refreshTimer = null;
let currentAnswers = [];

function escapeHtml(value = "") { return String(value).replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char])); }
function titleCase(value = "") { return String(value).replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase()); }
function formatDate(value) { return value ? new Date(value).toLocaleString() : "—"; }
function toast(message) { const element = $("#toast"); element.textContent = message; element.classList.remove("hidden"); clearTimeout(window.toastTimer); window.toastTimer = setTimeout(() => element.classList.add("hidden"), 5200); }

function renderSummary(summary, status) {
  if (!summary) {
    const message = status === "completed" ? "The local English summary is being generated. This page refreshes automatically." : "The final summary becomes available after the interview is complete.";
    $("#summary").innerHTML = `<p class="muted">${message}</p>`; return;
  }
  const sections = [["Overall summary", summary.overall_summary], ["Strengths observed", summary.strengths_observed], ["Areas to clarify", summary.areas_to_clarify], ["Recommended follow-up questions", summary.recommended_follow_up_questions], ["Human review note", summary.human_review_note]];
  $("#summary").innerHTML = sections.map(([heading, value]) => {
    const content = Array.isArray(value) ? `<ul>${value.map((item) => `<li>${escapeHtml(item)}</li>`).join("") || "<li>None recorded.</li>"}</ul>` : `<p>${escapeHtml(value || "—")}</p>`;
    return `<div class="summary-section"><h3>${heading}</h3>${content}</div>`;
  }).join("");
}
function renderAnswers(answers) {
  $("#transcript").innerHTML = answers.length ? answers.map((answer, index) => `
    <article class="transcript-entry">
      <p class="eyebrow">Question ${index + 1} · ${escapeHtml(answer.question_category || "General")}</p>
      <p><b>Interviewer:</b> ${escapeHtml(answer.question_text)}</p>
      ${answer.answer_original && answer.answer_original !== answer.answer_english && !String(answer.answer_original).startsWith("[") ? `<p><b>Original ${escapeHtml(answer.detected_language || "spoken-language")} transcript:</b> ${escapeHtml(answer.answer_original)}</p>` : ""}
      <p><b>Candidate answer in English:</b> ${escapeHtml(answer.answer_english)}</p>
      <p class="original"><b>Model English transcript:</b> ${escapeHtml(answer.model_english || answer.answer_english)} · Quality status: ${escapeHtml(titleCase(answer.quality_status || "unreviewed"))}${answer.word_error_rate != null ? ` · WER: ${Math.round(Number(answer.word_error_rate) * 100)}%` : ""}</p>
    </article>`).join("") : '<p class="muted">No finalized answers have been recorded yet.</p>';
}
function renderReview(answers, session) {
  const reviewable = answers.filter((answer) => !String(answer.answer_english || "").startsWith("["));
  $("#reviewNote").textContent = session.quality_consent ? "This candidate gave optional quality consent. Saved corrections may update the local recognition-hints list." : "This candidate did not opt into quality improvement. Corrections improve this record but are excluded from the training vocabulary export.";
  $("#transcriptReview").innerHTML = reviewable.length ? reviewable.map((answer, index) => `
    <article class="review-entry" data-answer-id="${Number(answer.id)}">
      <p class="eyebrow">Answer ${index + 1} · ${escapeHtml(answer.question_category || "General")}</p>
      <p class="helper">Model output: ${escapeHtml(answer.model_english || answer.answer_english)}</p>
      <label>Correct English transcript<textarea class="review-text" rows="3">${escapeHtml(answer.reviewed_text || answer.answer_english || "")}</textarea></label>
      <label>Reviewer note <span class="helper">optional</span><input class="review-note" maxlength="1000" value="" placeholder="e.g., Corrected project name / Urdu phrase" /></label>
    </article>`).join("") : '<p class="muted">No reviewable answers have been recorded yet.</p>';
}
function renderAudioReview(answers) {
  const playable = answers.filter((answer) => answer.audio_available);
  $("#audioReview").innerHTML = playable.length ? playable.map((answer, index) => `
    <article class="audio-review-entry">
      <div><p class="eyebrow">Question ${index + 1} · ${escapeHtml(answer.question_category || "General")}</p><p><b>${escapeHtml(answer.question_text)}</b></p><p class="muted">${escapeHtml(answer.transcription_status || "saved")} · ${escapeHtml(answer.detected_language || "Language pending")}</p></div>
      <audio controls preload="metadata" src="/api/admin/sessions/${sessionId}/answers/${Number(answer.id)}/audio"></audio>
    </article>`).join("") : '<p class="muted">No retained audio is available for this record yet.</p>';
}

function renderConversation(turns) {
  $("#conversation").innerHTML = turns.length ? turns.map((turn) => `
    <article class="conversation-entry"><p class="eyebrow">${escapeHtml(turn.speaker)} · ${escapeHtml(titleCase(turn.kind))} · ${escapeHtml(formatDate(turn.created_at))}</p><p>${escapeHtml(turn.text_en)}</p></article>`).join("") : '<p class="muted">No additional meeting turns have been recorded yet.</p>';
}
async function load() {
  const response = await fetch(`/api/admin/sessions/${sessionId}`); const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "Unable to load meeting record.");
  const { session, identity, answers, turns, summary } = data; currentAnswers = answers;
  $("#recordTitle").textContent = session.candidate_name || "Waiting for candidate";
  $("#recordMeta").innerHTML = `
    <div class="meta"><b>Status</b>${escapeHtml(titleCase(session.status))}</div>
    <div class="meta"><b>Father / guardian</b>${escapeHtml(identity?.father_name || "—")}</div>
    <div class="meta"><b>CNIC</b>${escapeHtml(identity?.cnic || "—")}</div>
    <div class="meta"><b>Resume code</b>${escapeHtml(identity?.resume_code || "Unavailable for legacy record")}</div>
    <div class="meta"><b>Finalized answers</b>${answers.filter((answer) => Number(answer.is_final) !== 0).length}</div>
    <div class="meta"><b>Completed</b>${formatDate(session.completed_at)}</div>`;
  $("#downloadMarkdown").href = `/api/admin/sessions/${sessionId}/export?format=md`;
  $("#downloadJson").href = `/api/admin/sessions/${sessionId}/export?format=json`;
  renderSummary(summary, session.status); renderReview(answers, session); renderAudioReview(answers); renderAnswers(answers); renderConversation(turns || []);
  clearTimeout(refreshTimer); if (!summary && session.status === "completed") refreshTimer = setTimeout(() => load().catch(() => {}), 5000);
}
$("#saveReviews").addEventListener("click", async () => {
  const reviews = [...document.querySelectorAll(".review-entry")].map((entry) => ({
    answer_id: Number(entry.dataset.answerId), corrected_text: entry.querySelector(".review-text").value.trim(), reviewer_note: entry.querySelector(".review-note").value.trim(),
  })).filter((review) => review.corrected_text);
  if (!reviews.length) return toast("There are no transcripts to save.");
  try {
    const response = await fetch(`/api/admin/sessions/${sessionId}/review`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reviews }) });
    const data = await response.json(); if (!response.ok) throw new Error(data.detail || "Could not save corrections.");
    toast(data.training_eligible ? `Saved ${data.saved} corrections. Vocabulary hints updated.` : `Saved ${data.saved} corrections for this record.`); await load();
  } catch (error) { toast(error.message); }
});
$("#printRecord").addEventListener("click", () => window.print());
load().catch((error) => { $("#recordTitle").textContent = error.message; });
