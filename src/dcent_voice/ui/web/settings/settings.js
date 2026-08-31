// DCENT_Voice — open-source, local-first voice dictation
// Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
// SPDX-License-Identifier: MIT
const fallbackConfig = {
  version: "dev",
  active_profile: "desktop",
  language: "en",
  language_mode: "english",
  cleanup_enabled: false,
  launch_at_startup: false,
  idle_unload_s: 600,
  hotkeys: { mode: "hold", dictation: "ctrl+win", command: "off", streaming: "off" },
  style: { default: "plain", per_app: {}, built_in: { "outlook.exe": "email", "Code.exe": "code", "Notion.exe": "notes" } },
  overlay: { enabled: true, lazy: true, position: "bottom-center", reduced_motion: false },
  service: { enabled: true, host: "127.0.0.1", port: 8765 },
  tts: { enabled: false, backend: "kokoro", mic_policy: "pause", duck_gain: 0.2, skip_code: true },
  recovery: { enabled: false, max_items: 10, max_age_hours: 24 },
  injector: { default: "clipboard", restore_clipboard: true, per_app: {} },
  profiles: {
    desktop: { asr: "parakeet:tdt-0.6b-v3:int8", llm: "none", cleanup_enabled: false },
    auto: { asr: "faster-whisper:distil-small.en", llm: "none", cleanup_enabled: false },
    quality: { asr: "faster-whisper:distil-small.en:cpu-int8", llm: "none", cleanup_enabled: false },
    accurate: { asr: "faster-whisper:large-v3:cpu-int8", llm: "none", cleanup_enabled: false },
    gpu: { asr: "faster-whisper:distil-small.en:cuda-float16", llm: "none", cleanup_enabled: false },
    laptop: { asr: "faster-whisper:base.en:cpu-int8", llm: "ollama:qwen2.5:3b", cleanup_enabled: true },
    tiny: { asr: "faster-whisper:tiny.en:cpu-int8", llm: "none", cleanup_enabled: false },
    cloud: { asr: "deepgram:nova-3", llm: "none", cleanup_enabled: false }
  },
  dictionary: [],
  snippets: [
    { spoken: "my email", expansion: "" },
    { spoken: "my calendar", expansion: "" },
    { spoken: "my signature", expansion: "" }
  ],
  dictation: { local_polish: true, spoken_edits: true, developer_terms: true, cleanup_level: "medium" }
};

let config = fallbackConfig;

// Calls that write config or credentials. Without the pywebview bridge these
// must hard-refuse: silently "saving" stub form values would overwrite the
// user's real config the moment the bridge attaches (RT-UX-2).
const mutatingCalls = new Set([
  "set_config", "set_input_device", "finish_setup",
  "connect_provider", "disconnect_provider",
  "begin_device_login", "poll_device_login",
  "grant_consent", "revoke_consent", "mark_first_run_education_shown",
  "install_tts_models", "import_snippets", "import_dictionary", "undo_snippet_import", "undo_dictionary_import",
  "set_recovery_policy", "delete_recovery_entry", "clear_recovery_entries", "copy_recovery_entry"
]);

const api = {
  async call(name, ...args) {
    if (window.pywebview?.api?.[name]) {
      const result = await window.pywebview.api[name](...args);
      if (name === "set_config") {
        if (result && result.snippet_undo) showSnippetImportUndo();
        else hideSnippetImportUndo();
        if (result && result.dictionary_undo) showDictionaryImportUndo();
        else hideDictionaryImportUndo();
      }
      return result;
    }
    if (mutatingCalls.has(name)) {
      showToast("Not connected to DCENT_Voice — nothing was saved", true);
      if (name === "set_config") return config;
      return { ok: false, status: "error", detail: "bridge unavailable" };
    }
    if (name === "get_config") return fallbackConfig;
    if (name === "list_providers") return [];
    if (name === "list_local_models") return { ollama: [], lmstudio: [], faster_whisper: [] };
    if (name === "tts_model_status") return {
      enabled: false,
      configured_backend: fallbackConfig.tts.backend,
      backends: [
        { backend: "kokoro", installed: false, runtime_ready: true, license: "Apache-2.0", asset_count: 2 }
      ]
    };
    if (name === "get_privacy_status") return { status: "sovereign", providers: [], missing_consents: [] };
    if (name === "get_recovery_status") return {
      enabled: false, entry_count: 0, entries: [], stores_audio: false, stores_successes: false,
      retention: { max_items: 10, max_age_hours: 24 }, integrity_ok: true, detail: ""
    };
    if (name === "get_egress_log") return [];
    if (name === "run_benchmark") return { stdout: "Bridge unavailable.", stderr: "", returncode: 0 };
    return {};
  }
};

let toastTimer = null;

function showToast(message, isError = false) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.classList.toggle("is-error", isError);
  toast.classList.add("is-visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("is-visible"), 2200);
}

async function withBusy(button, label, fn) {
  const original = button.textContent;
  const wasDisabled = button.disabled;
  button.textContent = label;
  button.classList.add("is-busy");
  // Disable while in flight so a double-click can't double-submit.
  button.disabled = true;
  try {
    return await fn();
  } finally {
    button.textContent = original;
    button.classList.remove("is-busy");
    button.disabled = wasDisabled;
  }
}

const tabs = [...document.querySelectorAll('[role="tab"]')];

function activateTab(button, { focus = false } = {}) {
  tabs.forEach((tab) => {
    const selected = tab === button;
    tab.classList.toggle("is-active", selected);
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    const page = document.getElementById(tab.dataset.page);
    page.classList.toggle("is-active", selected);
    page.hidden = !selected;
  });
  if (focus) button.focus();
}

tabs.forEach((button, index) => {
  button.addEventListener("click", () => activateTab(button));
  button.addEventListener("keydown", (event) => {
    let nextIndex = null;
    if (event.key === "ArrowDown" || event.key === "ArrowRight") nextIndex = index + 1;
    if (event.key === "ArrowUp" || event.key === "ArrowLeft") nextIndex = index - 1;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = tabs.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    activateTab(tabs[(nextIndex + tabs.length) % tabs.length], { focus: true });
  });
});

async function boot() {
  config = await api.call("get_config");
  renderGeneral();
  renderModels();
  await renderTtsModels();
  renderDictionary();
  if (config.snippet_undo) showSnippetImportUndo();
  if (config.dictionary_undo) showDictionaryImportUndo();
  await renderProviders();
  await renderPrivacy();
}

function renderGeneral() {
  document.getElementById("versionLine").textContent = `v${config.version}`;
  const profileSelect = document.getElementById("activeProfile");
  profileSelect.innerHTML = Object.keys(config.profiles)
    .map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`)
    .join("");
  profileSelect.value = config.active_profile;
  document.getElementById("languageMode").value = config.language_mode || "english";
  fillLanguageSelect(config.language || "en", config.language_choices);
  const langRow = document.getElementById("languageCodeRow");
  if (langRow) langRow.hidden = (config.language_mode || "english") === "english";
  document.getElementById("hotkeyMode").value = config.hotkeys.mode;
  document.getElementById("dictationHotkey").value = config.hotkeys.dictation;
  document.getElementById("commandHotkey").value = config.hotkeys.command || "off";
  document.getElementById("streamingHotkey").value = config.hotkeys.streaming || "off";
  const styleDefault = document.getElementById("styleDefault");
  if (styleDefault) styleDefault.value = (config.style && config.style.default) || "plain";
  const cleanupLevel = document.getElementById("dictationCleanupLevel");
  if (cleanupLevel) {
    cleanupLevel.value = (config.dictation && config.dictation.cleanup_level) || "medium";
  }
  document.getElementById("overlayPosition").value = config.overlay.position || "bottom-center";
  document.getElementById("serviceHost").value = config.service.host;
  document.getElementById("servicePort").value = config.service.port;
  document.getElementById("injectorDefault").value = config.injector.default;
  document.getElementById("overlayEnabled").checked = config.overlay.enabled;
  document.getElementById("overlayLazy").checked = config.overlay.lazy;
  document.getElementById("overlayReducedMotion").checked = config.overlay.reduced_motion;
  document.getElementById("serviceEnabled").checked = config.service.enabled;
  document.getElementById("restoreClipboard").checked = config.injector.restore_clipboard;
  document.getElementById("launchStartup").checked = config.launch_at_startup;
  const idleUnload = document.getElementById("idleUnloadS");
  if (idleUnload) {
    const seconds = String(Number(config.idle_unload_s ?? 600));
    idleUnload.value = ["0", "300", "600", "1800"].includes(seconds) ? seconds : "600";
  }
  document.getElementById("modeDictationHotkey").textContent = config.hotkeys.dictation;
  const heroHotkey = document.getElementById("heroDictationHotkey");
  if (heroHotkey) {
    heroHotkey.textContent = config.hotkeys.dictation || "Ctrl+Win";
  }
  document.getElementById("modeCommandHotkey").textContent = config.hotkeys.command;
  document.getElementById("modeStreamingHotkey").textContent = config.hotkeys.streaming;
  renderPerAppRows();
  renderStylePerAppRows();
  renderLocalCleanup();
}

function localCleanupStatusText(status) {
  const detected = status && status.detected;
  const requested = Boolean(status && status.requested);
  if (!requested) {
    return detected
      ? `On-device tone only. ${detected === "lmstudio" ? "LM Studio" : "Ollama"} is available if you want optional local cleanup.`
      : "On-device tone only. Optional local LLM is off. No Ollama or LM Studio detected.";
  }
  if (status.enabled) {
    const name = status.provider === "lmstudio" ? "LM Studio" : "Ollama";
    return `${name} ${status.model || ""} — if that model is down, dictation still uses on-device tone. Never cloud.`.replace(/\s+/g, " ").trim();
  }
  return "Cleanup is requested, but no local model is configured. Dictation stays on on-device tone until Ollama or LM Studio is running.";
}

async function renderLocalCleanup() {
  const toggle = document.getElementById("localCleanupToggle");
  const line = document.getElementById("localCleanupStatus");
  let status = (config && config.local_cleanup) || null;
  try {
    status = await api.call("local_cleanup_status");
    if (config) config.local_cleanup = status;
  } catch (_err) {
    const active = (config.profiles || {})[config.active_profile] || {};
    status = {
      requested: active.cleanup_enabled === true,
      enabled: false,
      detected: null,
      provider: "none",
      model: ""
    };
  }
  if (toggle) toggle.checked = Boolean(status && status.requested);
  if (line) line.textContent = localCleanupStatusText(status);
}

const LOCAL_ASR_PROVIDERS = new Set(["faster-whisper", "whisper-cpp", "parakeet"]);
const LOCAL_LLM_PROVIDERS = new Set(["ollama", "lmstudio", "none"]);

function parseProviderSpec(spec) {
  const [provider = "", model = "", ...options] = String(spec || "").split(":");
  return {
    provider: provider.trim().toLowerCase(),
    model: model.trim(),
    option: options.join(":").trim().toLowerCase(),
  };
}

function configuredHardware(asr) {
  if (asr.provider !== "faster-whisper") {
    return { kind: "provider", label: "Provider managed", provenance: "Determined by the transcription provider" };
  }
  if (asr.option.startsWith("cuda-")) {
    return { kind: "gpu", label: "NVIDIA GPU", provenance: `${asr.option} explicitly forces CUDA` };
  }
  if (asr.option.startsWith("cpu-") || ["int8", "int16", "float32"].includes(asr.option)) {
    return {
      kind: "cpu",
      label: "CPU (no high-end GPU required)",
      provenance: `${asr.option || "CPU"} — recommended default for laptops and desktops without CUDA`,
    };
  }
  return {
    kind: "auto",
    label: "Automatic",
    provenance: "Uses a working NVIDIA stack when CUDA/cuDNN are ready; otherwise CPU int8",
  };
}

function renderHardwareStatus() {
  const root = document.getElementById("hardwareStatus");
  if (!root) return;
  const hw = config.hardware || {};
  const device = String(hw.active_device || "cpu");
  const compute = String(hw.active_compute || "int8");
  const cudaReady = Boolean(hw.cuda_ready);
  const summary = hw.summary || `Active path: ${device} ${compute}`;
  const recommendation = hw.recommendation || "";
  const resolvedAsr = String(hw.resolved_asr || hw.active_asr || "unknown");
  const readiness = hw.model_readiness || null;
  const readinessLabel = !readiness
    ? "bundled"
    : (readiness.ready ? "verified and ready" : "missing or corrupt — reinstall complete package");
  root.hidden = false;
  root.className = `hardware-banner is-${device === "cuda" && cudaReady ? "gpu" : "cpu"}`;
  root.innerHTML = `
    <div class="hardware-banner-main">
      <strong>${escapeHtml(device === "cuda" ? "GPU path" : "CPU path")}</strong>
      <span>${escapeHtml(summary)}</span>
    </div>
    <p class="hardware-banner-detail">${escapeHtml(recommendation)}</p>
    <p class="hardware-banner-meta">Resolved speech model: <code>${escapeHtml(resolvedAsr)}</code>
      · <strong>${escapeHtml(readinessLabel)}</strong></p>
    <p class="hardware-banner-meta">CUDA stack ready: <strong>${cudaReady ? "yes" : "no"}</strong>
      · Default profile <code>desktop</code> uses <code>cpu-int8</code> so machines without a high-end GPU still work offline.</p>`;
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
  const model = asr.model.toLowerCase();
  const hardware = configuredHardware(asr);
  let responsiveness = 62;
  let accuracy = 72;
  let efficiency = 58;

  if (model.includes("large")) { responsiveness = 42; accuracy = 96; efficiency = 24; }
  else if (model.includes("medium")) { responsiveness = 55; accuracy = 88; efficiency = 40; }
  else if (model.includes("distil") && model.includes("small")) { responsiveness = 83; accuracy = 78; efficiency = 72; }
  else if (model.includes("small")) { responsiveness = 70; accuracy = 82; efficiency = 62; }
  else if (model.includes("base")) { responsiveness = 82; accuracy = 70; efficiency = 80; }
  else if (model.includes("tiny")) { responsiveness = 94; accuracy = 55; efficiency = 92; }

  if (hardware.kind === "gpu") responsiveness += 10;
  if (hardware.kind === "cpu") responsiveness -= model.includes("tiny") ? 3 : 8;
  if (asr.option.includes("int8")) efficiency += 5;

  const cleanupOn = Boolean(profile.cleanup_enabled && llm.provider && llm.provider !== "none");
  const asrLocal = LOCAL_ASR_PROVIDERS.has(asr.provider);
  const llmLocal = !cleanupOn || LOCAL_LLM_PROVIDERS.has(llm.provider);
  let sovereignty;
  if (asrLocal && llmLocal) {
    sovereignty = { score: 100, label: "Fully local" };
  } else if (asrLocal) {
    sovereignty = { score: 65, label: "Hybrid — text may leave" };
  } else if (llmLocal) {
    sovereignty = { score: 25, label: "Cloud — audio may leave" };
  } else {
    sovereignty = { score: 10, label: "Cloud — audio and text may leave" };
  }

  const responseScore = clampScore(responsiveness);
  const accuracyScore = clampScore(accuracy);
  const efficiencyScore = clampScore(efficiency);
  return {
    asr,
    llm,
    hardware,
    cleanupOn,
    axes: [
      {
        key: "responsiveness",
        title: "Responsiveness",
        score: responseScore,
        label: scoreLabel(responseScore, ["Very fast", "Fast", "Balanced", "Deliberate", "Slow"]),
        provenance: `Estimated from model family and configured ${hardware.label.toLowerCase()}`,
      },
      {
        key: "accuracy",
        title: "Accuracy",
        score: accuracyScore,
        label: scoreLabel(accuracyScore, ["Highest", "High", "Balanced", "Basic", "Draft"]),
        provenance: "Estimated from the transcription model family",
      },
      {
        key: "efficiency",
        title: "Efficiency",
        score: efficiencyScore,
        label: scoreLabel(efficiencyScore, ["Very light", "Light", "Moderate", "Heavy", "Very heavy"]),
        provenance: "Higher means lower expected memory and compute use",
      },
      {
        key: "sovereignty",
        title: "Sovereignty",
        score: sovereignty.score,
        label: sovereignty.label,
        provenance: "Derived from transcription plus enabled cleanup data flow",
      },
    ],
  };
}

function meterMarkup(axis, axisPrefix) {
  const labelId = `${axisPrefix}-${axis.key}`;
  const valueText = `${axis.label}; ${axis.score} out of 100; ${axis.provenance}`;
  return `
    <div class="tradeoff-axis">
      <div class="tradeoff-head">
        <span id="${labelId}" class="tradeoff-label">${escapeHtml(axis.title)}</span>
        <span class="tradeoff-value"><strong>${escapeHtml(axis.label)}</strong><b>${axis.score}/100</b></span>
      </div>
      <div class="tradeoff-meter" role="meter" aria-labelledby="${labelId}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${axis.score}" aria-valuetext="${escapeAttr(valueText)}">
        <i style="--axis-score:${axis.score}%"></i>
      </div>
      <small>${escapeHtml(axis.provenance)}</small>
    </div>`;
}

function cleanupReadinessMarkup(profile, description) {
  if (!description.cleanupOn) {
    return `<strong>Cleanup off.</strong> Dictation inserts the raw transcript without waiting for an LLM.`;
  }
  const provider = description.llm.provider === "lmstudio" ? "LM Studio" : description.llm.provider === "ollama" ? "Ollama" : description.llm.provider;
  if (LOCAL_LLM_PROVIDERS.has(description.llm.provider)) {
    return `<strong>Cleanup readiness:</strong> ${escapeHtml(provider)} must be running with <code>${escapeHtml(description.llm.model)}</code> loaded. Live health is checked when dictating; if unavailable, the raw transcript is used and the first fallback may add delay.`;
  }
  return `<strong>Cleanup readiness:</strong> ${escapeHtml(provider)} needs a connected account and consent. When enabled, transcript text is sent to that cloud provider.`;
}

function profileSummaryMarkup(name, profile, axisPrefix) {
  const description = describeProfile(profile);
  const active = name === config.active_profile;
  return `
    <div class="model-head">
      <span class="model-name">${escapeHtml(name)}</span>
      ${active ? '<span class="active-profile-badge">Active profile</span>' : ""}
    </div>
    <p class="hardware-summary"><strong>Configured hardware:</strong> ${escapeHtml(description.hardware.label)} <span>— ${escapeHtml(description.hardware.provenance)}</span></p>
    <div class="tradeoff-grid">${description.axes.map((axis) => meterMarkup(axis, axisPrefix)).join("")}</div>
    <p class="model-note">${cleanupReadinessMarkup(profile, description)}</p>`;
}

function rowProfile(row) {
  return {
    asr: row.querySelector('[data-field="asr"]').value,
    llm: row.querySelector('[data-field="llm"]').value,
    cleanup_enabled: row.querySelector('[data-field="cleanup_enabled"]').checked,
  };
}

function refreshModelRow(row) {
  row.classList.toggle("is-active-profile", row.dataset.profile === config.active_profile);
  row.querySelector(".model-summary").innerHTML = profileSummaryMarkup(
    row.dataset.profile,
    rowProfile(row),
    row.dataset.axisPrefix,
  );
}

function renderModels() {
  renderHardwareStatus();
  const root = document.getElementById("profileModels");
  root.innerHTML = Object.entries(config.profiles).map(([name, profile], index) => {
    const axisPrefix = `profile-axis-${index}`;
    return `
    <article class="row model-row ${name === config.active_profile ? "is-active-profile" : ""}" data-profile="${escapeAttr(name)}" data-axis-prefix="${axisPrefix}">
      <div class="model-summary">${profileSummaryMarkup(name, profile, axisPrefix)}</div>
      <div class="model-controls">
        <label class="switch"><input type="checkbox" data-field="cleanup_enabled" ${profile.cleanup_enabled ? "checked" : ""}><span>Enable optional AI cleanup for this profile</span></label>
      </div>
      <details class="model-advanced">
        <summary>Advanced model configuration</summary>
        <p>Prefer <code>:cpu-int8</code> (default) so any laptop works without a GPU. Legacy <code>:int8</code> is the same. Use <code>:cuda-float16</code> only when CUDA/cuDNN are installed; omit the suffix for automatic selection (CPU when CUDA is incomplete).</p>
        <div class="advanced-grid">
          <label><span>Transcription provider and model</span><input data-field="asr" value="${escapeAttr(profile.asr)}"></label>
          <label><span>AI cleanup provider and model</span><input data-field="llm" value="${escapeAttr(profile.llm)}"></label>
        </div>
      </details>
    </article>`;
  }).join("");
}

async function renderTtsModels() {
  const root = document.getElementById("ttsModels");
  const status = await api.call("tts_model_status");
  const backends = Array.isArray(status?.backends) ? status.backends : [];
  const configured = status?.configured_backend || config.tts?.backend || "kokoro";
  const enabled = Boolean(status?.enabled ?? config.tts?.enabled);
  root.innerHTML = `
    <article class="row model-row tts-model-card">
      <div class="model-summary">
        <div class="model-head"><span class="model-name">Local text-to-speech</span>${enabled ? '<span class="active-profile-badge">Enabled after restart</span>' : '<span class="mode-badge is-optional">Optional</span>'}</div>
        <p class="model-note">TTS models are never bundled. A source environment with a compatible local runtime can download checksum-pinned files from their listed upstream, record a <code>voice.model.download</code> egress event, and never upload microphone audio. The Windows public beta omits optional TTS runtimes while license compatibility is reviewed.</p>
      </div>
      <div class="model-controls tts-install-options">
        ${backends.map((backend) => {
          const display = "Kokoro";
          const selected = configured === backend.backend;
          const action = backend.installed && enabled && selected ? "Verify and enable" : `Install ${display}`;
          const runtime = backend.runtime_ready ? "runtime ready" : "runtime not installed";
          return `<div class="provider">
            <div><strong>${escapeHtml(display)}</strong><small>model license: ${escapeHtml(backend.license)} · ${Number(backend.asset_count) || 0} pinned files · ${backend.installed ? "verified assets found" : "not installed"} · ${escapeHtml(runtime)}</small></div>
            <button class="${selected ? "primary" : ""}" data-install-tts="${escapeAttr(backend.backend)}" ${backend.runtime_ready ? "" : "disabled"}>${escapeHtml(action)}</button>
          </div>`;
        }).join("") || '<p class="model-note">TTS status is unavailable until the DCENT_Voice bridge connects.</p>'}
      </div>
    </article>`;
}

document.getElementById("profileModels").addEventListener("input", (event) => {
  const row = event.target.closest(".model-row");
  if (row && event.target.matches("[data-field]")) refreshModelRow(row);
});

function renderDictionary() {
  const root = document.getElementById("dictionaryRows");
  const terms = config.dictionary || [];
  root.innerHTML = terms.map((entry, index) => dictionaryRow(entry.spoken, entry.written, index, entry.starred, entry.added_at)).join("");
  const snippetsRoot = document.getElementById("snippetRows");
  if (snippetsRoot) {
    const snippets = Array.isArray(config.snippets) ? config.snippets : [];
    snippetsRoot.innerHTML = snippets.map((entry, index) => snippetRow(entry.spoken, entry.expansion, index, entry.starred, entry.added_at)).join("");
  }
  const dictation = config.dictation || {};
  const polish = document.getElementById("dictationLocalPolish");
  const edits = document.getElementById("dictationSpokenEdits");
  const dev = document.getElementById("dictationDeveloperTerms");
  if (polish) polish.checked = dictation.local_polish !== false;
  if (edits) edits.checked = dictation.spoken_edits !== false;
  if (dev) dev.checked = dictation.developer_terms !== false;
  const level = document.getElementById("dictationCleanupLevel");
  if (level) level.value = dictation.cleanup_level || "medium";
  const pers = config.personalization || {};
  const persOn = document.getElementById("personalizationEnabled");
  const persLearn = document.getElementById("personalizationLearn");
  const persProse = document.getElementById("personalizationProseContext");
  if (persOn) persOn.checked = pers.enabled === true;
  if (persLearn) persLearn.checked = pers.learn === true;
  if (persProse) persProse.checked = pers.prose_context === true;
  renderLearnedTerms(config.learned || { terms: [], term_count: 0, app_styles: [] });
  applyListPrefs();
  filterDictionaryRows();
  sortDictionaryRows();
  filterSnippetRows();
  sortSnippetRows();
}

function applyListPrefs() {
  const lists = config.lists || {};
  const dict = document.getElementById("dictionarySort");
  const snip = document.getElementById("snippetSort");
  const dictOnly = document.getElementById("dictionaryStarredOnly");
  const snipOnly = document.getElementById("snippetStarredOnly");
  if (dict && lists.dictionary_sort) dict.value = lists.dictionary_sort;
  if (snip && lists.snippet_sort) snip.value = lists.snippet_sort;
  if (dictOnly) dictOnly.checked = lists.dictionary_starred_only === true;
  if (snipOnly) snipOnly.checked = lists.snippet_starred_only === true;
}

async function persistListPrefs() {
  try {
    config = await api.call("set_config", {
      lists: {
        dictionary_sort: document.getElementById("dictionarySort")?.value || "saved",
        snippet_sort: document.getElementById("snippetSort")?.value || "saved",
        dictionary_starred_only: document.getElementById("dictionaryStarredOnly")?.checked === true,
        snippet_starred_only: document.getElementById("snippetStarredOnly")?.checked === true
      }
    });
  } catch (err) {
    showToast(saveErrorMessage(err), true);
  }
}

function filterDictionaryRows() {
  const q = (document.getElementById("dictionaryFilter")?.value || "").trim().toLowerCase();
  const starredOnly = document.getElementById("dictionaryStarredOnly")?.checked;
  document.querySelectorAll("#dictionaryRows .row").forEach((row) => {
    const spoken = row.querySelector('[data-dict="spoken"]')?.value || "";
    const written = row.querySelector('[data-dict="written"]')?.value || "";
    const hit = (!q || spoken.toLowerCase().includes(q) || written.toLowerCase().includes(q))
      && (!starredOnly || dictionaryStarred(row));
    row.hidden = !hit;
  });
  updateListCount("dictionaryRows", "dictionaryCount", "No terms", "terms", dictionaryStarred, starredOnly);
}

function sortDictionaryRows() {
  const root = document.getElementById("dictionaryRows");
  if (!root) return;
  const mode = document.getElementById("dictionarySort")?.value || "saved";
  const rows = [...root.querySelectorAll(".row")];
  if (mode === "starred") {
    rows.sort((a, b) => {
      const delta = Number(dictionaryStarred(b)) - Number(dictionaryStarred(a));
      if (delta) return delta;
      const left = (a.querySelector('[data-dict="spoken"]')?.value || "").toLowerCase();
      const right = (b.querySelector('[data-dict="spoken"]')?.value || "").toLowerCase();
      return left.localeCompare(right);
    });
  } else if (mode === "newest" || mode === "oldest") {
    rows.sort((a, b) => compareAddedAt(a, b, mode === "newest"));
  } else if (mode === "az" || mode === "za") {
    rows.sort((a, b) => {
      const left = (a.querySelector('[data-dict="spoken"]')?.value || "").toLowerCase();
      const right = (b.querySelector('[data-dict="spoken"]')?.value || "").toLowerCase();
      const cmp = left.localeCompare(right);
      return mode === "za" ? -cmp : cmp;
    });
  } else {
    rows.sort((a, b) => Number(a.dataset.index || 0) - Number(b.dataset.index || 0));
  }
  rows.forEach((row) => root.appendChild(row));
}

function filterSnippetRows() {
  const q = (document.getElementById("snippetFilter")?.value || "").trim().toLowerCase();
  const starredOnly = document.getElementById("snippetStarredOnly")?.checked;
  document.querySelectorAll("#snippetRows .row").forEach((row) => {
    const spoken = row.querySelector('[data-snippet="spoken"]')?.value || "";
    const expansion = row.querySelector('[data-snippet="expansion"]')?.value || "";
    const hit = (!q || spoken.toLowerCase().includes(q) || expansion.toLowerCase().includes(q))
      && (!starredOnly || snippetStarred(row));
    row.hidden = !hit;
  });
  updateListCount("snippetRows", "snippetCount", "No snippets", "snippets", snippetStarred, starredOnly);
}

function exportEmptyToast(starredOnly, query) {
  if (String(query || "").trim()) return "Nothing visible to export";
  return starredOnly ? "Nothing starred to export" : "Nothing to export";
}

function importCancelledToast(kind) {
  return kind === "dictionary" ? "Dictionary import cancelled" : "Snippet import cancelled";
}

function importAppliedToast(kind, added, replaced, skipped, extra) {
  const lead = kind === "dictionary" ? "Dictionary imported" : "Snippets imported";
  return `${lead} ${added}, replaced ${replaced}, skipped ${skipped}${extra || ""}`;
}

function importReviewSummary(kind, added, replaced, skipped, extra) {
  const lead = kind === "dictionary" ? "Dictionary" : "Snippets";
  return `${lead}: import ${added}, replace ${replaced}, skip ${skipped}${extra || ""}. Nothing is saved until you apply.`;
}

function importEmptyReviewSummary(kind, skipped, extra) {
  const lead = kind === "dictionary" ? "Dictionary" : "Snippets";
  return `${lead}: Nothing will be imported. Skip ${skipped}${extra || ""}.`;
}

function importUndoneToast(kind) {
  return kind === "dictionary" ? "Dictionary import undone" : "Snippet import undone";
}

function importUndoButtonLabel(kind) {
  return kind === "dictionary" ? "Undo dictionary import" : "Undo snippet import";
}

function importUndoHelpLabel(kind) {
  return kind === "dictionary" ? "Undo dictionary import" : "Undo snippet import";
}

function dictionaryUndoStepsHelp() {
  return "Dictionary undo steps through each apply";
}

function snippetUndoStepsHelp() {
  return "Snippet undo steps through each apply";
}

function importApplyButtonLabel(kind) {
  return kind === "dictionary" ? "Apply dictionary import" : "Apply snippet import";
}

function importCancelButtonLabel(kind) {
  return kind === "dictionary" ? "Cancel dictionary import" : "Cancel snippet import";
}

function importDoneButtonLabel(kind) {
  return kind === "dictionary" ? "Done dictionary import" : "Done snippet import";
}

function importDoneHelpLabel(kind) {
  return kind === "dictionary" ? "Done dictionary import" : "Done snippet import";
}

function exportDownloadName(kind, query, starredOnly) {
  if (String(query || "").trim()) {
    return kind === "dictionary" ? "dcent-dictionary-visible.csv" : "dcent-snippets-visible.json";
  }
  if (starredOnly) {
    return kind === "dictionary" ? "dcent-dictionary-starred.csv" : "dcent-snippets-starred.json";
  }
  return kind === "dictionary" ? "dcent-dictionary.csv" : "dcent-snippets.json";
}

function exportDoneToast(kind, starredOnly, query) {
  if (String(query || "").trim()) {
    return kind === "dictionary" ? "Dictionary exported — visible" : "Snippets exported — visible";
  }
  if (kind === "dictionary") {
    return starredOnly ? "Dictionary exported — starred only" : "Dictionary exported — stars included";
  }
  return starredOnly ? "Snippets exported — starred only" : "Snippets exported";
}

function starredCountDetail(n) {
  return n ? `, ${n} starred` : "";
}

function starredOnlyEmptyLabel(unit) {
  return unit === "terms" ? "No starred terms" : "No starred snippets";
}

function updateListCount(rootId, countId, emptyLabel, unit, isStarred, starredOnly) {
  const rows = [...document.querySelectorAll(`#${rootId} .row`)];
  const visibleRows = rows.filter((row) => !row.hidden);
  const visible = visibleRows.length;
  const starred = isStarred ? visibleRows.filter((row) => isStarred(row)).length : 0;
  const starBit = starredCountDetail(starred);
  const el = document.getElementById(countId);
  if (!el) return;
  if (!rows.length) el.textContent = emptyLabel;
  else if (starredOnly && visible === 0) el.textContent = starredOnlyEmptyLabel(unit);
  else if (visible === rows.length) el.textContent = `${rows.length} ${unit}${starBit}`;
  else el.textContent = `${visible} of ${rows.length} ${unit}${starBit}`;
}

function removeVisibleNoun(kind, count) {
  if (kind === "dictionary") return Number(count) === 1 ? "term" : "terms";
  return Number(count) === 1 ? "snippet" : "snippets";
}

function removeVisibleSearchToast() {
  return "Search or Starred only, then remove visible";
}

function removeVisibleNeedsSearch(query, starredOnly) {
  return !String(query || "").trim() && !starredOnly;
}

function removeVisibleConfirm(visible, starred, noun) {
  return `Remove ${visible} visible ${noun}${starredCountDetail(starred)} from this machine?`;
}

function removeVisibleToast(visible, starred, noun) {
  return `Removed ${visible} visible ${noun}${starredCountDetail(starred)} — save to keep`;
}

function removeVisibleEmptyToast(starredOnly, kind) {
  if (starredOnly) {
    return kind === "dictionary"
      ? "Nothing starred terms to remove"
      : "Nothing starred snippets to remove";
  }
  return kind === "dictionary" ? "No terms to remove" : "No snippets to remove";
}

function removeVisibleRows(rootId, filterId, refresh, starredOnlyId, kind) {
  const q = (document.getElementById(filterId)?.value || "").trim();
  const starredOnly = document.getElementById(starredOnlyId)?.checked === true;
  if (removeVisibleNeedsSearch(q, starredOnly)) {
    showToast(removeVisibleSearchToast(), true);
    return;
  }
  const rows = [...document.querySelectorAll(`#${rootId} .row`)].filter((row) => !row.hidden);
  if (!rows.length) {
    showToast(removeVisibleEmptyToast(starredOnly, kind), true);
    return;
  }
  const isStarred = rootId === "snippetRows" ? snippetStarred : dictionaryStarred;
  const starred = rows.filter((row) => isStarred(row)).length;
  const noun = removeVisibleNoun(kind, rows.length);
  if (!confirm(removeVisibleConfirm(rows.length, starred, noun))) {
    return;
  }
  rows.forEach((row) => row.remove());
  refresh();
  showToast(removeVisibleToast(rows.length, starred, noun));
}

function sortSnippetRows() {
  const root = document.getElementById("snippetRows");
  if (!root) return;
  const mode = document.getElementById("snippetSort")?.value || "saved";
  const rows = [...root.querySelectorAll(".row")];
  if (mode === "starred") {
    rows.sort((a, b) => {
      const delta = Number(snippetStarred(b)) - Number(snippetStarred(a));
      if (delta) return delta;
      const left = (a.querySelector('[data-snippet="spoken"]')?.value || "").toLowerCase();
      const right = (b.querySelector('[data-snippet="spoken"]')?.value || "").toLowerCase();
      return left.localeCompare(right);
    });
  } else if (mode === "newest" || mode === "oldest") {
    rows.sort((a, b) => compareAddedAt(a, b, mode === "newest"));
  } else if (mode === "az" || mode === "za") {
    rows.sort((a, b) => {
      const left = (a.querySelector('[data-snippet="spoken"]')?.value || "").toLowerCase();
      const right = (b.querySelector('[data-snippet="spoken"]')?.value || "").toLowerCase();
      const cmp = left.localeCompare(right);
      return mode === "za" ? -cmp : cmp;
    });
  } else {
    rows.sort((a, b) => Number(a.dataset.index || 0) - Number(b.dataset.index || 0));
  }
  rows.forEach((row) => root.appendChild(row));
}

function renderLearnedTerms(learned) {
  const last = document.getElementById("lastUtterance");
  if (last) {
    last.textContent = learned && learned.has_last
      ? `Last dictation (memory only, no audio): ${learned.last_preview || ""}`
      : "No last dictation in memory.";
  }
  const rows = document.getElementById("learnedRows");
  const count = document.getElementById("learnedCount");
  const terms = (learned && learned.terms) || [];
  if (count) {
    count.textContent = terms.length
      ? `${terms.length} learned term${terms.length === 1 ? "" : "s"} — local file only, no audio.`
      : "No learned terms yet.";
  }
  if (rows) {
    rows.innerHTML = terms
      .map(
        (entry) => {
          const scope = [entry.style, entry.app].filter(Boolean).join(" · ") || "all apps";
          return `<div class="row"><label><span>Spoken</span><input value="${escapeAttr(entry.spoken)}" readonly></label><label><span>Written</span><input value="${escapeAttr(entry.written)}" readonly></label><span class="subtitle">${escapeHtml(String(entry.count || 1))}× · ${escapeHtml(scope)}</span></div>`;
        }
      )
      .join("");
  }
  const styleRows = document.getElementById("learnedStyleRows");
  const styleCount = document.getElementById("learnedStyleCount");
  const styles = (learned && learned.app_styles) || [];
  if (styleCount) {
    styleCount.textContent = styles.length
      ? `${styles.length} learned destination style${styles.length === 1 ? "" : "s"} — local file only, no audio.`
      : "No learned destination styles yet.";
  }
  if (styleRows) {
    styleRows.innerHTML = styles
      .map(
        (entry) =>
          `<div class="row"><label><span>App</span><input value="${escapeAttr(entry.app)}" readonly></label><label><span>Style</span><input value="${escapeAttr(entry.style)}" readonly></label><span class="subtitle">${escapeHtml(String(entry.count || 1))}× · ${escapeHtml(entry.source || "typed")}</span></div>`
      )
      .join("");
  }
}

function renderPerAppRows() {
  const root = document.getElementById("perAppRows");
  const entries = Object.entries(config.injector.per_app || {});
  root.innerHTML = entries.map(([process, injector]) => perAppRow(process, injector)).join("");
}

function renderStylePerAppRows() {
  const hint = document.getElementById("builtInStyleMap");
  const builtIn = (config.style && config.style.built_in) || {};
  if (hint) {
    const demo = [];
    for (const [app, style] of Object.entries(builtIn)) {
      const lower = app.toLowerCase();
      if (["outlook.exe", "slack.exe", "code.exe", "notion.exe", "obsidian.exe"].includes(lower)) {
        demo.push(`${app} → ${style}`);
      }
    }
    hint.textContent = demo.length
      ? `Built-in examples: ${demo.slice(0, 6).join(" · ")}`
      : "Built-in destination map is active for mail, chat, editors, and notes apps.";
  }
  const root = document.getElementById("stylePerAppRows");
  if (!root) return;
  const entries = Object.entries((config.style && config.style.per_app) || {});
  root.innerHTML = entries.map(([process, style]) => stylePerAppRow(process, style)).join("");
}

function stylePerAppRow(process = "", style = "plain") {
  const styles = ["plain", "email", "chat", "code", "formal", "notes"];
  const options = styles
    .map((name) => `<option value="${name}" ${style === name ? "selected" : ""}>${name}</option>`)
    .join("");
  return `
    <div class="row">
      <label><span>Process</span><input data-style-app="process" value="${escapeAttr(process)}" placeholder="outlook.exe"></label>
      <label><span>Style</span><select data-style-app="style">${options}</select></label>
      <button data-remove-row class="danger" type="button">Remove</button>
    </div>
  `;
}

function stampAddedAt() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

function compareAddedAt(a, b, newest) {
  const left = a.dataset.addedAt || "";
  const right = b.dataset.addedAt || "";
  if (!left && !right) return 0;
  if (!left) return 1;
  if (!right) return -1;
  const cmp = left < right ? -1 : left > right ? 1 : 0;
  return newest ? -cmp : cmp;
}

function dictionaryStarred(row) {
  return row.querySelector('[data-dict="starred"]')?.getAttribute("aria-pressed") === "true";
}

function formatAddedAt(addedAt) {
  const raw = String(addedAt || "").trim();
  if (!raw) return "No date";
  const day = raw.slice(0, 10);
  return /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : "No date";
}

function addedAtStamp(addedAt) {
  const day = formatAddedAt(addedAt);
  if (day === "No date") return `<time class="added-at">No date</time>`;
  return `<time class="added-at" datetime="${escapeAttr(addedAt)}">${day}</time>`;
}

function dictionaryStarAria(starred) {
  return starred
    ? "Starred — dictation priority on this machine"
    : "Star term for dictation priority";
}

function dictionaryRow(spoken = "", written = "", index = 0, starred = false, addedAt = "") {
  const pressed = starred ? "true" : "false";
  const label = starred ? "Starred" : "Star";
  return `
    <div class="row dictionary-row" data-index="${index}" data-added-at="${escapeAttr(addedAt || "")}">
      <div class="star-stack">
        <button type="button" data-dict="starred" class="ghost star-toggle" aria-pressed="${pressed}" aria-label="${escapeAttr(dictionaryStarAria(starred))}" title="${escapeAttr(dictionaryStarAria(starred))}">${label}</button>
        ${addedAtStamp(addedAt)}
      </div>
      <label><span>Spoken</span><input data-dict="spoken" maxlength="60" value="${escapeAttr(spoken)}"></label>
      <label><span>Written</span><textarea data-dict="written" maxlength="4000" rows="2">${escapeHtml(written)}</textarea></label>
      <button data-remove-row class="danger">Remove</button>
    </div>
  `;
}

function snippetPlaceholder(spoken) {
  const cue = (spoken || "").trim().toLowerCase();
  if (cue === "my email") return "you@example.com";
  if (cue === "my calendar") return "https://cal.example/me";
  if (cue === "my signature") return "Best regards,";
  return "https://cal.example/me";
}

function snippetStarred(row) {
  return row.querySelector('[data-snippet="starred"]')?.getAttribute("aria-pressed") === "true";
}

function snippetStarAria(starred) {
  return starred
    ? "Starred — dictation priority on this machine"
    : "Star snippet for dictation priority";
}

function snippetRow(spoken = "", expansion = "", index = 0, starred = false, addedAt = "") {
  const spokenPh = spoken ? "" : "my calendar";
  const expansionPh = snippetPlaceholder(spoken);
  const pressed = starred ? "true" : "false";
  const label = starred ? "Starred" : "Star";
  return `
    <div class="row snippet-row" data-index="${index}" data-added-at="${escapeAttr(addedAt || "")}">
      <div class="star-stack">
        <button type="button" data-snippet="starred" class="ghost star-toggle" aria-pressed="${pressed}" aria-label="${escapeAttr(snippetStarAria(starred))}" title="${escapeAttr(snippetStarAria(starred))}">${label}</button>
        ${addedAtStamp(addedAt)}
      </div>
      <label><span>Say this</span><input data-snippet="spoken" value="${escapeAttr(spoken)}" placeholder="${escapeAttr(spokenPh || spoken || "my calendar")}"></label>
      <label class="span-2"><span>Insert this</span><textarea data-snippet="expansion" rows="2" placeholder="${escapeAttr(expansionPh)}">${escapeHtml(expansion)}</textarea></label>
      <button data-remove-row class="danger">Remove</button>
    </div>
  `;
}

function perAppRow(process = "", injector = "keystroke") {
  return `
    <div class="row">
      <label><span>Process</span><input data-app="process" value="${escapeAttr(process)}"></label>
      <label><span>Injector</span><select data-app="injector"><option value="clipboard" ${injector === "clipboard" ? "selected" : ""}>Clipboard</option><option value="keystroke" ${injector === "keystroke" ? "selected" : ""}>Keystroke</option></select></label>
      <button data-remove-row class="danger">Remove</button>
    </div>
  `;
}

async function renderProviders() {
  const root = document.getElementById("providerList");
  const providers = await api.call("list_providers");
  root.innerHTML = providers.map((provider) => {
    const modes = provider.auth_modes || [];
    // Local providers have no account to manage: their docs link is
    // documentation, not a key portal, and "Disconnect" would be meaningless.
    const hasAccount = modes.includes("api_key") || modes.includes("device_code");
    const links = [];
    if (provider.docs_url) {
      const label = hasAccount ? "Get your key ↗" : "Docs ↗";
      links.push(`<a data-open="${escapeAttr(provider.docs_url)}" href="#">${label}</a>`);
    }
    if (provider.policy_url) links.push(`<a data-open="${escapeAttr(provider.policy_url)}" href="#">Privacy ↗</a>`);
    const connected = provider.account?.connected;
    let action;
    if (connected && hasAccount) {
      action = `<span class="chip on">${escapeHtml(provider.account?.label || "Connected")}</span>
                <button class="danger" data-connect="${escapeAttr(provider.name)}" data-role="disconnect">Disconnect</button>`;
    } else if (hasAccount) {
      const parts = [];
      if (modes.includes("device_code") && provider.device_login_available) {
        parts.push(`<button class="primary" data-signin="${escapeAttr(provider.name)}">Sign in</button>`);
      }
      if (modes.includes("api_key")) {
        parts.push(`<input type="password" placeholder="API key" data-key="${escapeAttr(provider.name)}">`);
        parts.push(`<button data-connect="${escapeAttr(provider.name)}">Use key</button>`);
      }
      action = parts.join(" ");
    } else {
      action = `<span class="chip ${connected ? "on" : ""}">${connected ? "Running" : "Not detected"}</span>`;
    }
    return `
    <div class="provider">
      <div>
        <strong>${escapeHtml(provider.display_name)}</strong>
        <small>${escapeHtml(provider.data_note || "")}</small>
        <span class="badge ${provider.locality}">${provider.locality}</span>
        ${links.length ? `<div class="provider-links">${links.join("")}</div>` : ""}
      </div>
      <div>${action}</div>
    </div>`;
  }).join("");
}

document.getElementById("providerList").addEventListener("click", (event) => {
  const link = event.target.closest("[data-open]");
  if (link) {
    event.preventDefault();
    api.call("open_url", link.dataset.open);
  }
});

document.getElementById("providerList").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-signin]");
  if (!button || button.disabled) return;
  const provider = button.dataset.signin;
  button.disabled = true;
  const start = await withBusy(button, "Starting…", () =>
    api.call("begin_device_login", provider, true));
  if (!start || !start.ok) {
    button.disabled = false;
    showToast((start && start.detail) || "Account sign-in is unavailable", true);
    return;
  }
  if (start.verification_uri) api.call("open_url", start.verification_uri);
  showToast(`Enter code ${start.user_code} in your browser to finish`, false);
  button.classList.add("is-busy");
  button.textContent = "Waiting for sign-in…";
  const period = Math.max(2, Number(start.interval) || 5) * 1000;
  const timer = setInterval(async () => {
    if (!button.isConnected) {
      // The list re-rendered under us (Refresh); stop polling for the
      // detached button instead of leaking the interval.
      clearInterval(timer);
      return;
    }
    const result = await api.call("poll_device_login", provider);
    if (result.status === "connected") {
      clearInterval(timer);
      showToast(`Signed in to ${provider}`);
      await renderProviders();
      await renderPrivacy();
    } else if (result.status === "error") {
      clearInterval(timer);
      button.classList.remove("is-busy");
      button.textContent = "Sign in";
      button.disabled = false;
      showToast(result.detail || "Sign-in failed", true);
    }
  }, period);
});

async function renderPrivacy() {
  const education = await api.call("get_first_run_education");
  document.getElementById("firstRunEducation").textContent = education.copy || "";
  const status = await api.call("get_privacy_status");
  const statusCopy = {
    sovereign: "Sovereign — everything is processed on this machine.",
    hybrid: "Hybrid — some consented data goes to cloud providers.",
    cloud: "Cloud — transcripts are processed by cloud providers you approved."
  };
  document.getElementById("privacyStatus").textContent =
    statusCopy[status.status] || `Status: ${status.status}`;
  document.getElementById("privacyProviders").innerHTML = status.providers.map((provider) => `
    <div class="provider">
      <div>
        <strong>${escapeHtml(provider.key)}</strong>
        <small>${escapeHtml(provider.payload_type)} / ${escapeHtml(provider.locality)}</small>
      </div>
      <div>
        ${provider.locality === "cloud" ? `<button data-consent="${escapeAttr(provider.key)}">${status.missing_consents.includes(provider.key) ? "Grant consent" : "Revoke consent"}</button>` : ""}
      </div>
    </div>
  `).join("");
  const log = await api.call("get_egress_log", 50);
  document.getElementById("egressLog").textContent = log.length
    ? JSON.stringify(log, null, 2)
    : "";
  await renderRecovery();
}

async function renderRecovery(state = null) {
  const status = state || await api.call("get_recovery_status");
  const retention = status.retention || config.recovery || { max_items: 10, max_age_hours: 24 };
  const toggle = document.getElementById("recoveryEnabled");
  toggle.checked = status.enabled === true;
  document.getElementById("recoveryMaxItems").value = String(retention.max_items || 10);
  document.getElementById("recoveryMaxAge").value = String(retention.max_age_hours || 24);
  const detail = document.getElementById("recoveryStatus");
  if (!status.integrity_ok) {
    detail.textContent = status.detail || "Recovery vault is unavailable; no new text is being retained.";
  } else if (!status.enabled) {
    detail.textContent = "Off — failed text is not retained.";
  } else {
    detail.textContent = `${status.entry_count || 0} failed dictation item${status.entry_count === 1 ? "" : "s"} retained locally.`;
  }
  document.getElementById("clearRecovery").disabled = !(
    status.entry_count > 0 || status.integrity_ok === false
  );
  const entries = Array.isArray(status.entries) ? status.entries : [];
  document.getElementById("recoveryList").innerHTML = entries.length ? entries.map((entry) => {
    const created = new Date(entry.created_at);
    const when = Number.isNaN(created.getTime()) ? "Retained locally" : created.toLocaleString();
    return `<article class="recovery-item">
      <div class="recovery-meta"><span>${escapeHtml(when)}</span><span>${escapeHtml(entry.mode || "dictation")} · ${escapeHtml(entry.reason || "insertion failed")}</span></div>
      <pre>${escapeHtml(entry.text || "")}</pre>
      <div class="recovery-actions">
        <button type="button" data-recovery-copy="${escapeAttr(entry.id)}">Copy</button>
        <button type="button" class="ghost" data-recovery-delete="${escapeAttr(entry.id)}">Delete</button>
      </div>
    </article>`;
  }).join("") : '<p class="model-note">No failed dictations are retained.</p>';
}

async function saveRecoveryPolicy(enabled) {
  const maxItems = Number(document.getElementById("recoveryMaxItems").value || 10);
  const maxAge = Number(document.getElementById("recoveryMaxAge").value || 24);
  const state = await api.call("set_recovery_policy", enabled === true, maxItems, maxAge);
  config.recovery = { enabled: state.enabled === true, max_items: maxItems, max_age_hours: maxAge };
  await renderRecovery(state);
  if (state.enabled) {
    showToast("Failed-dictation recovery enabled");
  } else if (state.integrity_ok === false) {
    showToast(state.detail || "Recovery disabled, but retained text could not be purged", true);
  } else {
    showToast("Recovery disabled and retained text purged");
  }
}

const localCleanupToggle = document.getElementById("localCleanupToggle");
if (localCleanupToggle) {
  localCleanupToggle.addEventListener("change", async () => {
    try {
      const next = await api.call("set_local_cleanup", localCleanupToggle.checked);
      config = next;
      renderLocalCleanup();
      renderModels();
      showToast(
        localCleanupToggle.checked
          ? "Local AI cleanup will run when Ollama or LM Studio is up"
          : "Local AI cleanup off — on-device tone still runs"
      );
    } catch (err) {
      localCleanupToggle.checked = !localCleanupToggle.checked;
      showToast(saveErrorMessage(err), true);
    }
  });
}

document.getElementById("saveGeneral").addEventListener("click", async () => {
  const before = config.service || {};
  const serviceChanged =
    before.enabled !== document.getElementById("serviceEnabled").checked ||
    String(before.host ?? "") !== document.getElementById("serviceHost").value ||
    Number(before.port ?? 0) !== Number(document.getElementById("servicePort").value);
  try {
    config = await api.call("set_config", {
    active_profile: document.getElementById("activeProfile").value,
    language_mode: document.getElementById("languageMode").value,
    language: languageFromMode(),
    launch_at_startup: document.getElementById("launchStartup").checked,
    idle_unload_s: Number((document.getElementById("idleUnloadS") || {}).value || 600),
    hotkeys: {
      mode: document.getElementById("hotkeyMode").value,
      dictation: document.getElementById("dictationHotkey").value,
      command: document.getElementById("commandHotkey").value,
      streaming: document.getElementById("streamingHotkey").value
    },
    service: {
      enabled: document.getElementById("serviceEnabled").checked,
      host: document.getElementById("serviceHost").value,
      port: Number(document.getElementById("servicePort").value)
    },
    injector: {
      default: document.getElementById("injectorDefault").value,
      restore_clipboard: document.getElementById("restoreClipboard").checked,
      per_app: collectPerAppRows()
    },
    style: {
      default: document.getElementById("styleDefault").value,
      per_app: collectStylePerAppRows()
    },
    dictation: {
      cleanup_level: (document.getElementById("dictationCleanupLevel") || {}).value || "medium"
    },
    overlay: {
      enabled: document.getElementById("overlayEnabled").checked,
      lazy: document.getElementById("overlayLazy").checked,
      position: document.getElementById("overlayPosition").value,
      reduced_motion: document.getElementById("overlayReducedMotion").checked
    }
    });
  } catch (err) {
    showToast(saveErrorMessage(err), true);
    return;
  }
  renderGeneral();
  renderModels();
  // The local API thread binds host/port at launch and isn't hot-swapped.
  showToast(
    serviceChanged
      ? "Saved — Local API changes apply after restarting DCENT_Voice"
      : "General settings saved"
  );
});

// A rejected save (the app validates the whole config before writing) surfaces
// its reason here instead of silently doing nothing.
function saveErrorMessage(err) {
  const raw = String(err?.message || err || "");
  const detail = raw.replace(/^.*ConfigError:?\s*/i, "").trim();
  return detail ? `Not saved — ${detail}` : "Not saved — invalid settings";
}

document.querySelectorAll("[data-record-hotkey]").forEach((button) => {
  button.addEventListener("click", async () => {
    const target = document.getElementById(button.dataset.recordHotkey);
    const result = await withBusy(button, "Listening…", () => api.call("record_hotkey", 5));
    if (result.ok) {
      target.value = result.chord;
      showToast(`Captured ${result.chord}`);
    } else {
      showToast("No hotkey captured", true);
    }
  });
});

document.getElementById("refreshModels").addEventListener("click", async () => {
  const models = await api.call("list_local_models");
  document.getElementById("modelOutput").textContent = JSON.stringify(models, null, 2);
  await renderTtsModels();
});

document.getElementById("saveModels").addEventListener("click", async () => {
  const profiles = {};
  document.querySelectorAll("#profileModels .row").forEach((row) => {
    const name = row.dataset.profile;
    profiles[name] = {
      asr: row.querySelector('[data-field="asr"]').value,
      llm: row.querySelector('[data-field="llm"]').value,
      cleanup_enabled: row.querySelector('[data-field="cleanup_enabled"]').checked,
      language: config.profiles[name].language || config.language
    };
  });
  try {
    config = await api.call("set_config", { profile: profiles });
  } catch (err) {
    showToast(saveErrorMessage(err), true);
    return;
  }
  renderModels();
  showToast("Model profiles saved");
});

document.getElementById("ttsModels").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-install-tts]");
  if (!button) return;
  const backend = button.dataset.installTts;
  const accepted = window.confirm(
    `Install ${backend} TTS model files? This downloads checksum-pinned files from the backend's upstream, records model-download egress in DCENT_Voice privacy logs, enables local TTS after restart, and never uploads microphone audio.`,
  );
  if (!accepted) return;
  const result = await withBusy(button, "Installing…", () =>
    api.call("install_tts_models", backend, true));
  if (!result?.ok) {
    showToast(result?.detail || "TTS model installation failed", true);
    return;
  }
  config = result.config || await api.call("get_config");
  await renderTtsModels();
  await renderPrivacy();
  showToast(`Installed ${result.backend}; restart DCENT_Voice to enable TTS`);
});

document.getElementById("runBenchmark").addEventListener("click", async (event) => {
  const result = await withBusy(event.currentTarget, "Benchmarking…", () => api.call("run_benchmark"));
  document.getElementById("modelOutput").textContent = `${result.stdout}\n${result.stderr}`.trim();
});

document.getElementById("refreshProviders").addEventListener("click", renderProviders);

document.getElementById("checkUpdate").addEventListener("click", async (event) => {
  const status = document.getElementById("updateStatus");
  status.textContent = "";
  const result = await withBusy(event.currentTarget, "Checking…", () =>
    api.call("check_for_update"));
  if (!result || result.ok === false) {
    status.textContent = "Could not check right now.";
    return;
  }
  if (result.available) {
    status.innerHTML =
      `v${escapeHtml(result.latest)} is available — ` +
      `<a data-open="${escapeAttr(result.url)}" href="#">release notes ↗</a>`;
  } else {
    status.textContent = `You're up to date (v${result.current}).`;
  }
});

document.getElementById("updateStatus").addEventListener("click", (event) => {
  const link = event.target.closest("[data-open]");
  if (link) {
    event.preventDefault();
    api.call("open_url", link.dataset.open);
  }
});
document.getElementById("refreshPrivacy").addEventListener("click", renderPrivacy);
document.getElementById("recoveryEnabled").addEventListener("change", async (event) => {
  const enabling = event.target.checked === true;
  const prompt = enabling
    ? "Enable local failed-dictation recovery? Only text that could not be inserted will be stored, with the limits shown. Successful dictation and audio are never retained."
    : "Disable recovery and permanently delete every retained failed dictation?";
  if (!window.confirm(prompt)) {
    event.target.checked = !enabling;
    return;
  }
  await saveRecoveryPolicy(enabling);
});
document.getElementById("applyRecoveryPolicy").addEventListener("click", async () => {
  await saveRecoveryPolicy(document.getElementById("recoveryEnabled").checked);
});
document.getElementById("clearRecovery").addEventListener("click", async () => {
  if (!window.confirm("Permanently delete every retained failed dictation?")) return;
  const state = await api.call("clear_recovery_entries");
  await renderRecovery(state);
  showToast(
    state.integrity_ok === false
      ? state.detail || "Retained text could not be purged"
      : "Failed-dictation recovery cleared",
    state.integrity_ok === false
  );
});
document.getElementById("recoveryList").addEventListener("click", async (event) => {
  const copy = event.target.closest("[data-recovery-copy]");
  if (copy) {
    const result = await api.call("copy_recovery_entry", copy.dataset.recoveryCopy);
    showToast(result?.detail || "Recovery item copied", result?.ok !== true);
    return;
  }
  const remove = event.target.closest("[data-recovery-delete]");
  if (remove) await renderRecovery(await api.call("delete_recovery_entry", remove.dataset.recoveryDelete));
});

document.getElementById("providerList").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-connect]");
  if (!button) return;
  const provider = button.dataset.connect;
  const keyInput = document.querySelector(`[data-key="${CSS.escape(provider)}"]`);
  // data-role is set at render time; matching on the label would break the
  // moment withBusy rewrites it (e.g. rapid double-click during "Verifying…").
  if (button.dataset.role === "disconnect") {
    const result = await api.call("disconnect_provider", provider);
    if (!result || result.ok !== true) {
      showToast(result?.detail || `Could not disconnect ${provider}`, true);
      return;
    }
    showToast(result.detail || `Disconnected ${provider}`);
    await renderProviders();
    await renderPrivacy();
    return;
  }
  if (!keyInput || !keyInput.value.trim()) {
    showToast("Enter an API key first", true);
    return;
  }
  const result = await withBusy(button, "Verifying…", () =>
    api.call("connect_provider", provider, keyInput.value, "", true));
  if (result && result.ok === false) {
    showToast(result.detail || "Could not verify that key", true);
    return;
  }
  keyInput.value = "";
  showToast(result?.detail || `Connected ${provider}`);
  await renderProviders();
  await renderPrivacy();
});

document.getElementById("privacyProviders").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-consent]");
  if (!button) return;
  if (button.textContent === "Grant consent") {
    await api.call("grant_consent", button.dataset.consent);
  } else {
    await api.call("revoke_consent", button.dataset.consent);
  }
  await renderPrivacy();
});

document.getElementById("addDictionaryRow").addEventListener("click", () => {
  const root = document.getElementById("dictionaryRows");
  const index = root ? root.querySelectorAll(".row").length : 0;
  root?.insertAdjacentHTML("beforeend", dictionaryRow("", "", index, false, stampAddedAt()));
  filterDictionaryRows();
  sortDictionaryRows();
});

document.getElementById("dictionaryFilter")?.addEventListener("input", filterDictionaryRows);
document.getElementById("dictionaryStarredOnly")?.addEventListener("change", () => {
  filterDictionaryRows();
  persistListPrefs();
});

document.getElementById("dictionarySort")?.addEventListener("change", () => {
  sortDictionaryRows();
  persistListPrefs();
});

document.getElementById("removeVisibleDictionary")?.addEventListener("click", () => {
  removeVisibleRows("dictionaryRows", "dictionaryFilter", () => {
    filterDictionaryRows();
    sortDictionaryRows();
  }, "dictionaryStarredOnly", "dictionary");
});

document.getElementById("addSnippetRow").addEventListener("click", () => {
  const root = document.getElementById("snippetRows");
  const index = root ? root.querySelectorAll(".row").length : 0;
  root?.insertAdjacentHTML("beforeend", snippetRow("", "", index, false, stampAddedAt()));
  filterSnippetRows();
  sortSnippetRows();
});

document.getElementById("snippetFilter")?.addEventListener("input", filterSnippetRows);
document.getElementById("snippetStarredOnly")?.addEventListener("change", () => {
  filterSnippetRows();
  persistListPrefs();
});

document.getElementById("snippetSort")?.addEventListener("change", () => {
  sortSnippetRows();
  persistListPrefs();
});

document.getElementById("removeVisibleSnippets")?.addEventListener("click", () => {
  removeVisibleRows("snippetRows", "snippetFilter", () => {
    filterSnippetRows();
    sortSnippetRows();
  }, "snippetStarredOnly", "snippets");
});

document.getElementById("importSnippets")?.addEventListener("click", () => {
  document.getElementById("importSnippetsFile")?.click();
});

function snippetImportSkipDetail(stats) {
  const skipParts = [];
  if (stats.skipped_empty) skipParts.push(`${Number(stats.skipped_empty)} empty`);
  if (stats.skipped_dictionary) skipParts.push(`${Number(stats.skipped_dictionary)} dictionary`);
  if (stats.skipped_snippet) skipParts.push(`${Number(stats.skipped_snippet)} snippet`);
  if (stats.skipped_malformed) skipParts.push(`${Number(stats.skipped_malformed)} invalid`);
  if (stats.skipped_duplicate) skipParts.push(`${Number(stats.skipped_duplicate)} duplicate`);
  if (stats.skipped_existing) skipParts.push(`${Number(stats.skipped_existing)} existing`);
  return skipParts.length ? ` (${skipParts.join(", ")})` : "";
}

function snippetImportStarredDetail(stats) {
  if (typeof stats.starred_detail === "string") {
    return stats.starred_detail;
  }
  const n = Number(stats.starred_added) || 0;
  return n ? `, starred ${n}` : "";
}

let pendingSnippetImport = null;
let pendingImportKind = "snippets";

function hideSnippetImportUndo() {
  const btn = document.getElementById("undoSnippetImport");
  if (btn) btn.hidden = true;
}

function showSnippetImportUndo() {
  const btn = document.getElementById("undoSnippetImport");
  if (btn) {
    btn.textContent = importUndoButtonLabel();
    btn.hidden = false;
  }
}

function hideDictionaryImportUndo() {
  const btn = document.getElementById("undoDictionaryImport");
  if (btn) btn.hidden = true;
}

function showDictionaryImportUndo() {
  const btn = document.getElementById("undoDictionaryImport");
  if (btn) {
    btn.textContent = importUndoButtonLabel("dictionary");
    btn.hidden = false;
  }
}

function hideSnippetImportReview() {
  pendingSnippetImport = null;
  const box = document.getElementById("snippetImportReview");
  if (box) box.hidden = true;
  const apply = document.getElementById("applySnippetImport");
  const done = document.getElementById("doneSnippetImport");
  const cancel = document.getElementById("cancelSnippetImport");
  if (apply) apply.hidden = false;
  if (done) done.hidden = true;
  if (cancel) cancel.hidden = false;
}

function snippetImportRowStarred(row) {
  return (row.action === "add" || row.action === "replace") && row.starred === true;
}

function snippetImportRowLabel(row) {
  const why = {
    empty: "empty",
    malformed: "invalid",
    duplicate: "duplicate",
    existing: "saved",
    dictionary: "dictionary",
    snippet: "snippet",
  }[row.reason];
  let action = "Add";
  if (row.action === "replace") action = "Replace";
  else if (row.action === "skip") action = why ? `Skip (${why})` : "Skip";
  const cue = row.spoken || "";
  const expansion = String(row.expansion || "");
  let label = expansion ? `${action} ${cue} → ${expansion}` : `${action} ${cue}`.trim();
  if (snippetImportRowStarred(row)) {
    label += " (Starred)";
  }
  return label;
}

function showSnippetImportReview(text, stats, doneOnly) {
  pendingSnippetImport = doneOnly ? null : text;
  const box = document.getElementById("snippetImportReview");
  const summary = document.getElementById("snippetImportSummary");
  const list = document.getElementById("snippetImportChanges");
  const apply = document.getElementById("applySnippetImport");
  const done = document.getElementById("doneSnippetImport");
  const cancel = document.getElementById("cancelSnippetImport");
  if (!box || !summary || !list) return;
  const added = Number(stats.added) || 0;
  const replaced = Number(stats.replaced) || 0;
  const skipped = Number(stats.skipped) || 0;
  if (doneOnly) {
    summary.textContent = importEmptyReviewSummary(
      pendingImportKind,
      skipped,
      snippetImportSkipDetail(stats),
    );
  } else {
    summary.textContent = importReviewSummary(
      pendingImportKind,
      added,
      replaced,
      skipped,
      `${snippetImportStarredDetail(stats)}${snippetImportSkipDetail(stats)}`,
    );
  }
  list.replaceChildren();
  const changes = Array.isArray(stats.changes) ? stats.changes : [];
  for (const row of changes) {
    const item = document.createElement("li");
    item.textContent = snippetImportRowLabel(row);
    list.appendChild(item);
  }
  if (apply) {
    apply.textContent = importApplyButtonLabel(pendingImportKind);
    apply.hidden = Boolean(doneOnly);
  }
  if (done) {
    done.textContent = importDoneButtonLabel(pendingImportKind);
    done.hidden = !doneOnly;
  }
  if (cancel) {
    cancel.textContent = importCancelButtonLabel(pendingImportKind);
    cancel.hidden = Boolean(doneOnly);
  }
  box.hidden = false;
}

document.getElementById("importSnippetsFile")?.addEventListener("change", async (event) => {
  await handleBulkImportFile(event, "snippets");
});

document.getElementById("importDictionary")?.addEventListener("click", () => {
  document.getElementById("importDictionaryFile")?.click();
});

document.getElementById("importDictionaryFile")?.addEventListener("change", async (event) => {
  await handleBulkImportFile(event, "dictionary");
});

async function handleBulkImportFile(event, kind) {
  const file = event.target.files && event.target.files[0];
  event.target.value = "";
  if (!file) return;
  let text = "";
  try {
    text = await file.text();
  } catch (err) {
    showToast("Could not read that file", true);
    return;
  }
  const method = kind === "dictionary" ? "preview_dictionary" : "preview_snippets";
  const statsKey = kind === "dictionary" ? "dictionary_import" : "snippet_import";
  const emptyToast = kind === "dictionary"
    ? "No dictionary terms found in that file"
    : "No snippets found in that file";
  let preview;
  try {
    preview = await api.call(method, text);
  } catch (err) {
    showToast(saveErrorMessage(err), true);
    return;
  }
  const stats = preview[statsKey] || {};
  const skipped = Number(stats.skipped) || 0;
  pendingImportKind = kind;
  if (!stats.applied) {
    if (skipped) {
      showSnippetImportReview(text, stats, true);
      return;
    }
    showToast(emptyToast, true);
    return;
  }
  showSnippetImportReview(text, stats);
}

document.getElementById("applySnippetImport")?.addEventListener("click", async () => {
  const text = pendingSnippetImport;
  if (!text) return;
  const method = pendingImportKind === "dictionary" ? "import_dictionary" : "import_snippets";
  const statsKey = pendingImportKind === "dictionary" ? "dictionary_import" : "snippet_import";
  try {
    config = await api.call(method, text);
  } catch (err) {
    showToast(saveErrorMessage(err), true);
    return;
  }
  hideSnippetImportReview();
  renderDictionary();
  const applied = config[statsKey] || {};
  showToast(
    importAppliedToast(
      pendingImportKind,
      Number(applied.added) || 0,
      Number(applied.replaced) || 0,
      Number(applied.skipped) || 0,
      `${snippetImportStarredDetail(applied)}${snippetImportSkipDetail(applied)}`,
    ),
  );
  if (config.snippet_undo) showSnippetImportUndo();
  if (config.dictionary_undo) showDictionaryImportUndo();
});

document.getElementById("doneSnippetImport")?.addEventListener("click", () => {
  const kind = pendingImportKind;
  hideSnippetImportReview();
  showToast(
    kind === "dictionary"
      ? "No dictionary terms imported — nothing to add"
      : "No snippets imported — nothing to add",
  );
});

document.getElementById("cancelSnippetImport")?.addEventListener("click", () => {
  hideSnippetImportReview();
  showToast(importCancelledToast(pendingImportKind));
});

document.getElementById("undoSnippetImport")?.addEventListener("click", async () => {
  try {
    config = await api.call("undo_snippet_import");
  } catch (err) {
    showToast(saveErrorMessage(err), true);
    return;
  }
  if (config.snippet_undo) showSnippetImportUndo();
  else hideSnippetImportUndo();
  renderDictionary();
  showToast(importUndoneToast());
});

document.getElementById("undoDictionaryImport")?.addEventListener("click", async () => {
  try {
    config = await api.call("undo_dictionary_import");
  } catch (err) {
    showToast(saveErrorMessage(err), true);
    return;
  }
  if (config.dictionary_undo) showDictionaryImportUndo();
  else hideDictionaryImportUndo();
  renderDictionary();
  showToast(importUndoneToast("dictionary"));
});

document.getElementById("exportSnippets")?.addEventListener("click", async () => {
  const starredOnly = document.getElementById("snippetStarredOnly")?.checked === true;
  const query = document.getElementById("snippetFilter")?.value || "";
  let payload;
  try {
    payload = await api.call("export_snippets", starredOnly, query);
  } catch (err) {
    showToast(saveErrorMessage(err), true);
    return;
  }
  if (!Array.isArray(payload.items) || !payload.items.length) {
    showToast(exportEmptyToast(starredOnly, query), true);
    return;
  }
  const json = JSON.stringify(payload, null, 2);
  const blob = new Blob([json], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = exportDownloadName("snippets", query, starredOnly);
  link.click();
  URL.revokeObjectURL(url);
  showToast(exportDoneToast("snippets", starredOnly, query));
});

document.getElementById("exportDictionary")?.addEventListener("click", async () => {
  const starredOnly = document.getElementById("dictionaryStarredOnly")?.checked === true;
  const query = document.getElementById("dictionaryFilter")?.value || "";
  let payload;
  try {
    payload = await api.call("export_dictionary", starredOnly, query);
  } catch (err) {
    showToast(saveErrorMessage(err), true);
    return;
  }
  const csv = String(payload.csv || "");
  if (!csv.trim()) {
    showToast(exportEmptyToast(starredOnly, query), true);
    return;
  }
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = exportDownloadName("dictionary", query, starredOnly);
  link.click();
  URL.revokeObjectURL(url);
  showToast(exportDoneToast("dictionary", starredOnly, query));
});

document.getElementById("addPerAppRow").addEventListener("click", () => {
  document.getElementById("perAppRows").insertAdjacentHTML("beforeend", perAppRow());
});

document.getElementById("addStylePerAppRow").addEventListener("click", () => {
  const root = document.getElementById("stylePerAppRows");
  if (root) root.insertAdjacentHTML("beforeend", stylePerAppRow());
});

document.getElementById("perAppRows").addEventListener("click", (event) => {
  const button = event.target.closest("[data-remove-row]");
  if (button) button.closest(".row").remove();
});

document.getElementById("stylePerAppRows").addEventListener("click", (event) => {
  const button = event.target.closest("[data-remove-row]");
  if (button) button.closest(".row").remove();
});

document.getElementById("dictionaryRows").addEventListener("click", (event) => {
  const star = event.target.closest("[data-dict=\"starred\"]");
  if (star) {
    const on = star.getAttribute("aria-pressed") !== "true";
    star.setAttribute("aria-pressed", on ? "true" : "false");
    star.textContent = on ? "Starred" : "Star";
    star.setAttribute("aria-label", dictionaryStarAria(on));
    star.title = dictionaryStarAria(on);
    filterDictionaryRows();
    sortDictionaryRows();
    return;
  }
  const button = event.target.closest("[data-remove-row]");
  if (button) button.closest(".row").remove();
});

document.getElementById("snippetRows").addEventListener("click", (event) => {
  const star = event.target.closest("[data-snippet=\"starred\"]");
  if (star) {
    const on = star.getAttribute("aria-pressed") !== "true";
    star.setAttribute("aria-pressed", on ? "true" : "false");
    star.textContent = on ? "Starred" : "Star";
    star.setAttribute("aria-label", snippetStarAria(on));
    star.title = snippetStarAria(on);
    filterSnippetRows();
    sortSnippetRows();
    return;
  }
  const button = event.target.closest("[data-remove-row]");
  if (button) button.closest(".row").remove();
});

const FALLBACK_LANGUAGE_CHOICES = [
  { code: "auto", name: "Detect automatically" },
  { code: "en", name: "English" },
  { code: "fr", name: "French" },
  { code: "de", name: "German" },
  { code: "es", name: "Spanish" },
  { code: "it", name: "Italian" },
  { code: "pt", name: "Portuguese" },
  { code: "nl", name: "Dutch" },
  { code: "pl", name: "Polish" },
  { code: "ru", name: "Russian" },
  { code: "ja", name: "Japanese (same-size fallback)" },
];

function fillLanguageSelect(selected, choices) {
  const select = document.getElementById("language");
  if (!select) return;
  const rows = Array.isArray(choices) && choices.length ? choices : FALLBACK_LANGUAGE_CHOICES;
  const current = selected || "auto";
  select.innerHTML = rows.map((row) => {
    const code = escapeHtml(row.code);
    const name = escapeHtml(row.name);
    const isSelected = row.code === current ? " selected" : "";
    return `<option value="${code}"${isSelected}>${name}</option>`;
  }).join("");
  if (![...select.options].some((opt) => opt.value === current)) {
    select.value = "auto";
  } else {
    select.value = current;
  }
}

function languageFromMode() {
  const mode = document.getElementById("languageMode").value;
  if (mode === "english") return "en";
  const typed = document.getElementById("language").value.trim();
  return typed || "auto";
}

document.getElementById("languageMode").addEventListener("change", () => {
  const row = document.getElementById("languageCodeRow");
  if (row) row.hidden = document.getElementById("languageMode").value === "english";
});

document.getElementById("learnLast").addEventListener("click", async () => {
  const input = document.getElementById("lastCorrection");
  const correction = input ? input.value.trim() : "";
  if (!correction) {
    showToast("Type what you meant first", true);
    return;
  }
  try {
    const learned = await api.call("learn_last", correction);
    config.learned = learned;
    renderLearnedTerms(learned);
    if (input) input.value = "";
    showToast(learned && learned.ok === false ? "Nothing to learn" : "Correction saved locally");
  } catch (err) {
    showToast(saveErrorMessage(err), true);
  }
});

document.getElementById("rememberAppStyle").addEventListener("click", async () => {
  const app = (document.getElementById("learnedStyleApp") || {}).value || "";
  const style = (document.getElementById("learnedStyleName") || {}).value || "";
  if (!app.trim() || !style.trim()) {
    showToast("Choose an app and a style first", true);
    return;
  }
  try {
    const learned = await api.call("remember_app_style", app.trim(), style.trim());
    config.learned = learned;
    renderLearnedTerms(learned);
    showToast("Destination style saved locally");
  } catch (err) {
    showToast(saveErrorMessage(err), true);
  }
});

document.getElementById("resetAppStyles").addEventListener("click", async () => {
  try {
    const learned = await api.call("reset_app_styles");
    config.learned = learned;
    renderLearnedTerms(learned);
    showToast("Learned destination styles cleared");
  } catch (err) {
    showToast(saveErrorMessage(err), true);
  }
});

document.getElementById("resetPersonalization").addEventListener("click", async () => {
  try {
    const learned = await api.call("reset_personalization");
    config.learned = learned;
    renderLearnedTerms(learned);
    showToast("Learned vocabulary cleared");
  } catch (err) {
    showToast(saveErrorMessage(err), true);
  }
});

document.getElementById("saveDictionary").addEventListener("click", async () => {
  const terms = [...document.querySelectorAll("#dictionaryRows .row")]
    .sort((a, b) => Number(a.dataset.index || 0) - Number(b.dataset.index || 0))
    .map((row) => ({
    spoken: row.querySelector('[data-dict="spoken"]').value,
    written: row.querySelector('[data-dict="written"]').value,
    starred: row.querySelector('[data-dict="starred"]')?.getAttribute("aria-pressed") === "true",
    added_at: row.dataset.addedAt || ""
  })).filter((entry) => entry.spoken && entry.written);
  const items = [...document.querySelectorAll("#snippetRows .row")]
    .sort((a, b) => Number(a.dataset.index || 0) - Number(b.dataset.index || 0))
    .map((row) => ({
    spoken: row.querySelector('[data-snippet="spoken"]').value,
    expansion: row.querySelector('[data-snippet="expansion"]').value,
    starred: row.querySelector('[data-snippet="starred"]')?.getAttribute("aria-pressed") === "true",
    added_at: row.dataset.addedAt || ""
  })).filter((entry) => entry.spoken);
  const dictation = {
    local_polish: document.getElementById("dictationLocalPolish").checked,
    spoken_edits: document.getElementById("dictationSpokenEdits").checked,
    developer_terms: document.getElementById("dictationDeveloperTerms").checked,
    cleanup_level: (document.getElementById("dictationCleanupLevel") || {}).value || "medium"
  };
  try {
    config = await api.call("set_config", {
      dictionary: { terms },
      snippets: { items },
      dictation,
      personalization: {
        enabled: document.getElementById("personalizationEnabled").checked,
        learn: document.getElementById("personalizationLearn").checked,
        prose_context: document.getElementById("personalizationProseContext").checked
      }
    });
  } catch (err) {
    showToast(saveErrorMessage(err), true);
    return;
  }
  renderDictionary();
  showToast("Dictionary & snippets saved");
});

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
}

function collectPerAppRows() {
  const result = {};
  document.querySelectorAll("#perAppRows .row").forEach((row) => {
    const process = row.querySelector('[data-app="process"]').value.trim();
    const injector = row.querySelector('[data-app="injector"]').value;
    if (process) result[process] = injector;
  });
  return result;
}

function collectStylePerAppRows() {
  const result = {};
  document.querySelectorAll("#stylePerAppRows .row").forEach((row) => {
    const process = row.querySelector('[data-style-app="process"]').value.trim();
    const style = row.querySelector('[data-style-app="style"]').value;
    if (process) result[process] = style;
  });
  return result;
}

function escapeAttr(value) {
  return escapeHtml(value);
}

// pywebview injects window.pywebview.api AFTER the page scripts run, so booting
// immediately would render the placeholder fallback config (version "dev",
// empty provider list) instead of the user's real settings. Poll for the
// bridge for up to 15s — a slow machine must land on the real config, never on
// the stub (whose values, once saved, would overwrite the user's settings).
// The stub is only for previewing the page in a plain browser, where saves are
// refused anyway (see mutatingCalls above).
let bootedFromBridge = false;

function tryBridgeBoot() {
  if (!window.pywebview?.api) return false;
  if (!bootedFromBridge) {
    bootedFromBridge = true;
    boot();
  }
  return true;
}

if (!tryBridgeBoot()) {
  window.addEventListener("pywebviewready", tryBridgeBoot);
  let waitedMs = 0;
  const poll = setInterval(() => {
    waitedMs += 250;
    if (tryBridgeBoot()) {
      clearInterval(poll);
    } else if (waitedMs >= 15000) {
      clearInterval(poll);
      // Plain-browser preview: render the stub. If the bridge attaches even
      // later, the pywebviewready listener re-boots from the real config.
      if (!bootedFromBridge) boot();
    }
  }, 250);
}
