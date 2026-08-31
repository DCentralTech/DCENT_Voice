# LibriSpeech evaluation clips

The WAV files under `tests/fixtures/audio/eval/librispeech/` are 16 kHz mono
extracts of LibriSpeech *test/dev-clean* utterances (CC BY 4.0).

Governing license: <https://creativecommons.org/licenses/by/4.0/>. The
complete legal text is bundled in [`packaging/licenses/CC-BY-4.0.txt`](../packaging/licenses/CC-BY-4.0.txt).

Source: Panayotov, Chen, Povey, Khudanpur — LibriSpeech ASR corpus
(https://www.openslr.org/12). Dummy packaging used:
`hf-internal-testing/librispeech_asr_dummy` (same underlying audio).

These clips are for product WER measurement. They are **not** SAPI-synthesized.
Do not mix their WER with text-only polish items.

Additional *test-clean* utterances (speakers 61, 121, 237, 260, 1089, 1580,
3570, 6930, 8455) were extracted from the official OpenSLR `test-clean.tar.gz`
and resampled to 16 kHz mono WAV. That set is mixed-gender and not a single
reader. Extraction and resampling are modifications made for this evaluation
corpus; the underlying speech remains attributed to LibriSpeech and its source
speakers under CC BY 4.0.

`tests/fixtures/audio/eval/noisy/*-8db.wav` are those same utterances mixed
with additive white noise at 8 dB SNR for a noisy-room gate. They are not
new recordings. W47 added endeavour / Christmas / Quilter / fortune remixes
via `scripts/mix_eval_awgn.py` (seeds 47–50). Existing exist / wait / marie
8 dB files were left bit-identical.
