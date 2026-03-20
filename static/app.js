const conversationEl = document.getElementById("conversation");
const textInputEl = document.getElementById("text-input");
const sendBtnEl = document.getElementById("send-btn");
const micBtnEl = document.getElementById("mic-btn");
const micIconEl = document.getElementById("mic-icon");
const voiceSubEl = document.getElementById("voice-sub");
const logEl = document.getElementById("log");
const latencyEl = document.getElementById("latency");
const statusDotEl = document.getElementById("status-dot");
const statusTextEl = document.getElementById("status-text");
const modelSelectEl = document.getElementById("model-select");
const connectBtnEl = document.getElementById("connect-btn");
const connectStatusEl = document.getElementById("connect-status");

let sessionId = null;
let isSending = false;
let isListening = false;
let recognition = null;
let _currentEventReader = null;
let _llmReplied = false;          // true once "processing" SSE event arrives
let _streamDone = Promise.resolve(); // resolves when the current stream fully completes
let _streamDoneResolver = null;
let micActive = false;            // true while mic mode is ON (survives processing/TTS)

// --- TTS queue ---
let _ttsQueue = [];
let _ttsSpeaking = false;

function cancelTTS() {
  _ttsQueue = [];
  _ttsSpeaking = false;
  if ("speechSynthesis" in window) window.speechSynthesis.cancel();
}

function enqueueSentence(text) {
  if (!("speechSynthesis" in window) || !text.trim()) return;
  _ttsQueue.push(text.trim());
  if (!_ttsSpeaking) _speakNext();
}

function _speakNext() {
  if (!_ttsQueue.length) {
    _ttsSpeaking = false;
    // TTS finished — resume listening if mic mode is still on
    if (micActive) startListening();
    return;
  }
  _ttsSpeaking = true;
  const u = new SpeechSynthesisUtterance(_ttsQueue.shift());
  u.rate = 1.02; u.pitch = 1.0; u.volume = 1.0;
  u.onend = u.onerror = () => _speakNext();
  window.speechSynthesis.speak(u);
}

function startListening() {
  if (!recognition || isListening || !micActive) return;
  try {
    recognition.start();
  } catch (_) {
    // recognition may already be starting; ignore
  }
}

// --- Sentence segmenter ---
let _sentenceBuffer = "";
const SENTENCE_END = /[.!?](?:\s|$)/;

function flushSentences(newChunk, force = false) {
  _sentenceBuffer += newChunk;
  const sentences = [];
  let remaining = _sentenceBuffer;
  let match;
  while ((match = SENTENCE_END.exec(remaining)) !== null) {
    sentences.push(remaining.slice(0, match.index + match[0].length).trim());
    remaining = remaining.slice(match.index + match[0].length);
  }
  _sentenceBuffer = remaining;
  if (force && remaining.trim()) {
    sentences.push(remaining.trim());
    _sentenceBuffer = "";
  }
  return sentences;
}

// --- Streaming bubble helpers ---
function addStreamingBubble() {
  const wrapper = document.createElement("div");
  wrapper.className = "message bot streaming";
  const meta = document.createElement("div");
  meta.className = "message-meta";
  meta.textContent = "Agent";
  const body = document.createElement("div");
  body.className = "message-body";
  wrapper.appendChild(meta);
  wrapper.appendChild(body);
  conversationEl.appendChild(wrapper);
  conversationEl.scrollTop = conversationEl.scrollHeight;
  return body;
}

function appendToBubble(bodyEl, text) {
  bodyEl.textContent += text;
  conversationEl.scrollTop = conversationEl.scrollHeight;
}

function finalizeStreamingBubble(bodyEl) {
  const wrapper = bodyEl.closest(".message");
  if (wrapper) wrapper.classList.remove("streaming");
}

function appendLog(message, isError = false) {
  const div = document.createElement("div");
  div.className = "log-entry" + (isError ? " error" : "");
  div.textContent = message;
  logEl.appendChild(div);
  logEl.scrollTop = logEl.scrollHeight;
}

function setStatus(text, active = false) {
  statusTextEl.textContent = text;
  statusDotEl.classList.toggle("idle", !active);
}

function setConnectStatus(connected) {
  connectStatusEl.textContent = connected ? "Connected" : "Not connected";
  connectBtnEl.textContent = connected ? "Logout" : "Connect calendar";
  connectBtnEl.classList.toggle("connected", connected);
}

async function refreshConnectStatus() {
  if (!sessionId) return;
  try {
    const resp = await fetch(`/api/connected?session_id=${encodeURIComponent(sessionId)}`);
    if (!resp.ok) return;
    const data = await resp.json();
    setConnectStatus(Boolean(data.connected));
  } catch (err) {
    // ignore
  }
}

async function loadModels() {
  try {
    const resp = await fetch("/api/models");
    if (!resp.ok) return;
    const data = await resp.json();
    const select = modelSelectEl;
    select.innerHTML = ""; // Clear existing
    // Add options for each category
    for (const [category, models] of Object.entries(data)) {
      if (category === "default_model") continue;
      models.forEach(model => {
        const option = document.createElement("option");
        option.value = model;
        option.textContent = `${model} (${category})`;
        if (model === data.default_model) {
          option.textContent += " (default)";
        }
        select.appendChild(option);
      });
    }
    // Set default
    select.value = data.default_model;
  } catch (err) {
    console.error("Error loading models:", err);
  }
}

function addMessage({ role, text }) {
  const wrapper = document.createElement("div");
  wrapper.className = "message " + (role === "user" ? "user" : "bot");

  const meta = document.createElement("div");
  meta.className = "message-meta";
  meta.textContent = role === "user" ? "You" : "Agent";
  wrapper.appendChild(meta);

  const body = document.createElement("div");
  body.textContent = text;
  wrapper.appendChild(body);

  conversationEl.appendChild(wrapper);
  conversationEl.scrollTop = conversationEl.scrollHeight;
}

async function callBackend(message) {
  if (!message.trim()) return;

  if (_currentEventReader) {
    if (!_llmReplied) {
      // LLM still thinking → cancel and let both messages be seen together.
      // The first user message is already in history; LLM will see both in sequence.
      try { await _currentEventReader.cancel(); } catch (_) {}
      _currentEventReader = null;
      cancelTTS();
      _sentenceBuffer = "";
    } else {
      // LLM has replied; calendar post-processing is running.
      // Wait for it to finish cleanly before sending the new message.
      appendLog("Waiting for current processing to finish…");
      setStatus("Queued…", true);
      await _streamDone;
    }
  }

  // Pause mic while processing (will auto-resume after TTS)
  if (isListening) recognition.stop();

  // Reset per-stream state
  _llmReplied = false;
  _streamDone = new Promise(resolve => { _streamDoneResolver = resolve; });

  cancelTTS();
  _sentenceBuffer = "";

  isSending = true;
  sendBtnEl.disabled = true;
  const started = performance.now();
  setStatus("Thinking…", true);

  const bubbleBody = addStreamingBubble();

  try {
    const resp = await fetch("/api/converse/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        session_id: sessionId,
        model: modelSelectEl ? modelSelectEl.value : undefined,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      }),
    });

    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const reader = resp.body.getReader();
    _currentEventReader = reader;
    const decoder = new TextDecoder();
    let sseBuffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      sseBuffer += decoder.decode(value, { stream: true });

      // SSE messages are separated by "\n\n"
      const parts = sseBuffer.split("\n\n");
      sseBuffer = parts.pop(); // keep incomplete last part

      for (const part of parts) {
        const dataLine = part.trim();
        if (!dataLine.startsWith("data:")) continue;
        let event;
        try { event = JSON.parse(dataLine.slice("data:".length).trim()); } catch { continue; }

        if (event.type === "chunk") {
          appendToBubble(bubbleBody, event.text);
          flushSentences(event.text).forEach(s => enqueueSentence(s));
          if (event.session_id && !sessionId) sessionId = event.session_id;
        } else if (event.type === "processing") {
          _llmReplied = true;
          setStatus("Processing…", true);
        } else if (event.type === "supplement") {
          // Post-LLM calendar ops produced a different reply
          flushSentences("", true).forEach(s => enqueueSentence(s));
          addMessage({ role: "bot", text: event.text });
          enqueueSentence(event.text);
        } else if (event.type === "done") {
          sessionId = event.session_id || sessionId;
          flushSentences("", true).forEach(s => enqueueSentence(s));
          finalizeStreamingBubble(bubbleBody);
          const duration = performance.now() - started;
          latencyEl.textContent = `Last round-trip: ${duration.toFixed(0)} ms`;
          appendLog(`Stream complete (session: ${sessionId})`);
        } else if (event.type === "error") {
          appendToBubble(bubbleBody, " [Error: " + event.message + "]");
          finalizeStreamingBubble(bubbleBody);
          appendLog("Backend error: " + event.message, true);
        }
      }
    }
  } catch (err) {
    if (err.name !== "AbortError") {
      console.error(err);
      appendLog("Error calling backend: " + err.message, true);
      appendToBubble(bubbleBody, "[Connection error]");
      finalizeStreamingBubble(bubbleBody);
    }
  } finally {
    _streamDoneResolver?.();   // unblock any queued callBackend waiting on _streamDone
    _streamDoneResolver = null;
    _currentEventReader = null;
    isSending = false;
    sendBtnEl.disabled = false;
    setStatus(isListening ? "Listening…" : "Idle", isListening);
  }
}

function setupSTT() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    voiceSubEl.textContent =
      "Browser speech recognition not available. You can still type below.";
    appendLog(
      "Web Speech API (SpeechRecognition) not available; voice disabled.",
      true,
    );
    micBtnEl.disabled = true;
    return;
  }

  recognition = new SpeechRecognition();
  recognition.continuous = false;
  recognition.interimResults = false;
  recognition.lang = "en-US";

  recognition.onstart = () => {
    isListening = true;
    micBtnEl.classList.add("listening");
    micBtnEl.classList.remove("idle");
    micIconEl.textContent = "●";
    setStatus("Listening…", true);
    appendLog("Listening for speech…");
  };

  recognition.onresult = (event) => {
    const transcript = event.results[0][0].transcript;
    appendLog("Heard: " + transcript);
    addMessage({ role: "user", text: transcript });
    callBackend(transcript);
  };

  recognition.onerror = (event) => {
    appendLog("Speech recognition error: " + event.error, true);
  };

  recognition.onend = () => {
    isListening = false;
    micBtnEl.classList.remove("listening");
    micBtnEl.classList.add("idle");
    micIconEl.textContent = "🎤";
    if (!micActive) {
      setStatus("Idle", false);
    }
    // If mic mode is on but we stopped (e.g. after speaking), _speakNext
    // will restart listening once TTS finishes. If no TTS queued, restart now.
    if (micActive && !_ttsSpeaking && !_ttsQueue.length) {
      startListening();
    }
  };

  appendLog("SpeechRecognition initialized.");
}

function toggleMic() {
  if (!recognition) return;
  if (micActive) {
    // Turn mic mode OFF
    micActive = false;
    recognition.stop();
    micBtnEl.classList.remove("listening");
    micBtnEl.classList.add("idle");
    micIconEl.textContent = "🎤";
    setStatus("Idle", false);
  } else {
    // Turn mic mode ON
    micActive = true;
    startListening();
  }
}

sendBtnEl.addEventListener("click", () => {
  const text = textInputEl.value.trim();
  if (!text) return;
  textInputEl.value = "";
  addMessage({ role: "user", text });
  callBackend(text);
});

textInputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendBtnEl.click();
  }
});

micBtnEl.addEventListener("click", () => {
  toggleMic();
});

function parseQueryParams() {
  return Object.fromEntries(new URLSearchParams(window.location.search));
}

connectBtnEl.addEventListener("click", async () => {
  const sid = sessionId || "session-1";
  const isConnected = connectBtnEl.classList.contains("connected");
  if (isConnected) {
    // Logout
    console.log("Logout button clicked");
    try {
      const resp = await fetch(`/api/logout?session_id=${encodeURIComponent(sid)}`, {
        method: "POST",
      });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      setConnectStatus(false);
      appendLog("Logged out successfully");
    } catch (err) {
      appendLog("Error logging out: " + err.message, true);
    }
  } else {
    // Connect
    console.log("Connect calendar button clicked");
    try {
      const resp = await fetch(`/api/auth_url?session_id=${encodeURIComponent(sid)}`);
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const data = await resp.json();
      // Navigate to Google's consent screen
      window.location.href = data.auth_url;
    } catch (err) {
      appendLog("Error starting OAuth: " + err.message, true);
    }
  }
});

window.addEventListener("load", () => {
  const params = parseQueryParams();
  if (params.session_id) {
    sessionId = params.session_id;
  }
  // Default to disconnected and then refresh to see if we are connected.
  setConnectStatus(false);
  if (params.connected) {
    setConnectStatus(true);
  }
  setupSTT();
  setStatus("Idle", false);
  appendLog("UI ready.");
  refreshConnectStatus();
  loadModels();
});

