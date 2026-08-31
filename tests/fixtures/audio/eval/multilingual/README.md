# Multilingual real-speech fixtures

Run the shipped default Parakeet path (never tiny, never silence):

```powershell
uv run python scripts/eval_dictation.py --corpus eval/multilingual.json --language-mode auto --json
```

English uses the committed `hello.wav` fixture. French, German, and Spanish use
Lingua Libre recordings published on Wikimedia Commons under CC0 1.0.
This is a routing and quality check on the shipped local model, not a large-scale
language benchmark.

The source recordings are otherwise unmodified; the committed WAVs are 48 kHz
mono PCM and the ASR/evaluation path performs its normal in-memory resampling.
The canonical CC0 deed is
<https://creativecommons.org/publicdomain/zero/1.0/> and the complete legal code
is bundled at
[`packaging/licenses/CC0-1.0.txt`](../../../../../packaging/licenses/CC0-1.0.txt).

## French — je m’appelle

`fr-je-mappelle.wav` spoken by XANA000.

- Source: <https://commons.wikimedia.org/wiki/File:LL-Q150_(fra)-XANA000-je_m%E2%80%99appelle.wav>
- SHA-256: `168fa81af78b34da34adc79fd9b29b4d4d9c7478ad60ee4c0bed8498555bafda`

## German — Hallo

`de-hallo.wav` spoken by Trypeds.

- Source: <https://commons.wikimedia.org/wiki/File:LL-Q188_(deu)-Trypeds-Hallo.wav>
- License: CC0 1.0
- SHA-256: `3657db7aad9c38cd3d998458acfc84c797ae298c204d433d5d5cfdf551a4a24c`

## Spanish — Hola

`es-hola.wav` spoken by CaroEspinoza.

- Source: <https://commons.wikimedia.org/wiki/File:LL-Q1321_(spa)-CaroEspinoza-HOLA.wav>
- License: CC0 1.0
- SHA-256: `ecad3025298576f5c13d3077b776682f8292a8a00f1b99b91bfe79771237d829`
