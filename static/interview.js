const $ = (selector) => document.querySelector(selector);
const shell = document.querySelector(".meeting-shell");
const sessionId = shell.dataset.sessionId;

let state = null;
let audioStream = null;
let videoStream = null;
let audioContext = null;
let analyser = null;
let analyserData = null;
let recorder = null;
let recorderShouldSubmit = false;
let chunks = [];
let monitoringFrame = null;
let listening = false;
let botSpeaking = false;
let processingTurn = false;
let autoListen = true;
let hasSpeech = false;
let lastSpeechAt = 0;
let turnStartedAt = 0;
let turnTimerId = null;
let meetingTimerId = null;
let meetingStartedAt = null;
let speechToken = 0;
let noiseSamples = [];
let adaptiveNoiseFloor = 0.004;
let consecutiveVoiceFrames = 0;
let noResponseTimerId = null;
let cloudVoiceDisabledUntil = 0;
let audioUnlocked = false;
let pendingSpeechRequest = null;
let activeAudioObjectUrl = null;
let lastFailedTurn = null;
const agentAudio = document.querySelector("#agentAudioElement") || new Audio();
agentAudio.preload = "auto";
agentAudio.playsInline = true;
agentAudio.setAttribute("playsinline", "");

// Audio turn-taking is tuned for intelligibility before speed. Urdu and Hindi often
// contain longer natural pauses than English, so their auto-submit window is longer.
const MIN_VOICE_THRESHOLD = 0.012;
const MAX_VOICE_THRESHOLD = 0.055;
const ENGLISH_SILENCE_TO_SUBMIT_MS = 1200;
const URDU_HINDI_SILENCE_TO_SUBMIT_MS = 2400;
const MAX_TURN_MS = 60000;
const NO_RESPONSE_SKIP_MS = 60000;
const PRE_SPEECH_CALIBRATION_MS = 850;
const MIN_CAPTURE_MS = 900;
const REQUIRED_VOICE_FRAMES = 3;
// The first four normal answers can be saved and transcribed in a controlled backend queue.
// Short voice commands and the final project section are always understood immediately.

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.remove("hidden");
  clearTimeout(window.toastTimer);
  window.toastTimer = window.setTimeout(() => element.classList.add("hidden"), 5600);
}

async function api(url, options = {}) {
  const controller = new AbortController();
  const activeLanguage = String(document.querySelector("#answerLanguage")?.value || state?.candidate_language || "en");
  const turnTimeoutMs = activeLanguage === "ur" ? 180000 : activeLanguage === "hi" ? 120000 : 90000;
  const timeoutMs = Number(options.timeoutMs || (url.endsWith("/turn") ? turnTimeoutMs : 60000));
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  const fetchOptions = { ...options, signal: options.signal || controller.signal };
  delete fetchOptions.timeoutMs;
  try {
    const response = await fetch(url, fetchOptions);
    let data = {};
    try {
      data = await response.json();
    } catch (_) {
      // The API normally returns JSON. This keeps a useful browser error if a proxy returns HTML.
    }
    if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("The agent took too long to respond. Please use Next or try speaking again.");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function formatTime(seconds) {
  const value = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
}

function setBotState(label, speaking = false) {
  $("#botStateBadge").textContent = label;
  $("#botTile").classList.toggle("speaking", speaking);
}

function setListeningStatus(message, mode = "waiting") {
  $("#recordingStatus").textContent = message;
  $("#listeningPanel").dataset.mode = mode;
  $("#candidateMicBadge").textContent = mode === "listening"
    ? "Listening"
    : mode === "thinking"
      ? "Saving"
      : mode === "paused"
        ? "Mic paused"
        : "Mic ready";
}

function setMicActivity(active) {
  $("#micRing").classList.toggle("active", active);
}

function updateMeetingTimer() {
  if (!meetingStartedAt) return;
  $("#elapsedMeeting").textContent = formatTime((Date.now() - meetingStartedAt) / 1000);
}

function updateTurnTimer() {
  if (!turnStartedAt || !listening) return;
  $("#turnTimer").textContent = formatTime((performance.now() - turnStartedAt) / 1000);
}

function candidateInitial(name = "") {
  return (String(name).trim().charAt(0) || "C").toUpperCase();
}

function renderState() {
  if (!state) return;

  $("#meetingTitle").textContent = state.meeting_title || "AI Interview Meeting";
  $("#botName").textContent = state.bot_name || "Adeeb AI Meeting Agent";
  $("#candidateNameLabel").textContent = state.candidate_name || "Candidate";
  $("#candidateAvatar").textContent = candidateInitial(state.candidate_name);
  $("#completeName").textContent = state.candidate_name || "Candidate";
  if (state.candidate_language && ["auto", "en", "ur", "hi"].includes(state.candidate_language)) {
    $("#answerLanguage").value = state.candidate_language;
  }
  const activeLanguage = String($("#answerLanguage").value || "en");
  $("#languageBadge").textContent = activeLanguage.toUpperCase();
  const isRtl = activeLanguage === "ur";
  $("#questionText").dir = isRtl ? "rtl" : "ltr";
  $("#questionText").classList.toggle("rtl-text", isRtl);
  if ($("#languageLockStatus")) {
    $("#languageLockStatus").textContent = activeLanguage === "ur"
      ? "Urdu locked — say ‘talk in English’ to change"
      : activeLanguage === "hi"
        ? "Hindi locked — say ‘talk in English’ to change"
        : activeLanguage === "auto"
          ? "Mixed-language detection"
          : "English locked — say ‘talk in Urdu’ to change";
  }

  const question = state.question;
  const total = Number(state.total_questions || 0);
  const complete = Number(state.completed_questions || 0);
  const pendingText = "";

  if (!question) {
    $("#progressText").textContent = `${total} of ${total} complete`;
    $("#progressBar").style.width = "100%";
    $("#promptHelper").textContent = `Your meeting is complete.${pendingText}`;
    return;
  }

  const ordinal = Math.min(total, complete + 1);
  $("#progressText").textContent = question.prompt_type === "follow_up"
    ? `Clarification · ${ordinal}/${total}`
    : `Question ${ordinal} of ${total}`;
  $("#progressBar").style.width = `${total ? (complete / total) * 100 : 0}%`;
  $("#promptLabel").textContent = question.prompt_type === "follow_up" ? "Adeeb follow-up" : "Current question";
  $("#questionText").textContent = question.text || "Waiting for the next question…";
  const backgroundPrompt = String(question.transcription_mode || "immediate") === "background";
  $("#promptHelper").textContent = question.prompt_type === "follow_up"
    ? `This project follow-up is transcribed immediately so Adeeb can understand your evidence. Use Next to skip or Repeat to hear it again.${pendingText}`
    : backgroundPrompt
      ? `Your answer is saved first and transcribed in the backend. After the skills section, Adeeb uses the completed transcript to create the next relevant question. Short spoken commands are handled immediately.${pendingText}`
      : `This project answer is transcribed immediately. Adeeb then asks two focused follow-ups about your project depth and skills. Use Next or Repeat for instant controls.${pendingText}`;
}

function addCaption(speaker, text) {
  if (!text) return;
  const container = $("#captions");
  if (container.querySelector(".caption-muted")) container.innerHTML = "";
  const row = document.createElement("article");
  row.className = `caption ${speaker === "Adeeb" ? "bot-caption" : "candidate-caption"}`;
  const label = document.createElement("strong");
  label.textContent = speaker;
  const content = document.createElement("p");
  content.textContent = text;
  if (/[؀-ۿ]/.test(text)) content.dir = "rtl";
  else if (/[ऀ-ॿ]/.test(text)) content.dir = "ltr";
  row.append(label, content);
  container.prepend(row);
  while (container.children.length > 5) container.lastElementChild.remove();
}

function selectedAgentLanguage() {
  const value = state?.candidate_language || $("#answerLanguage")?.value || "en";
  return ["ur", "hi", "en"].includes(value) ? value : "en";
}

function speechLocaleFor(language) {
  if (language === "ur") return "ur-PK";
  if (language === "hi") return "hi-IN";
  return "en-US";
}

function getNativeSpeechVoice(language = "en") {
  const voices = window.speechSynthesis?.getVoices?.() || [];
  const locale = speechLocaleFor(language).toLowerCase();
  const prefix = locale.split("-")[0];
  return voices.find((voice) => voice.lang?.toLowerCase() === locale)
    || voices.find((voice) => voice.lang?.toLowerCase().startsWith(prefix))
    || null;
}

function getSpeechVoice(language = "en") {
  const voices = window.speechSynthesis?.getVoices?.() || [];
  const nativeVoice = getNativeSpeechVoice(language);
  if (nativeVoice) return nativeVoice;
  return voices.find((voice) => /female|zira|susan|aria|jenny|hazel|google us english|microsoft aria/i.test(voice.name))
    || voices.find((voice) => voice.lang?.toLowerCase().startsWith("en"))
    || voices[0]
    || null;
}

function setVoiceStatus(message, mode = "ready") {
  const el = $("#voicePlaybackStatus");
  if (!el) return;
  el.textContent = message;
  el.dataset.mode = mode;
}

function showAudioUnlockButton(show, message = "Tap once to enable Adeeb's voice on this phone.") {
  const button = $("#audioUnlockButton");
  const notice = $("#audioUnlockNotice");
  if (button) button.classList.toggle("hidden", !show);
  if (notice) {
    notice.classList.toggle("hidden", !show);
    notice.textContent = message;
  }
}

function unlockAudioPlayback() {
  // Mobile Chrome and iOS require speaker playback to begin inside a direct user
  // gesture. Prime one persistent audio element before any awaited network request.
  try {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (AudioContextClass) {
      if (!window.__adeebPlaybackContext) window.__adeebPlaybackContext = new AudioContextClass();
      window.__adeebPlaybackContext.resume?.().catch(() => {});
    }
  } catch (_) {}

  try {
    window.speechSynthesis?.resume?.();
    const primer = new SpeechSynthesisUtterance(" ");
    primer.volume = 0;
    window.speechSynthesis?.speak?.(primer);
  } catch (_) {}

  // A tiny silent WAV is played on the same element later used for real agent audio.
  const silentWav = "data:audio/wav;base64,UklGRigAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQQAAACAgICA";
  try {
    agentAudio.onended = null;
    agentAudio.onerror = null;
    agentAudio.pause();
    agentAudio.src = silentWav;
    agentAudio.volume = 0.01;
    const attempt = agentAudio.play();
    if (attempt?.then) {
      attempt.then(() => {
        agentAudio.pause();
        agentAudio.currentTime = 0;
        agentAudio.volume = 1;
        audioUnlocked = true;
        showAudioUnlockButton(false);
        setVoiceStatus("Speaker ready", "ready");
      }).catch(() => {
        audioUnlocked = false;
      });
    } else {
      audioUnlocked = true;
    }
  } catch (_) {
    audioUnlocked = false;
  }
}

function queueManualSpeechRetry(text, options) {
  pendingSpeechRequest = { text, options };
  showAudioUnlockButton(true);
  setVoiceStatus("Voice blocked by the phone — tap Hear Adeeb", "blocked");
  setBotState("Tap to hear", false);
  setListeningStatus("Your phone blocked automatic audio. Tap Hear Adeeb to continue.", "paused");
}

function finishSpeaking(token, listenAfter, onend) {
  if (token !== speechToken) return;
  botSpeaking = false;
  setBotState("Ready", false);
  setVoiceStatus("Speaker ready", "ready");
  if (onend) onend();
  if (listenAfter) startListeningSoon();
}

async function browserTextForSpeech(text, language) {
  // Never convert Urdu into Roman Urdu. Edge neural audio is the normal path; if
  // the browser must speak locally, keep the exact native-script prompt and locale.
  return { text, language };
}

async function playBrowserSpeech(text, token, listenAfter, onend, language = selectedAgentLanguage()) {
  if (!("speechSynthesis" in window)) {
    window.setTimeout(() => finishSpeaking(token, listenAfter, onend), Math.min(1800 + text.length * 24, 9000));
    return;
  }

  const speechText = await browserTextForSpeech(text, language);
  if (token !== speechToken) return;
  window.speechSynthesis.cancel();
  window.speechSynthesis.resume?.();
  const utterance = new SpeechSynthesisUtterance(speechText.text);
  utterance.lang = speechLocaleFor(speechText.language);
  utterance.rate = speechText.language === "en" ? 0.93 : 0.88;
  utterance.pitch = 1;
  const voice = getSpeechVoice(speechText.language);
  if (voice) utterance.voice = voice;
  let ended = false;
  const safetyMs = Math.min(2500 + speechText.text.length * 85, 30000);
  const safetyTimer = window.setTimeout(() => {
    if (!ended) finishSpeaking(token, listenAfter, onend);
  }, safetyMs);
  utterance.onend = () => {
    ended = true;
    clearTimeout(safetyTimer);
    finishSpeaking(token, listenAfter, onend);
  };
  utterance.onerror = (event) => {
    ended = true;
    clearTimeout(safetyTimer);
    if (event?.error === "not-allowed" || event?.error === "audio-busy") {
      queueManualSpeechRetry(text, { listenAfter, onend });
      return;
    }
    finishSpeaking(token, listenAfter, onend);
  };
  window.speechSynthesis.speak(utterance);
}

function speakBot(text, { listenAfter = false, onend = null } = {}) {
  if (!text) {
    if (onend) onend();
    if (listenAfter) startListeningSoon();
    return;
  }

  stopListening({ submit: false });
  clearTimeout(noResponseTimerId);
  speechToken += 1;
  const token = speechToken;
  botSpeaking = true;
  setBotState("Speaking", true);
  setVoiceStatus("Preparing voice…", "loading");
  setListeningStatus("Adeeb is speaking. Your microphone will listen when the message ends.", "waiting");

  const language = selectedAgentLanguage();
  const shouldTryCloud = Date.now() > cloudVoiceDisabledUntil;
  if (!shouldTryCloud) {
    playBrowserSpeech(text, token, listenAfter, onend, language);
    return;
  }

  fetch("/api/voice/tts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, language }),
  })
    .then((response) => {
      if (!response.ok) throw new Error("Cloud voice unavailable");
      return response.blob();
    })
    .then((blob) => {
      if (token !== speechToken) return;
      if (activeAudioObjectUrl) URL.revokeObjectURL(activeAudioObjectUrl);
      activeAudioObjectUrl = URL.createObjectURL(blob);
      agentAudio.pause();
      agentAudio.src = activeAudioObjectUrl;
      agentAudio.volume = 1;
      agentAudio.load();
      agentAudio.onended = () => {
        if (activeAudioObjectUrl) URL.revokeObjectURL(activeAudioObjectUrl);
        activeAudioObjectUrl = null;
        finishSpeaking(token, listenAfter, onend);
      };
      agentAudio.onerror = () => {
        if (activeAudioObjectUrl) URL.revokeObjectURL(activeAudioObjectUrl);
        activeAudioObjectUrl = null;
        cloudVoiceDisabledUntil = Date.now() + 60000;
        playBrowserSpeech(text, token, listenAfter, onend, language);
      };
      setVoiceStatus(language === "ur" ? "Playing Urdu voice" : "Playing agent voice", "playing");
      agentAudio.play().then(() => {
        audioUnlocked = true;
        showAudioUnlockButton(false);
      }).catch((error) => {
        if (activeAudioObjectUrl) URL.revokeObjectURL(activeAudioObjectUrl);
        activeAudioObjectUrl = null;
        if (error?.name === "NotAllowedError") {
          queueManualSpeechRetry(text, { listenAfter, onend });
          return;
        }
        cloudVoiceDisabledUntil = Date.now() + 60000;
        playBrowserSpeech(text, token, listenAfter, onend, language);
      });
    })
    .catch(() => {
      cloudVoiceDisabledUntil = Date.now() + 180000;
      playBrowserSpeech(text, token, listenAfter, onend, language);
    });
}

async function speakWelcomeThenQuestion() {
  let welcome = String(state?.welcome_message || "").replace("{candidate_name}", state?.candidate_name || "Candidate");
  let question = state?.question?.text || "";
  try {
    const spoken = await api(`/api/interview/${sessionId}/spoken-current`);
    if (spoken?.language && ["en", "ur", "hi"].includes(spoken.language)) {
      state.candidate_language = spoken.language;
      applyLanguageFromResult({ state });
    }
    welcome = spoken?.welcome || welcome;
    question = spoken?.question || question;
    if (state?.question && question) {
      state.question.text = question;
      renderState();
    }
  } catch (_) {
    // Fallback to the visible English prompt if the translation service is unavailable.
  }
  speakBot(welcome, { listenAfter: false, onend: () => speakBot(question, { listenAfter: true }) });
}

function supportedAudioConstraints() {
  return navigator.mediaDevices?.getSupportedConstraints?.() || {};
}

async function requestAudio() {
  if (audioStream?.active) return audioStream;
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error("This browser does not support microphone access. Use the latest Chrome, Edge, Firefox, or Safari browser.");
  }

  const supported = supportedAudioConstraints();
  const audio = {
    channelCount: { ideal: 1 },
    sampleRate: { ideal: 48000 },
    sampleSize: { ideal: 16 },
    latency: { ideal: 0.02 },
  };
  if (supported.echoCancellation) audio.echoCancellation = true;
  if (supported.noiseSuppression) audio.noiseSuppression = true;
  if (supported.autoGainControl) audio.autoGainControl = true;

  audioStream = await navigator.mediaDevices.getUserMedia({ audio, video: false });
  setupAnalyser();
  const track = audioStream.getAudioTracks()[0];
  const settings = track?.getSettings?.() || {};
  const quality = [
    settings.noiseSuppression === true ? "noise reduction" : "standard noise handling",
    settings.echoCancellation === true ? "echo control" : "standard echo handling",
  ];
  setListeningStatus(`Microphone ready with ${quality.join(" and ")}.`, "waiting");
  return audioStream;
}

function setupAnalyser() {
  if (!audioStream || analyser) return;
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) return;
  audioContext = new AudioContextClass();
  const source = audioContext.createMediaStreamSource(audioStream);
  analyser = audioContext.createAnalyser();
  analyser.fftSize = 1024;
  analyser.smoothingTimeConstant = 0.78;
  analyserData = new Uint8Array(analyser.fftSize);
  source.connect(analyser);
}

function supportedMimeType() {
  return [
    "audio/webm;codecs=opus",
    "audio/ogg;codecs=opus",
    "audio/mp4",
    "audio/webm",
  ].find((type) => window.MediaRecorder?.isTypeSupported?.(type));
}

function extensionForMime(mimeType = "") {
  const value = String(mimeType).toLowerCase();
  if (value.includes("ogg")) return "ogg";
  if (value.includes("mp4")) return "m4a";
  if (value.includes("wav")) return "wav";
  return "webm";
}

function currentSilenceWindow() {
  const language = $("#answerLanguage")?.value || "auto";
  return language === "ur" || language === "hi" || language === "auto"
    ? URDU_HINDI_SILENCE_TO_SUBMIT_MS
    : ENGLISH_SILENCE_TO_SUBMIT_MS;
}

function median(values) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function startListeningSoon() {
  if (!autoListen || processingTurn || state?.status === "completed") return;
  clearTimeout(noResponseTimerId);
  window.setTimeout(() => startListening().catch((error) => toast(error.message)), 250);
}

async function startListening() {
  if (!autoListen || listening || processingTurn || botSpeaking || state?.status === "completed") return;
  await requestAudio();
  if (audioContext?.state === "suspended") await audioContext.resume();
  if (!window.MediaRecorder) {
    throw new Error("This browser does not support automatic audio recording. Try Chrome or Edge.");
  }

  chunks = [];
  hasSpeech = false;
  lastSpeechAt = 0;
  noiseSamples = [];
  adaptiveNoiseFloor = 0.004;
  consecutiveVoiceFrames = 0;
  turnStartedAt = performance.now();

  const mimeType = supportedMimeType();
  const options = { audioBitsPerSecond: 128000 };
  if (mimeType) options.mimeType = mimeType;

  const activeRecorder = new MediaRecorder(audioStream, options);
  recorder = activeRecorder;
  recorderShouldSubmit = false;
  activeRecorder.ondataavailable = (event) => {
    if (event.data?.size) chunks.push(event.data);
  };

  activeRecorder.onstop = () => {
    const shouldSend = recorderShouldSubmit;
    const actualMime = activeRecorder.mimeType || mimeType || "audio/webm";
    if (recorder === activeRecorder) recorder = null;

    if (shouldSend && hasSpeech && chunks.length) {
      const blob = new Blob(chunks, { type: actualMime });
      if (blob.size >= 650) {
        const durationMs = Math.round(performance.now() - turnStartedAt);
        queueAnswer(blob, durationMs, actualMime).catch((error) => {
          processingTurn = false;
          autoListen = false;
          lastFailedTurn = { blob, durationMs, mimeType: actualMime };
          const retryButton = $("#retryTurnButton");
          if (retryButton) retryButton.classList.remove("hidden");
          setBotState("Needs retry", false);
          setListeningStatus("Adeeb could not finish processing this turn. Your microphone is paused so the same answer is not recorded again. Click Retry answer.", "paused");
          toast(error.message);
        });
      }
    } else if (!processingTurn && autoListen && !botSpeaking) {
      setListeningStatus("Listening. Start speaking when you are ready.", "listening");
    }
  };

  activeRecorder.start(250);
  listening = true;
  $("#turnTimer").textContent = "00:00";
  clearInterval(turnTimerId);
  turnTimerId = window.setInterval(updateTurnTimer, 400);
  setListeningStatus("Listening. Speak clearly; Adeeb saves your answer after a natural pause.", "listening");
  setBotState("Listening", false);
  clearTimeout(noResponseTimerId);
  noResponseTimerId = window.setTimeout(() => {
    if (listening && !hasSpeech && !processingTurn && autoListen && !botSpeaking) {
      setListeningStatus("No answer detected for one minute. Moving to the next question…", "thinking");
      stopListening({ submit: false });
      moveToNextQuestion();
    }
  }, NO_RESPONSE_SKIP_MS);
  monitorVolume();
}

function monitorVolume() {
  if (!listening) return;
  const now = performance.now();
  let rms = 0;

  if (analyser && analyserData) {
    analyser.getByteTimeDomainData(analyserData);
    let sum = 0;
    for (const sample of analyserData) {
      const normalized = (sample - 128) / 128;
      sum += normalized * normalized;
    }
    rms = Math.sqrt(sum / analyserData.length);
  }

  // Calibrate the local background during the first quiet moment instead of relying
  // on one fixed threshold. This reduces false starts from fans and room noise.
  if (!hasSpeech && now - turnStartedAt < PRE_SPEECH_CALIBRATION_MS) {
    noiseSamples.push(rms);
    adaptiveNoiseFloor = Math.max(0.002, median(noiseSamples));
  }
  const threshold = Math.min(
    MAX_VOICE_THRESHOLD,
    Math.max(MIN_VOICE_THRESHOLD, adaptiveNoiseFloor * 3.2 + 0.006)
  );
  const isVoice = rms >= threshold;

  if (isVoice) {
    consecutiveVoiceFrames += 1;
    if (consecutiveVoiceFrames >= REQUIRED_VOICE_FRAMES) {
      hasSpeech = true;
      clearTimeout(noResponseTimerId);
      lastSpeechAt = now;
      setMicActivity(true);
      setListeningStatus("Listening to your response…", "listening");
    }
  } else {
    consecutiveVoiceFrames = 0;
    setMicActivity(false);
  }

  const elapsed = now - turnStartedAt;
  if (hasSpeech && elapsed >= MIN_CAPTURE_MS && now - lastSpeechAt >= currentSilenceWindow()) {
    stopListening({ submit: true });
    return;
  }
  if (elapsed >= MAX_TURN_MS) {
    stopListening({ submit: true });
    return;
  }
  monitoringFrame = requestAnimationFrame(monitorVolume);
}


function stopListening({ submit = false } = {}) {
  cancelAnimationFrame(monitoringFrame);
  monitoringFrame = null;
  clearInterval(turnTimerId);
  clearTimeout(noResponseTimerId);
  setMicActivity(false);

  if (!recorder || recorder.state === "inactive") {
    listening = false;
    return;
  }

  recorderShouldSubmit = Boolean(submit);
  listening = false;
  recorder.stop();
}

function buildAudioForm(blob, durationMs, mimeType) {
  const form = new FormData();
  form.append("spoken_language", $("#answerLanguage").value);
  form.append("audio_duration_ms", String(Math.max(0, durationMs || 0)));
  form.append("audio", blob, `voice-turn.${extensionForMime(mimeType || blob.type)}`);
  return form;
}

function applyLanguageFromResult(result) {
  const lang = result?.state?.candidate_language;
  if (lang && ["auto", "en", "ur", "hi"].includes(lang)) {
    $("#answerLanguage").value = lang;
    $("#languageBadge").textContent = lang.toUpperCase();
  }
}

async function handleAgentTurnResult(result) {
  processingTurn = false;
  const previousQuestionText = state?.question?.text || "";
  state = result.state || state;
  applyLanguageFromResult(result);
  // Keep the on-screen prompt in the same locked language as Adeeb's audio. The server
  // state stores canonical English, while spoken replies may be Urdu/Hindi. Clarification
  // and instruction turns do not replace the current interview question.
  const questionActions = new Set(["next", "repeat", "follow_up", "skill_follow_up", "role_specific_question", "clarify_answer", "project_depth_follow_up", "skill_evidence_follow_up"]);
  const keepCurrentQuestionActions = new Set(["clarification", "instruction"]);
  if (!result.completed && state?.question && result.action === "language" && result.question) {
    state.question.text = result.question;
  } else if (!result.completed && state?.question && result.bot_message && questionActions.has(result.action)) {
    state.question.text = result.bot_message;
  } else if (!result.completed && state?.question && previousQuestionText && keepCurrentQuestionActions.has(result.action)) {
    state.question.text = previousQuestionText;
  }
  renderState();

  if (result.candidate_text) {
    addCaption("You", result.candidate_text);
  } else if (result.transcription_status === "queued" || result.accepted) {
    addCaption("You", "Answer saved — the full transcript is processing safely in the background.");
  } else {
    addCaption("You", "Your audio was saved, but no readable transcript was returned. HR can review the recording.");
  }
  if (result.bot_message) addCaption("Adeeb", result.bot_message);

  if (result.completed) {
    speakBot(result.bot_message || state?.closing_message || "Thank you.", { listenAfter: false, onend: showCompleted });
    return;
  }

  autoListen = true;
  speakBot(result.bot_message || state?.question?.text || "Please continue.", { listenAfter: true });
}

async function queueAnswer(blob, durationMs, mimeType) {
  processingTurn = true;
  autoListen = false;

  const transcriptionMode = String(state?.question?.transcription_mode || "immediate");
  // A spoken command is normally short. Send short turns through the immediate LLM path
  // so “talk in Urdu”, “repeat”, and “next question” still work during the staged section.
  const likelyCommand = Number(durationMs || 0) <= 6500;
  const useBackgroundQueue = Boolean(
    state?.staged_interview_flow
    && state?.fast_answer_mode
    && transcriptionMode === "background"
    && !likelyCommand
  );
  const endpoint = useBackgroundQueue ? "queue-answer" : "turn";

  setListeningStatus(
    useBackgroundQueue
      ? "Your answer is being saved. Adeeb will continue while the transcript is prepared in the backend…"
      : "Adeeb is transcribing your words and the LLM brain is deciding the correct response…",
    "thinking",
  );
  setBotState(useBackgroundQueue ? "Saving" : "Thinking", false);

  const form = buildAudioForm(blob, durationMs, mimeType);
  const language = String($("#answerLanguage")?.value || state?.candidate_language || "en");
  const result = await api(`/api/interview/${sessionId}/${endpoint}`, {
    method: "POST",
    body: form,
    // Q3/Q4 may wait for the controlled background queue before the LLM creates the
    // next tailored question. Keep the browser patient and never restart listening early.
    timeoutMs: useBackgroundQueue ? 240000 : (language === "ur" ? 300000 : 180000),
  });
  lastFailedTurn = null;
  const retryButton = $("#retryTurnButton");
  if (retryButton) retryButton.classList.add("hidden");
  await handleAgentTurnResult(result);
}

async function retryLastAnswer() {
  if (!lastFailedTurn || processingTurn) return;
  unlockAudioPlayback();
  const retryButton = $("#retryTurnButton");
  if (retryButton) retryButton.classList.add("hidden");
  const pending = lastFailedTurn;
  try {
    await queueAnswer(pending.blob, pending.durationMs, pending.mimeType);
  } catch (error) {
    processingTurn = false;
    autoListen = false;
    lastFailedTurn = pending;
    if (retryButton) retryButton.classList.remove("hidden");
    setBotState("Needs retry", false);
    setListeningStatus("The saved answer still could not be processed. Check the server/internet, then click Retry answer again.", "paused");
    toast(error.message);
  }
}

async function moveToNextQuestion() {
  if (!state?.question || processingTurn) return;
  autoListen = false;
  stopListening({ submit: false });
  processingTurn = true;
  setListeningStatus("Moving to the next question…", "thinking");

  try {
    const result = await api(`/api/interview/${sessionId}/skip`, { method: "POST" });
    processingTurn = false;
    state = result.state || state;
    if (!result.completed && state?.question && result.bot_message) state.question.text = result.bot_message;
    renderState();

    if (result.bot_message) addCaption("Adeeb", result.bot_message);
    if (result.completed) {
      speakBot(result.bot_message || state?.closing_message || "Thank you.", { listenAfter: false, onend: showCompleted });
      return;
    }

    autoListen = true;
    speakBot(result.bot_message || state?.question?.text || "Please continue.", { listenAfter: true });
  } catch (error) {
    processingTurn = false;
    autoListen = true;
    toast(error.message);
    startListeningSoon();
  }
}

async function askAdeeb() {
  const input = $("#agentQuestionInput");
  const question = input.value.trim();
  if (!question) {
    toast("Type your question for Adeeb first.");
    return;
  }

  autoListen = false;
  stopListening({ submit: false });
  processingTurn = true;
  setListeningStatus("Adeeb is checking approved company information…", "thinking");

  try {
    addCaption("You", question);
    const result = await api(`/api/interview/${sessionId}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    input.value = "";
    processingTurn = false;
    state = result.state || state;
    renderState();
    addCaption("Adeeb", result.answer);
    autoListen = true;
    speakBot(result.answer, { listenAfter: true });
  } catch (error) {
    processingTurn = false;
    autoListen = true;
    toast(error.message);
    startListeningSoon();
  }
}

function showCompleted() {
  autoListen = false;
  stopListening({ submit: false });
  stopMedia();
  $("#meetingRoom").classList.add("hidden");
  $("#pausedCard").classList.add("hidden");
  $("#completeCard").classList.remove("hidden");
  $("#closingText").textContent = state?.closing_message || "Your interview has been submitted for human review.";
  $("#meetingStatus").textContent = "Meeting completed";
}

async function toggleCamera() {
  const preview = $("#cameraPreview");
  if (videoStream?.active) {
    videoStream.getTracks().forEach((track) => track.stop());
    videoStream = null;
    preview.srcObject = null;
    preview.classList.add("hidden");
    $("#candidateAvatar").classList.remove("hidden");
    $("#cameraToggle").classList.remove("active");
    return;
  }

  try {
    videoStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user" }, audio: false });
    preview.srcObject = videoStream;
    preview.classList.remove("hidden");
    $("#candidateAvatar").classList.add("hidden");
    $("#cameraToggle").classList.add("active");
    toast("Camera preview is on. Video is not recorded or uploaded by this app.");
  } catch (_) {
    toast("Camera could not be started. You can continue with audio only.");
  }
}

function pauseOrResumeMic() {
  if (autoListen) {
    autoListen = false;
    stopListening({ submit: false });
    $("#micToggle").classList.remove("active");
    setListeningStatus("Microphone paused. Click Mic to resume.", "paused");
    setBotState("Paused", false);
  } else {
    autoListen = true;
    $("#micToggle").classList.add("active");
    if (!botSpeaking && !processingTurn) startListeningSoon();
  }
}

function stopMedia() {
  if (audioStream) audioStream.getTracks().forEach((track) => track.stop());
  if (videoStream) videoStream.getTracks().forEach((track) => track.stop());
  audioStream = null;
  videoStream = null;
  if (audioContext) audioContext.close().catch(() => {});
  audioContext = null;
  analyser = null;
  analyserData = null;
}

function closeInterviewTab() {
  stopMedia();
  window.speechSynthesis?.cancel?.();
  window.close();
  window.setTimeout(() => {
    $("#closeTabHint")?.classList.remove("hidden");
    toast("Your browser blocked automatic tab closing. You can safely close this tab now.");
  }, 350);
}

function leaveMeeting() {
  if (!confirm("Leave this meeting now? Your completed answers remain saved and you can resume later from the same private link.")) return;
  autoListen = false;
  speechToken += 1;
  window.speechSynthesis?.cancel();
  stopListening({ submit: false });
  stopMedia();
  $("#meetingRoom").classList.add("hidden");
  $("#pausedCard").classList.remove("hidden");
  $("#meetingStatus").textContent = "Meeting paused — progress saved";
}

async function identifyAndStart() {
  const name = $("#candidateNameInput").value.trim();
  if (!name) throw new Error("Please enter your full name.");
  state = await api(`/api/interview/${sessionId}/identify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ candidate_name: name, quality_consent: $("#qualityConsent").checked }),
  });
  await api(`/api/interview/${sessionId}/start`, { method: "POST" });
}

async function joinInterview() {
  try {
    await identifyAndStart();
    await requestAudio();
    state = await api(`/api/interview/${sessionId}`);
    if (state.status === "completed") {
      showCompleted();
      return;
    }
    $("#consentCard").classList.add("hidden");
    $("#pausedCard").classList.add("hidden");
    $("#meetingRoom").classList.remove("hidden");
    meetingStartedAt = Date.now();
    clearInterval(meetingTimerId);
    meetingTimerId = window.setInterval(updateMeetingTimer, 1000);
    updateMeetingTimer();
    renderState();
    $("#meetingStatus").textContent = "Private interview in progress";
    autoListen = true;
    $("#micToggle").classList.add("active");
    speakWelcomeThenQuestion().catch(() => speakBot(state?.question?.text || "Please continue.", { listenAfter: true }));
  } catch (error) {
    toast(error.message.includes("secure") ? "Remote microphone access needs the secure HTTPS candidate link." : error.message);
  }
}

async function resumeInterview() {
  try {
    await requestAudio();
    await api(`/api/interview/${sessionId}/start`, { method: "POST" });
    state = await api(`/api/interview/${sessionId}`);
    $("#pausedCard").classList.add("hidden");
    $("#meetingRoom").classList.remove("hidden");
    autoListen = true;
    $("#micToggle").classList.add("active");
    renderState();
    let currentQuestion = state.question?.text || "Please continue.";
    try {
      const spoken = await api(`/api/interview/${sessionId}/spoken-current`);
      if (state?.question && spoken?.question) {
        state.question.text = spoken.question;
        currentQuestion = spoken.question;
        renderState();
      }
    } catch (_) {}
    speakBot(currentQuestion, { listenAfter: true });
  } catch (error) {
    toast(error.message);
  }
}

$("#consent").addEventListener("change", (event) => {
  $("#beginButton").disabled = !event.target.checked;
});
$("#beginButton").addEventListener("click", () => { unlockAudioPlayback(); joinInterview(); });
$("#resumeButton").addEventListener("click", () => { unlockAudioPlayback(); resumeInterview(); });
$("#micToggle").addEventListener("click", pauseOrResumeMic);
if ($("#cameraToggle")) $("#cameraToggle").addEventListener("click", toggleCamera);
$("#answerLanguage").addEventListener("change", async () => {
  const language = String($("#answerLanguage").value);
  $("#languageBadge").textContent = language.toUpperCase();
  if (!state?.session_id || !["en", "ur", "hi"].includes(language)) {
    renderState();
    return;
  }
  unlockAudioPlayback();
  autoListen = false;
  stopListening({ submit: false });
  setListeningStatus("Changing Adeeb's language…", "thinking");
  try {
    const result = await api(`/api/interview/${sessionId}/language`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language }),
    });
    state = result.state || state;
    if (state?.question && result.question) state.question.text = result.question;
    renderState();
    addCaption("Adeeb", result.bot_message);
    autoListen = true;
    speakBot(result.bot_message, { listenAfter: true });
  } catch (error) {
    autoListen = true;
    toast(error.message);
    renderState();
    startListeningSoon();
  }
});
$("#replayButton").addEventListener("click", () => {
  unlockAudioPlayback();
  if (!state?.question || processingTurn) return;
  autoListen = true;
  $("#micToggle").classList.add("active");
  speakBot(state.question.text, { listenAfter: true });
});
$("#nextButton").addEventListener("click", moveToNextQuestion);
if ($("#retryTurnButton")) $("#retryTurnButton").addEventListener("click", retryLastAnswer);
$("#askAgentButton").addEventListener("click", askAdeeb);
$("#agentQuestionInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    askAdeeb();
  }
});
if ($("#audioUnlockButton")) $("#audioUnlockButton").addEventListener("click", () => {
  unlockAudioPlayback();
  const pending = pendingSpeechRequest;
  pendingSpeechRequest = null;
  showAudioUnlockButton(false);
  if (pending) window.setTimeout(() => speakBot(pending.text, pending.options || {}), 120);
  else if (state?.question) window.setTimeout(() => speakBot(state.question.text, { listenAfter: true }), 120);
});
$("#leaveButton").addEventListener("click", leaveMeeting);
if ($("#closeTabButton")) $("#closeTabButton").addEventListener("click", closeInterviewTab);
async function updateConnectionStatus() {
  const el = $("#connectionStatus");
  if (!el) return;
  if (!navigator.onLine) {
    el.textContent = "Offline — reconnect to continue";
    el.dataset.online = "false";
    return;
  }
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch("/healthz", { cache: "no-store", signal: controller.signal });
    if (!response.ok) throw new Error("health check failed");
    el.textContent = "Connected to Adeeb";
    el.dataset.online = "true";
  } catch (_) {
    el.textContent = "Server or tunnel unavailable";
    el.dataset.online = "false";
  } finally {
    clearTimeout(timer);
  }
}
window.addEventListener("online", updateConnectionStatus);
window.addEventListener("offline", updateConnectionStatus);
updateConnectionStatus();
window.setInterval(updateConnectionStatus, 15000);
window.addEventListener("beforeunload", () => {
  agentAudio.pause();
  if (activeAudioObjectUrl) URL.revokeObjectURL(activeAudioObjectUrl);
  stopMedia();
});

// Pre-fill the name only for resumed/older sessions. New links keep it blank until the candidate types it.
(async () => {
  try {
    state = await api(`/api/interview/${sessionId}`);
    if (state.candidate_identified) {
      $("#candidateNameInput").value = state.candidate_name;
    }
    if (state.identity_registered) {
      $("#candidateNameInput").readOnly = true;
      $("#candidateNameInput").setAttribute("aria-readonly", "true");
      $("#candidateNameInput").closest("label").classList.add("verified-identity");
      $("#candidateNameInput").closest("label").insertAdjacentHTML("beforeend", '<span class="helper">Verified through the secure candidate registration form.</span>');
    }
    if (state.candidate_language && ["auto", "en", "ur", "hi"].includes(state.candidate_language)) {
      $("#answerLanguage").value = state.candidate_language;
    }
  } catch (_) {
    // The user can still try the secure link again when the local server is back online.
  }
})();
