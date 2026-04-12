# User Optimizations

A living document of model recommendations, config tweaks, and performance tips for the Local Speech Dictation System. Add to this as new optimizations are discovered.

---

## Ollama Model Recommendations

| Model | Speed | Quality | VRAM | Pull command |
|-------|-------|---------|------|--------------|
| `mistral` | Medium | ★★★★☆ | ~4 GB | `ollama pull mistral` |
| `phi3` | Fast | ★★★☆☆ | ~2 GB | `ollama pull phi3` |

**Recommendation:** Use `phi3` if Ollama latency feels slow (it's noticeably faster for simple cleanup tasks). Use `mistral` if output quality matters more than speed.

To switch models, edit `config/settings.yaml`:
```yaml
structuring:
  model: phi3   # or mistral
```

---

## Whisper Model Recommendations

| Model | VRAM | Latency per segment | Best for |
|-------|------|---------------------|----------|
| `tiny` | ~0.4 GB | ~0.1s | Testing / lowest latency |
| `base` | ~0.7 GB | ~0.2s | Fast machines, short dictation |
| `small` | ~1.2 GB | ~0.2–0.3s | Good balance |
| `medium` | ~2.5 GB | ~0.3–0.5s | **Default — recommended** |
| `large-v3` | ~6 GB | ~0.8–1.5s | Best accuracy (RTX 3060 handles this) |

**Recommendation:** Start with `medium`. If accuracy on technical vocabulary is important (e.g. engineering terms), upgrade to `large-v3` — the RTX 3060 12GB has plenty of VRAM for it.

To switch models, edit `config/settings.yaml`:
```yaml
transcription:
  model_size: large-v3
```

---

## Latency Tuning

If end-to-end latency (speech end → text in window) feels too slow:

1. **Disable structuring** — cuts Ollama round-trip entirely:
   ```yaml
   structuring:
     enabled: false
   ```

2. **Reduce silence threshold** — text injects sooner after you stop speaking:
   ```yaml
   buffer:
     max_silence_ms: 800   # default: 1500
   vad:
     min_silence_duration_ms: 400   # default: 700
   ```

3. **Downgrade Whisper model** — `small` is ~2x faster than `medium`:
   ```yaml
   transcription:
     model_size: small
   ```

4. **Switch to `phi3`** — faster Ollama inference than `mistral`.

---

## VAD Sensitivity Tuning

If the system triggers on background noise (too sensitive):
```yaml
vad:
  threshold: 0.65   # default: 0.5 — raise to reduce false triggers
```

If the system misses the start of speech (not sensitive enough):
```yaml
vad:
  threshold: 0.35   # lower to catch quieter speech
```

---

## Environment / Startup Checklist

Always check your prompt shows `(dictation)` before running any command.
If it shows `(base)`, the wrong Python environment is active and imports will fail.

```powershell
conda activate dictation   # run this first, every time
python src/main.py
```

A `ModuleNotFoundError` for any package (`pyautogui`, `faster_whisper`, etc.) almost
always means you're in the base env, not the dictation env.

---

## To-Do / Future Optimizations

- [ ] Test `large-v3` model accuracy vs `medium` on technical vocabulary
- [ ] Benchmark `phi3` vs `mistral` cleanup quality on real dictation samples
- [ ] Evaluate `VAD threshold` sweet spot for home office background noise
