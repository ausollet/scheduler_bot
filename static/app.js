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
  if (!message.trim() || isSending) return;
  isSending = true;
  sendBtnEl.disabled = true;
  const started = performance.now();
  setStatus("Thinking…", true);

  try {
    const resp = await fetch("/api/converse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        session_id: sessionId,
        model: modelSelectEl ? modelSelectEl.value : undefined,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      }),
    });

    const duration = performance.now() - started;
    latencyEl.textContent = `Last round-trip: ${duration.toFixed(0)} ms`;

    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }

    const data = await resp.json();
    sessionId = data.session_id || sessionId;
    appendLog(`Backend reply (session: ${sessionId})`);

    addMessage({ role: "bot", text: data.reply || "[empty reply]" });
    speakText(data.reply || "");
  } catch (err) {
    console.error(err);
    appendLog("Error calling backend: " + err.message, true);
  } finally {
    isSending = false;
    sendBtnEl.disabled = false;
    setStatus(isListening ? "Listening…" : "Idle", isListening);
  }
}

function speakText(text) {
  if (!("speechSynthesis" in window) || !text) return;
  try {
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1.02;
    utterance.pitch = 1.0;
    utterance.volume = 1.0;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(utterance);
  } catch (err) {
    appendLog("TTS error: " + err.message, true);
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
    setStatus("Idle", false);
  };

  appendLog("SpeechRecognition initialized.");
}

function toggleMic() {
  if (!recognition) return;
  if (isListening) {
    recognition.stop();
    return;
  }
  recognition.start();
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

