# ASR backend status and evaluation policy

DCENT_Voice treats speech recognition as a replaceable backend competition.
The desktop default is evidence-driven; no provider is a permanent assumption.
Every candidate must remain local/offline by default, run on CPU, fail cleanly
when acceleration is absent, and pass the same real-audio corpus before it can
replace the default.

## Current support

| Backend | Product status | Offline CPU | Acceleration | Current decision |
|---|---|---:|---|---|
| Parakeet TDT 0.6B v3 through ONNX Runtime | Shipped desktop default with pinned model manifest | Yes | ONNX Runtime execution-provider dependent; the shipped Windows build currently validates CPU | Keep default |
| faster-whisper / CTranslate2 | Shipped fallback and opt-in profiles | Yes | CUDA when the complete runtime is validated; otherwise CPU int8 | Keep modular fallback |
| whisper.cpp through `pywhispercpp` | Optional adapter; not installed or packaged in the 2026-08-23 Windows artifact | Yes | Upstream supports Core ML, OpenVINO, CUDA, and Vulkan builds | Evaluate on a host with the optional runtime and pinned model present |
| NVIDIA Nemotron 3.5 ASR Streaming 0.6B | Research candidate; no DCENT provider or weights shipped | Upstream offers a local C++ path, but DCENT has no CPU measurement yet | Upstream publishes native cache-aware streaming and GPU measurements | High-priority candidate; not a supported backend yet |
| community `parakeet.cpp` ports | Research candidates; no DCENT provider or weights shipped | Claimed upstream; not independently measured here | Implementations target ggml GPUs or Apple Metal/Axiom | Evaluate portability, model fidelity, licenses, and reproducible builds before integration |

The official Parakeet v3 model card describes a 600M-parameter model covering
25 European languages and licenses the model under CC-BY-4.0:
<https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3>. DCENT ships a pinned,
verified ONNX conversion rather than downloading the official checkpoint at
runtime.

NVIDIA's June 2026 Nemotron 3.5 model card describes a 600M-parameter,
cache-aware streaming model with 40 language-locales, configurable 80–1120 ms
chunks, commercial use, and a local NeMo-Speech.cpp GGUF route:
<https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b>. Its published
throughput comparison is on an H100 and is not evidence for DCENT's CPU targets.

The upstream acceleration claims for whisper.cpp are documented at
<https://github.com/ggml-org/whisper.cpp>. Two actively developed but distinct
projects currently use the name `parakeet.cpp`:
<https://github.com/mudler/parakeet.cpp> and
<https://github.com/Frikallo/parakeet.cpp>. They are candidates, not dependencies
or performance claims made by this repository.

## 2026-08-24 expanded controlled CPU tournament

Host: Windows 10 x64, Ryzen 5 5600X (6 cores / 12 threads), about 15.9 GiB RAM.
All models were forced to CPU and local-file-only operation. The same corpus
contained 43 ASR cases / 197.829 seconds of audio: product and developer phrases,
numbers, URLs, public speech, long speech, 8 dB noise, UK English, EN/FR/DE/ES,
deterministic synthesized silence, and two deterministic EN/FR decoder-transition
checks. The code-switch checks concatenate separately attributed CC0 real-speech
components; they are synthetic composites, not natural conversation or a broad
accent benchmark. WER/CER below are aggregate edit rates; RTF is total ASR decode
time divided by total audio duration. File-only timings exclude microphone
capture, hotkeys, overlay, and insertion.

| Backend | Aggregate WER | Aggregate CER | RTF | Per-item p50 / p95 | Word edits | Result |
|---|---:|---:|---:|---:|---:|---|
| Parakeet TDT 0.6B v3 int8 | **3.30%** | **4.67%** | **0.087** | **0.268 / 0.891 s** | **15 / 455** | Overall accuracy and efficiency winner; retain default |
| faster-whisper Base int8 | 8.35% | 7.77% | 0.138 | 0.623 / 0.984 s | 38 / 455 | Viable small fallback, materially less accurate |
| faster-whisper Large-v3 int8 | 4.40% | 5.23% | 2.502 | 10.438 / 18.496 s | 20 / 455 | Code-switch quality profile only; far too slow as this CPU's default |

Measured 2026-08-24. Each run writes its own JSON under `artifacts/` (a
local-only directory): one expanded dictation-eval file per engine
(Parakeet, faster-whisper Base, faster-whisper Large-v3) plus a matching
code-switch file per engine. Reproduce with the evaluation commands in
[docs/build.md](build.md).

On the two synthetic transition checks, Parakeet and faster-whisper Base each
drop the trailing French half of the EN-to-FR case (aggregate code-switch WER
25%) while preserving the reverse direction. Large-v3 preserves both directions
(0% word error, 7.14% character error from punctuation/casing), but its dedicated
CPU run takes 18.426 seconds p50 and 19.559 seconds p95 for 2.757-second clips.
That is useful evidence for an opt-in accuracy profile or an accelerated host,
not justification to replace the responsive CPU default. A natural, multi-speaker,
broad-accent code-switch corpus remains required before claiming conversational
code-switch robustness.

The shipped Parakeet ONNX session caps its intra-op pool to the smaller of four
workers or the detected logical CPU count and uses one inter-op worker. On the
validated 6C/12T host, a short five-clip probe improved from 0.318 to 0.268 s
p95 without load and from 0.409 to 0.367 s p95 with two separately owned CPU-load
workers. The full expanded corpus preserved identical WER/CER and essentially
identical RTF (0.08722 to 0.08716); its p50 improved from 0.282 to 0.268 s while
long-item p95 varied from 0.839 to 0.891 s. This is a bounded-contention decision,
not a claim that every latency percentile improved.

Large-v3 initially fabricated a list of supplied hotwords on two short clips.
The faster-whisper provider now detects a transcript dominated by multiple hint
terms and retries once without hotwords; both affected clips then transcribed
correctly. If the gentler initial prompt also echoes on the retry, the result is
rejected rather than injected. This safeguard does not change the Parakeet path.

## Admission protocol for a new default

A candidate must be pinned with source, revision, hashes, license, and package
size; run offline after installation; and be scored on the checked-in corpus.
Reports must include aggregate and per-domain WER/CER, hallucination traps,
first/stable partial latency where streaming is native, final latency, RTF,
model-load time, resident/unloaded RAM, CPU, and applicable GPU/VRAM. Results
must name the OS, architecture, hardware, runtime, quantization, and whether
microphone and insertion are included.

No model replaces the default solely from upstream claims or a single demo
clip. A new default must improve the accuracy/latency/resource Pareto frontier,
preserve CPU-only operation and offline sovereignty, and retain a tested
fallback when its accelerator or optional runtime is missing.
