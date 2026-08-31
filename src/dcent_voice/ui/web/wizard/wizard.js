// DCENT_Voice — open-source, local-first voice dictation
// Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
// SPDX-License-Identifier: MIT
const api = {
  async call(name, ...args) {
    if (window.pywebview?.api?.[name]) {
      return window.pywebview.api[name](...args);
    }
    // Standalone-browser fallback so the page renders during development.
    if (name === "setup_state") {
      return {
        version: "dev",
        input_devices: [{ index: 0, name: "Default microphone" }],
        input_device: null,
        hotkeys: { dictation: "ctrl+win", command: "off", streaming: "off" },
        hotkey_mode: "hold",
        platform: "win32",
        active_profile: "desktop",
        has_local_asr_model: false,
        models: { faster_whisper: [], ollama: [], lmstudio: [] },
      };
    }
    if (name === "test_microphone") return { ok: true, peak: 0.4 };
    return {};
  }
};

let toastTimer = null;
function showToast(message, isError = false) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.style.borderColor = isError ? "rgba(239,68,68,.55)" : "rgba(255,110,0,.45)";
  toast.setAttribute("role", isError ? "alert" : "status");
  toast.setAttribute("aria-live", isError ? "assertive" : "polite");
  toast.classList.add("is-visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("is-visible"), 2200);
}

const HOTKEY_LABELS = { dictation: "Dictation", streaming: "Live Dictation", command: "Command" };
const LOCAL_ASR_PROVIDERS = new Set(["faster-whisper", "whisper-cpp", "parakeet"]);
const LOCAL_LLM_PROVIDERS = new Set(["ollama", "lmstudio", "none"]);
const DEFAULT_PROFILES = {
  desktop: { asr: "parakeet:tdt-0.6b-v3:int8", llm: "none", cleanup_enabled: false },
  auto: { asr: "faster-whisper:distil-small.en", llm: "none", cleanup_enabled: false },
  quality: { asr: "faster-whisper:distil-small.en:cpu-int8", llm: "none", cleanup_enabled: false },
  accurate: { asr: "faster-whisper:large-v3:cpu-int8", llm: "none", cleanup_enabled: false },
  gpu: { asr: "faster-whisper:distil-small.en:cuda-float16", llm: "none", cleanup_enabled: false },
  laptop: { asr: "faster-whisper:base.en:cpu-int8", llm: "ollama:qwen2.5:3b", cleanup_enabled: true },
  tiny: { asr: "faster-whisper:tiny.en:cpu-int8", llm: "none", cleanup_enabled: false },
  cloud: { asr: "deepgram:nova-3", llm: "none", cleanup_enabled: false },
};

function parseProviderSpec(spec) {
  const [provider = "", model = "", ...options] = String(spec || "").split(":");
  return {
    provider: provider.trim().toLowerCase(),
    model: model.trim(),
    option: options.join(":").trim().toLowerCase(),
  };
}

function resolveActiveProfile(state) {
  const name = String(state.active_profile || "desktop");
  const exposed = state.active_profile_config
    || (state.profile?.asr ? state.profile : null)
    || state.profiles?.[name];
  const profile = exposed || DEFAULT_PROFILES[name] || { asr: "", llm: "none", cleanup_enabled: false };
  return {
    name,
    asr: profile.asr || "",
    llm: profile.llm || "none",
    cleanup_enabled: Boolean(profile.cleanup_enabled),
    provenance: exposed
      ? "Read from the active configuration"
      : DEFAULT_PROFILES[name]
        ? "Estimated from the active profile name"
        : "Exact model details are not exposed by this app build",
  };
}

function configuredHardware(asr) {
  if (asr.provider !== "faster-whisper") {
    return { kind: "provider", label: "Provider managed", detail: "Hardware is managed by the transcription provider" };
  }
  if (asr.option.startsWith("cuda-")) {
    return { kind: "gpu", label: "NVIDIA GPU", detail: `${asr.option} explicitly forces CUDA` };
  }
  if (asr.option.startsWith("cpu-") || ["int8", "int16", "float32"].includes(asr.option)) {
    return {
      kind: "cpu",
      label: "CPU (works without a discrete GPU)",
      detail: `${asr.option || "CPU"} — recommended for laptops and PCs without CUDA`,
    };
  }
  return {
    kind: "auto",
    label: "Automatic",
    detail: "Uses a working NVIDIA stack when CUDA/cuDNN are ready; otherwise CPU int8",
  };
}

function clampScore(value) {
  return Math.max(0, Math.min(100, Math.round(value)));
}

function scoreLabel(score, labels) {
  if (score >= 88) return labels[0];
  if (score >= 72) return labels[1];
  if (score >= 55) return labels[2];
  if (score >= 38) return labels[3];
  return labels[4];
}

function describeProfile(profile) {
  const asr = parseProviderSpec(profile.asr);
  const llm = parseProviderSpec(profile.llm);
  const hardware = configuredHardware(asr);
  const model = asr.model.toLowerCase();
  let responsiveness = 62;
  let accuracy = 72;
  let efficiency = 58;
  if (model.includes("large")) { responsiveness = 42; accuracy = 96; efficiency = 24; }
  else if (model.includes("medium")) { responsiveness = 55; accuracy = 88; efficiency = 40; }
  else if (model.includes("distil") && model.includes("small")) { responsiveness = 83; accuracy = 78; efficiency = 72; }
  else if (model.includes("small")) { responsiveness = 70; accuracy = 82; efficiency = 62; }
  else if (model.includes("base")) { responsiveness = 82; accuracy = 70; efficiency = 80; }
  else if (model.includes("tiny")) { responsiveness = 94; accuracy = 55; efficiency = 92; }
  // CPU int8 is a first-class path: do not paint it as slow for distil/tiny.
  if (hardware.kind === "gpu") responsiveness += 10;
  if (hardware.kind === "cpu" && model.includes("large")) responsiveness -= 10;
  if (hardware.kind === "cpu" && model.includes("tiny")) responsiveness += 2;
  if (asr.option.includes("int8")) efficiency += 8;

  const cleanupOn = Boolean(profile.cleanup_enabled && llm.provider && llm.provider !== "none");
  const asrLocal = LOCAL_ASR_PROVIDERS.has(asr.provider);
  const llmLocal = !cleanupOn || LOCAL_LLM_PROVIDERS.has(llm.provider);
  const sovereignty = asrLocal && llmLocal
    ? { score: 100, label: "Fully local" }
    : asrLocal
      ? { score: 65, label: "Hybrid — transcript may leave" }
      : llmLocal
        ? { score: 25, label: "Cloud — audio may leave" }
        : { score: 10, label: "Cloud — audio and text may leave" };
  const responseScore = clampScore(responsiveness);
  const accuracyScore = clampScore(accuracy);
  const efficiencyScore = clampScore(efficiency);
  return {
    asr,
    llm,
    hardware,
    cleanupOn,
    axes: [
      { title: "Responsiveness", score: responseScore, label: scoreLabel(responseScore, ["Very fast", "Fast", "Balanced", "Deliberate", "Slow"]), provenance: "Estimated from the model and configured hardware" },
      { title: "Accuracy", score: accuracyScore, label: scoreLabel(accuracyScore, ["Highest", "High", "Balanced", "Basic", "Draft"]), provenance: "Estimated from the model family" },
      { title: "Efficiency", score: efficiencyScore, label: scoreLabel(efficiencyScore, ["Very light", "Light", "Moderate", "Heavy", "Very heavy"]), provenance: "Higher means lighter expected resource use" },
      { title: "Sovereignty", score: sovereignty.score, label: sovereignty.label, provenance: "Derived from transcription and enabled cleanup" },
    ],
  };
}

function axisMarkup(axis, index) {
  const id = `wizard-axis-${index}`;
  const valueText = `${axis.label}; ${axis.score} out of 100; ${axis.provenance}`;
  return `
    <li>
      <div class="axis-head"><span class="axis-label" id="${id}">${escapeHtml(axis.title)}</span><span class="axis-value"><strong>${escapeHtml(axis.label)}</strong> ${axis.score}/100</span></div>
      <div class="axis-meter" role="meter" aria-labelledby="${id}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${axis.score}" aria-valuetext="${escapeAttr(valueText)}"><i style="--axis-score:${axis.score}%"></i></div>
      <small>${escapeHtml(axis.provenance)}</small>
    </li>`;
}

function renderEngine(state) {
  const profile = resolveActiveProfile(state);
  const description = describeProfile(profile);
  const modelName = description.asr.model || "Model details unavailable";
  const cleanupCopy = !description.cleanupOn
    ? "AI cleanup is off, so dictation does not wait for an LLM."
    : LOCAL_LLM_PROVIDERS.has(description.llm.provider)
      ? `${description.llm.provider === "lmstudio" ? "LM Studio" : "Ollama"} must be running with ${description.llm.model} loaded; otherwise raw text is used.`
      : `Cleanup uses ${description.llm.provider}; transcript text may be sent with your consent.`;
  const hw = state.hardware || {};
  const runtimeLine = hw.summary
    ? `<p class="hardware-line runtime-path"><strong>Runtime path:</strong> ${escapeHtml(hw.summary)}</p>
       <p class="estimate-note">${escapeHtml(hw.recommendation || "Default desktop profile uses CPU int8 so machines without a high-end GPU still work.")}</p>`
    : `<p class="estimate-note">Default desktop profile uses CPU int8 — no discrete GPU required.</p>`;
  document.getElementById("engineCard").innerHTML = `
    <div class="engine-head">
      <div><span class="profile-name">${escapeHtml(profile.name)}</span><span class="engine-name">${escapeHtml(description.asr.provider || "Unknown provider")} · ${escapeHtml(modelName)}</span></div>
      <span class="badge-default">Active</span>
    </div>
    <p class="hardware-line"><strong>Configured hardware:</strong> ${escapeHtml(description.hardware.label)} — ${escapeHtml(description.hardware.detail)}</p>
    ${runtimeLine}
    <ul class="axes">${description.axes.map(axisMarkup).join("")}</ul>
    <p class="estimate-note">${escapeHtml(profile.provenance)}. Scores are estimates, not a benchmark of this computer.</p>
    <p class="cleanup-note"><strong>Optional cleanup:</strong> ${escapeHtml(cleanupCopy)}</p>`;
  renderModelReadiness(state, profile, description);
  renderDownloadNote(description);
}

function renderDownloadNote(description) {
  const note = document.getElementById("downloadNote");
  if (LOCAL_ASR_PROVIDERS.has(description.asr.provider)) {
    note.innerHTML = `<h3>Offline model storage</h3><p>The complete DCENT_Voice package includes the verified local speech model and its offline fallback. Dictation never downloads a speech model at runtime and never uploads microphone audio on a local profile. If either model is missing or corrupt, reinstall the complete package or provision the pinned offline bundle.</p><p>To choose a different balance of speed, accuracy, and resource use, open <strong>Settings → Models</strong>.</p>`;
  } else {
    note.innerHTML = `<h3>Cloud profile setup</h3><p>This transcription model is hosted by ${escapeHtml(description.asr.provider || "a cloud provider")} rather than stored on this computer. Connect the provider account and review consent in <strong>Settings → Accounts</strong> before use. Microphone audio may be sent to that provider.</p><p>Choose the desktop or another local profile in <strong>Settings → Models</strong> for offline transcription.</p>`;
  }
}

function renderModelReadiness(state, profile, description) {
  const status = document.getElementById("modelStatus");
  status.className = "status-band";
  if (!LOCAL_ASR_PROVIDERS.has(description.asr.provider)) {
    status.classList.add("warning");
    status.innerHTML = `<strong>Cloud transcription selected.</strong> Confirm the provider account and consent in Settings before dictating. Microphone audio may be sent to ${escapeHtml(description.asr.provider || "the provider")}.`;
    return;
  }
  const readiness = state.asr_readiness || {};
  const primary = readiness.primary || state.hardware?.model_readiness || {};
  const fallback = readiness.fallback || null;
  const primaryReady = primary.ready === true;
  const fallbackReady = fallback?.ready === true;
  const activeProvider = readiness.primary_provider || description.asr.provider;
  const isParakeet = activeProvider === "parakeet";
  if (isParakeet && primaryReady && fallbackReady) {
    status.classList.add("ok");
    status.innerHTML = `<strong>Offline speech is ready.</strong> The shipped Parakeet model and verified Faster Whisper base fallback are both available on this machine.`;
  } else if (isParakeet && primaryReady) {
    status.classList.add("warning");
    status.innerHTML = `<strong>Parakeet is ready; the offline fallback is missing.</strong> Dictation supported by Parakeet remains local, but reinstall the complete package before relying on fallback languages or recovery. Runtime downloads are disabled.`;
  } else if (isParakeet && fallbackReady) {
    status.classList.add("warning");
    status.innerHTML = `<strong>Using the verified offline fallback.</strong> Faster Whisper base is ready on this machine, but the shipped Parakeet model is missing or corrupt. Reinstall the complete package to restore the default path. Runtime downloads are disabled.`;
  } else if (primaryReady) {
    status.classList.add("ok");
    status.innerHTML = `<strong>Offline speech is ready.</strong> ${escapeHtml(description.asr.model || profile.name)} passed local model verification.`;
  } else {
    status.classList.add("warning");
    status.innerHTML = `<strong>No verified speech model is available.</strong> Reinstall the complete package or provision the pinned offline bundle before dictating. DCENT_Voice never downloads speech models at runtime.`;
  }
}

const KEY_DISPLAY = { ctrl: "Ctrl", win: "Win", cmd: "Cmd", alt: "Alt", shift: "Shift", esc: "Esc" };

function formatChord(chord) {
  return String(chord || "")
    .split("+")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => KEY_DISPLAY[part.toLowerCase()] || (part.length === 1 ? part.toUpperCase() : part[0].toUpperCase() + part.slice(1)))
    .join("+");
}

// The three facts a first-time user needs, mirroring the native dialog shown
// on hosts without WebView2 (src/dcent_voice/ui/first_run.py).
function renderFirstRun(state) {
  const chord = formatChord(state.hotkeys?.dictation || "ctrl+win") || "Ctrl+Win";
  const verb = state.hotkey_mode === "toggle" ? "Press" : "Hold";
  const platform = detectPlatform(state);
  const trayLine = platform === "windows"
    ? `The tray icon is in the notification area next to the clock. Windows may hide it under the <kbd>^</kbd> chevron; drag it to the taskbar to pin it.`
    : `The tray icon is in your system tray. Settings and this setup wizard open from that icon at any time.`;
  document.getElementById("firstRunFacts").innerHTML = [
    `<li>${verb} <kbd>${escapeHtml(chord)}</kbd> and speak. Release and the text lands where you were typing.</li>`,
    `<li>${trayLine}</li>`,
    `<li>Everything stays on this machine. Your voice is transcribed locally and nothing is uploaded.</li>`,
  ].join("");
  document.getElementById("dictateSteps").innerHTML =
    `${verb} <kbd>${escapeHtml(chord)}</kbd>, speak, release. Text lands where you were typing.`;
}

function detectPlatform(state) {
  const raw = String(
    state.platform || state.os || navigator.userAgentData?.platform || navigator.platform || navigator.userAgent || ""
  ).toLowerCase();
  if (raw.includes("win")) return "windows";
  if (raw.includes("mac") || raw.includes("darwin")) return "macos";
  if (raw.includes("linux") || raw.includes("x11") || raw.includes("wayland")) return "linux";
  return "unknown";
}

function renderPlatformReadiness(state) {
  const root = document.getElementById("platformReadiness");
  const platform = detectPlatform(state);
  if (platform === "windows") {
    root.innerHTML = `<h3>Windows checks</h3><ul><li>Allow microphone access for desktop apps in Windows Privacy &amp; security settings.</li><li>Dictation can type into normal apps at the same privilege level; a non-admin app cannot insert into an elevated Administrator window.</li><li>If a shortcut opens the Start menu or conflicts with another app, record a different shortcut in Settings.</li></ul>`;
  } else if (platform === "macos") {
    root.innerHTML = `<h3>macOS checks</h3><ul><li>Grant DCENT_Voice Microphone permission in System Settings → Privacy &amp; Security.</li><li>Grant Accessibility permission so the app can insert text into other applications.</li><li>If shortcuts do not respond after granting permission, restart DCENT_Voice once.</li></ul>`;
  } else if (platform === "linux") {
    root.innerHTML = `<h3>Linux desktop checks</h3><ul><li>Allow microphone access through your desktop or sandbox permission controls.</li><li>Wayland clipboard insertion needs <code>wl-copy</code> plus <code>wtype</code> or <code>ydotool</code>.</li><li>X11 clipboard insertion needs <code>xclip</code> or <code>xsel</code> plus <code>xdotool</code>. This wizard cannot verify those helper programs yet.</li></ul>`;
  } else {
    root.innerHTML = `<h3>Permission checks</h3><ul><li>Allow microphone access for DCENT_Voice.</li><li>Allow accessibility or input-control permission if your operating system requires it for inserting text.</li><li>Check Settings if shortcuts or text insertion do not work.</li></ul>`;
  }
}

async function boot() {
  const state = await api.call("setup_state");

  const select = document.getElementById("micSelect");
  const devices = state.input_devices || [];
  select.innerHTML =
    `<option value="">System default</option>` +
    devices.map((d) => `<option value="${d.index}">${escapeHtml(d.name)}</option>`).join("");
  if (state.input_device !== null && state.input_device !== undefined) {
    select.value = String(state.input_device);
  }
  const micReadiness = document.getElementById("micReadiness");
  if (devices.length) {
    micReadiness.className = "readiness-line ok";
    micReadiness.textContent = `${devices.length} microphone input${devices.length === 1 ? "" : "s"} detected. Test the selected device before finishing.`;
  } else {
    micReadiness.className = "readiness-line warning";
    micReadiness.textContent = "No microphone inputs were listed. Connect or enable a microphone, check operating-system permission, then test the system default.";
  }
  select.addEventListener("change", async () => {
    const value = select.value === "" ? null : Number(select.value);
    try {
      await api.call("set_input_device", value);
      showToast("Microphone saved");
    } catch (error) {
      showToast(`Could not save microphone — ${String(error?.message || error)}`, true);
    }
  });

  const keys = document.getElementById("hotkeys");
  keys.innerHTML = Object.entries(HOTKEY_LABELS)
    .map(([id, label]) => `<div><dt>${label}</dt><dd>${escapeHtml(state.hotkeys?.[id] || "")}</dd></div>`)
    .join("");
  renderFirstRun(state);
  renderEngine(state);
  renderPlatformReadiness(state);
}

document.getElementById("testMic").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const original = button.textContent;
  button.textContent = "Listening…";
  button.classList.add("is-busy");
  button.disabled = true;
  const status = document.getElementById("micStatus");
  const fill = document.getElementById("micFill");
  const meter = fill.parentElement;
  status.textContent = "Say a few words…";
  let result;
  try {
    result = await api.call("test_microphone", 1.5);
  } catch (error) {
    result = { ok: false, peak: 0, error: String(error?.message || error) };
  } finally {
    button.textContent = original;
    button.classList.remove("is-busy");
    button.disabled = false;
  }
  const peak = Math.max(0, Math.min(1, Number(result.peak) || 0));
  const percent = Math.round(peak * 100);
  fill.style.width = percent + "%";
  meter.setAttribute("aria-valuenow", String(percent));
  if (!result.ok) {
    status.textContent = "Could not open the microphone. Check the selected device and operating-system permission.";
    meter.setAttribute("aria-valuetext", "Microphone unavailable");
    showToast(result.error || "Could not open the microphone", true);
  } else if (peak > 0.02) {
    status.textContent = "Heard you clearly. ✓";
    meter.setAttribute("aria-valuetext", `Microphone test successful; peak ${percent} percent`);
    const readiness = document.getElementById("micReadiness");
    readiness.className = "readiness-line ok";
    readiness.textContent = "Microphone ready.";
  } else {
    status.textContent = "Very quiet — check the device or unmute the mic.";
    meter.setAttribute("aria-valuetext", `Microphone very quiet; peak ${percent} percent`);
  }
});

document.getElementById("finish").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.classList.add("is-busy");
  button.disabled = true;
  try {
    await api.call("finish_setup");
    showToast("You're all set");
    setTimeout(() => api.call("close_setup"), 700);
  } catch (error) {
    button.classList.remove("is-busy");
    button.disabled = false;
    showToast(`Setup was not saved — ${String(error?.message || error)}`, true);
  }
});

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function escapeAttr(value) {
  return escapeHtml(value);
}

// Wait for the pywebview bridge so the wizard shows the real devices/hotkeys,
// not the browser-preview stub — and never boot twice (duplicate listeners).
let booted = false;

function tryBoot() {
  if (!window.pywebview?.api) return false;
  if (!booted) { booted = true; boot(); }
  return true;
}

if (!tryBoot()) {
  window.addEventListener("pywebviewready", tryBoot);
  let waitedMs = 0;
  const poll = setInterval(() => {
    waitedMs += 250;
    if (tryBoot()) {
      clearInterval(poll);
    } else if (waitedMs >= 15000) {
      clearInterval(poll);
      if (!booted) { booted = true; boot(); } // plain-browser preview
    }
  }, 250);
}
