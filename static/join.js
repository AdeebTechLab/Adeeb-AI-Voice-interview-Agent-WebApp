const $ = (selector) => document.querySelector(selector);
let pendingMeetingUrl = "";
let disclaimerAccepted = false;

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.remove("hidden");
  clearTimeout(window.toastTimer);
  window.toastTimer = setTimeout(() => el.classList.add("hidden"), 6000);
}

function normaliseCnicInput(value) {
  const digits = String(value || "").replace(/\D/g, "").slice(0, 13);
  if (digits.length <= 5) return digits;
  if (digits.length <= 12) return `${digits.slice(0, 5)}-${digits.slice(5)}`;
  return `${digits.slice(0, 5)}-${digits.slice(5, 12)}-${digits.slice(12)}`;
}

function setFormLock(locked) {
  const form = $("#universalJoinForm");
  form.classList.toggle("form-locked", locked);
  form.setAttribute("aria-disabled", String(locked));
  form.querySelectorAll("input, select, button").forEach((control) => {
    control.disabled = locked;
  });
}

$("#cnic").addEventListener("input", (event) => {
  event.target.value = normaliseCnicInput(event.target.value);
});

$("#disclaimerAcknowledgement").addEventListener("change", (event) => {
  $("#acceptDisclaimer").disabled = !event.target.checked;
});

$("#acceptDisclaimer").addEventListener("click", () => {
  if (!$("#disclaimerAcknowledgement").checked) return;
  disclaimerAccepted = true;
  $("#audioDisclaimer").classList.add("hidden");
  setFormLock(false);
  $("#candidateName").focus();
});

$("#universalJoinForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!disclaimerAccepted) {
    toast("Please acknowledge the audio-quality notice first.");
    return;
  }
  const consent = $("#consent").checked;
  if (!consent) {
    toast("Please confirm recording, HR playback, and transcription consent.");
    return;
  }
  const payload = {
    candidate_name: $("#candidateName").value.trim(),
    father_name: $("#fatherName").value.trim(),
    cnic: $("#cnic").value.trim(),
    preferred_language: $("#preferredLanguage").value,
    quality_consent: $("#qualityConsent").checked,
    resume_code: $("#resumeCode").value.trim(),
  };
  const button = event.currentTarget.querySelector("button[type='submit']");
  button.disabled = true;
  $("#joinStatus").textContent = "Creating your secure interview record…";
  try {
    const response = await fetch("/api/join/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || "Could not create the interview record.");
    pendingMeetingUrl = new URL(
      result.session_url,
      window.location.origin
    ).href;
    if (result.resumed) {
      window.location.assign(pendingMeetingUrl);
      return;
    }
    $("#resumeCodeValue").textContent = result.resume_code || "—";
    $(".join-grid").classList.add("hidden");
    $("#resumeCodeCard").classList.remove("hidden");
    $("#joinStatus").textContent = "Registration complete.";
  } catch (error) {
    $("#joinStatus").textContent = "Check your details and try again.";
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});

$("#openMeeting").addEventListener("click", () => {
  if (pendingMeetingUrl) window.location.assign(pendingMeetingUrl);
});

setFormLock(true);
