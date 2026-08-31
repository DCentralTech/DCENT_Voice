// DCENT_Voice — open-source, local-first voice dictation
// Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
// SPDX-License-Identifier: MIT
(() => {
  const stateClasses = ["is-listening", "is-processing", "is-loading", "is-active", "is-discarded"];
  const bodyStateClasses = [
    "state-listening",
    "state-processing",
    "state-loading",
    "state-streaming",
    "state-active",
    "state-command",
    "state-discarded",
    "state-permission",
  ];
  const stateLabels = {
    listening: "Listening",
    loading: "Loading model",
    processing: "Processing",
    streaming: "Live",
    command: "Command",
    active: "Inserted",
    discarded: "No speech",
    permission: "Microphone",
  };
  let icon = null;
  let targetLevel = 0;
  let displayLevel = 0;
  let rafId = null;
  let currentStateLabel = "";
  let currentMessage = "";
  const currentChips = {
    langBadge: "",
    styleBadge: "",
    cleanupBadge: "",
    priorityBadge: "",
  };

  const clamp01 = (value) => Math.max(0, Math.min(1, Number(value) || 0));

  function updateAccessibleStatus() {
    const el = document.getElementById("accessibleStatus");
    if (!el) {
      return;
    }
    el.textContent = [currentStateLabel, currentMessage, ...Object.values(currentChips)]
      .filter(Boolean)
      .join(". ");
  }

  async function loadIcon() {
    const host = document.getElementById("iconHost");
    const response = await fetch("./dcent_voice_icon.svg");
    host.innerHTML = await response.text();
    icon = host.querySelector("#dcent-voice-icon");
    if (icon) {
      icon.style.setProperty("--voice-pulse", "0");
    }
  }

  function setState(state) {
    document.body.classList.add("is-visible");
    document.body.classList.remove(...bodyStateClasses);
    startTick();
    const label = stateLabels[state] || "Listening";
    currentStateLabel = label;
    const labelEl = document.getElementById("stateLabel");
    if (labelEl) {
      labelEl.textContent = label;
    }
    if (!icon) {
      document.body.classList.add(`state-${state}`);
      updateAccessibleStatus();
      return;
    }
    icon.classList.remove(...stateClasses);
    if (state === "listening") {
      icon.classList.add("is-listening");
      document.body.classList.add("state-listening");
      setMessage("");
    } else if (state === "loading") {
      icon.classList.add("is-loading");
      document.body.classList.add("state-loading");
    } else if (state === "processing") {
      icon.classList.add("is-processing");
      document.body.classList.add("state-processing");
      setMessage("");
    } else if (state === "streaming") {
      icon.classList.add("is-listening");
      document.body.classList.add("state-streaming");
    } else if (state === "active") {
      icon.classList.add("is-active");
      document.body.classList.add("state-active");
      setMessage("");
    } else if (state === "command") {
      icon.classList.add("is-listening");
      document.body.classList.add("state-command");
      setMessage("");
    } else if (state === "discarded") {
      icon.classList.add("is-discarded");
      document.body.classList.add("state-discarded");
    } else if (state === "permission") {
      icon.classList.add("is-discarded");
      document.body.classList.add("state-permission");
    }
    updateAccessibleStatus();
  }

  function setMessage(message) {
    const el = document.getElementById("statusMessage");
    if (!el) {
      return;
    }
    const text = String(message || "").trim();
    currentMessage = text;
    el.textContent = text;
    el.hidden = !text;
    updateAccessibleStatus();
  }

  function setLevel(level) {
    targetLevel = clamp01(level);
  }

  function setPrivacy(status) {
    document.body.classList.toggle("privacy-cloud", status === "cloud" || status === "hybrid");
  }

  function setChip(id, label) {
    const el = document.getElementById(id);
    if (!el) {
      return;
    }
    const text = String(label || "").trim();
    if (Object.prototype.hasOwnProperty.call(currentChips, id)) {
      currentChips[id] = text;
    }
    el.textContent = text;
    el.hidden = !text;
    updateAccessibleStatus();
  }

  function setLanguage(label) {
    setChip("langBadge", label);
  }

  function setStyle(label) {
    setChip("styleBadge", label);
  }

  function setCleanup(label) {
    setChip("cleanupBadge", label);
  }

  function overlayPriorityTitle(text) {
    text = String(text || "").trim();
    return text ? `Starred: ${text}` : "";
  }

  function setPriority(label) {
    const title = overlayPriorityTitle(label);
    setChip("priorityBadge", title);
    const el = document.getElementById("priorityBadge");
    if (el) {
      el.title = title;
      if (title) el.setAttribute("aria-label", title);
      else el.removeAttribute("aria-label");
    }
  }

  function setReducedMotion(enabled) {
    document.documentElement.classList.toggle("reduced-motion", Boolean(enabled));
  }

  function hide() {
    document.body.classList.remove("is-visible", ...bodyStateClasses);
    if (icon) {
      icon.classList.remove(...stateClasses);
    }
    currentStateLabel = "";
    currentMessage = "";
    Object.keys(currentChips).forEach((key) => {
      currentChips[key] = "";
    });
    setMessage("");
    setLanguage("");
    setStyle("");
    setCleanup("");
    setPriority("");
    updateAccessibleStatus();
    // Stop the animation loop and rest the icon while hidden so the renderer
    // isn't woken ~60x/s for an overlay nobody can see.
    stopTick();
    targetLevel = 0;
    displayLevel = 0;
    document.documentElement.style.setProperty("--voice-pulse", "0");
    if (icon) {
      icon.style.setProperty("--voice-pulse", "0");
    }
  }

  function tick() {
    // Fast attack, gentle release — like a VU meter — so the waveform snaps up
    // on speech onsets but settles smoothly, tracking the voice in real time.
    const rate = targetLevel > displayLevel ? 0.5 : 0.16;
    displayLevel += (targetLevel - displayLevel) * rate;
    // Perceptual curve: the meter already log-scales to ~0.15–0.45 for normal
    // speech; this lifts that band so ordinary talking visibly drives the wave.
    const shaped = Math.pow(clamp01(displayLevel), 0.7);
    const value = shaped.toFixed(3);
    document.documentElement.style.setProperty("--voice-pulse", value);
    if (icon) {
      icon.style.setProperty("--voice-pulse", value);
    }
    rafId = requestAnimationFrame(tick);
  }

  function startTick() {
    if (rafId === null) {
      rafId = requestAnimationFrame(tick);
    }
  }

  function stopTick() {
    if (rafId !== null) {
      cancelAnimationFrame(rafId);
      rafId = null;
    }
  }

  window.dcent = {
    setState,
    setLevel,
    setPrivacy,
    setMessage,
    setLanguage,
    setStyle,
    setCleanup,
    setPriority,
    setReducedMotion,
    hide,
  };
  loadIcon().catch(() => {});
})();
