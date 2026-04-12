# Local Speech Dictation System

A local, always-on dictation pipeline for Windows. Transcribes speech via faster-whisper, cleans it with a local LLM (Ollama), and injects the result directly into whatever window is currently focused.

**Toggle:** `Ctrl+\`` (backtick) — press once to start listening, again to stop and inject.

---

## Architecture

```
Microphone
    │
    ▼ float32 chunks (512 samples @ 16kHz)
AudioCapture ──► [audio_queue]
    │
    ▼ numpy array (variable length speech segment)
VADProcessor ──► [speech_queue]
    │
    ▼ string (raw transcript)
TranscriptionWorker ──► [text_queue]
    │
    ▼ string (accumulated, flushed on silence/word limit/boundary)
TranscriptionBuffer ──► [output_queue]
    │
    ▼ string (cleaned, punctuated)
TextStructurer (Ollama)
    │
    ▼
OutputInjector (clipboard + Ctrl+V → active window)
```

---

## Requirements

- Windows 10/11
- Python 3.11 (via conda — see below)
- NVIDIA GPU with CUDA 12.x (optional but strongly recommended)
- [Ollama](https://ollama.ai) installed and running (optional — raw text injected if unavailable)

---

## Setup

### 1. Create a Python 3.11 conda environment

```bash
conda create -n dictation python=3.11 -y
conda activate dictation
```

> **Why 3.11?** The `ctranslate2` backend used by faster-whisper does not yet ship
> wheels for Python 3.13. Using 3.11 avoids build-from-source pain.

### 2. Install PyTorch with CUDA support

```bash
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
```

> If you don't have a CUDA-capable GPU, install the CPU build instead:
> ```bash
> pip install torch torchaudio
> ```

### 3. Install remaining dependencies

```bash
pip install faster-whisper silero-vad sounddevice numpy pyperclip pyautogui keyboard pyyaml requests
```

### 4. Install Ollama and pull a model

Download and install Ollama from https://ollama.ai, then:

```bash
ollama pull mistral
```

> If Ollama is not running, the system still works — raw transcription text is
> injected without cleanup. Ollama is optional.

### 5. Verify audio devices (optional)

```bash
conda activate dictation
python src/main.py --list-devices
```

Set `audio.device_index` in `config/settings.yaml` if the system default isn't your microphone.

---

## Running

Open a terminal **as Administrator** (required for global hotkeys on Windows):

```bash
conda activate dictation
python src/main.py
```

The system loads models on first run (~30–60 seconds for Whisper medium). Once you see:

```
Dictation system ready.
  Press Ctrl+` to start/stop listening.
```

...it's ready.

### Debug mode

```bash
python src/main.py --debug
```

Logs every transcription fragment, buffer flush, and Ollama interaction to both the console and `dictation.log`.

### AHK launcher (alternative to terminal)

Double-click `scripts/start.ahk` — a system tray icon appears. Right-click to start/stop the Python process. The `Ctrl+\`` hotkey still comes from Python, not AHK.

---

## Hotkeys

| Key | Action |
|-----|--------|
| `Ctrl+\`` | Toggle listening ON/OFF |
| `Escape` | Cancel listening (discard buffer, no injection) |
| `Ctrl+C` | Quit the system |

---

## Configuration

All settings live in `config/settings.yaml`.

### Key settings

| Setting | Default | Description |
|---------|---------|-------------|
| `transcription.model_size` | `medium` | Whisper model size. `large-v3` for best accuracy on RTX 3060. |
| `transcription.language` | `en` | Set to `null` for auto-detect. |
| `vad.threshold` | `0.5` | VAD sensitivity. Lower = more sensitive (more false triggers). |
| `vad.min_silence_duration_ms` | `700` | Silence needed to end a speech segment. |
| `buffer.max_silence_ms` | `1500` | Silence after which the buffer flushes to output. |
| `structuring.enabled` | `true` | Set to `false` to bypass Ollama and inject raw transcription. |
| `structuring.model` | `mistral` | Any Ollama model you've pulled. `phi3` is faster, `mistral` is better. |
| `hotkey.toggle` | `ctrl+\`` | Toggle listening hotkey. |

### Disabling structuring

To use raw transcription without any LLM cleanup:

```yaml
structuring:
  enabled: false
```

---

## Performance expectations (RTX 3060 12GB)

| Whisper model | VRAM | Transcription latency |
|---------------|------|----------------------|
| `tiny` | ~0.4 GB | ~0.1s per segment |
| `medium` | ~2.5 GB | ~0.3–0.5s per segment |
| `large-v3` | ~6 GB | ~0.8–1.5s per segment |

Ollama adds ~0.5–2s for structuring per flush, depending on model and hardware.

Total latency (speech end → text in window): **1–3 seconds** with `medium` + `mistral`.

---

## Troubleshooting

**Global hotkey not working**
Run the terminal as Administrator. The `keyboard` library requires elevated privileges for global hotkey intercept on Windows.

**`ctranslate2` import error / DLL not found**
Ensure you're in the `dictation` conda env (`conda activate dictation`). If CUDA DLLs are missing, add your CUDA bin directory to PATH:
```
set PATH=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.1\bin;%PATH%
```

**Ollama model not found**
Run `ollama pull mistral` (or whichever model is set in `settings.yaml`).

**Text injected into wrong window**
The system injects into the focused window at the moment the buffer flushes. Switch to your target window before speaking, or increase `buffer.max_silence_ms` to give more time.

**Whisper hallucinations ("thank you.", "[BLANK_AUDIO]")**
These are filtered automatically. If you see others, add them to `_HALLUCINATIONS` in `src/transcribe.py`.

**High latency**
- Reduce `transcription.model_size` to `small` or `base`
- Set `structuring.enabled: false`
- Reduce `buffer.max_silence_ms` to `800`

---

## Project structure

```
├── config/
│   └── settings.yaml       All tunable parameters
├── scripts/
│   └── start.ahk           Optional AHK system-tray launcher (v1)
├── src/
│   ├── main.py             Entry point, orchestrator, hotkey registration
│   ├── audio.py            Microphone capture (sounddevice)
│   ├── vad.py              Voice activity detection (Silero VAD v5)
│   ├── transcribe.py       Speech-to-text (faster-whisper)
│   ├── buffer.py           Text accumulator with flush logic
│   ├── structure.py        LLM text cleanup (Ollama)
│   └── output.py           OS text injection (clipboard + Ctrl+V)
├── requirements.txt
└── README.md
```
