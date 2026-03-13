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
      body: JSON.stringify({ message, session_id: sessionId }),
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

window.addEventListener("load", () => {
  setupSTT();
  setStatus("Idle", false);
  appendLog("UI ready.");
});

