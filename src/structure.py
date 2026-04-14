"""
structure.py — Text cleanup pipeline.

Two-stage processing:
  1. Deterministic Python formatting (always runs):
       - Strip Whisper-generated punctuation
       - Convert spoken punctuation words ("period", "comma", etc.) to symbols
       - Fix spacing around punctuation
       - Capitalize sentence starts
  2. Optional Ollama LLM cleanup (runs when structuring.enabled = true)

Failure modes for Ollama are handled gracefully:
  - Ollama not running → returns Python-formatted text immediately
  - Request timeout → returns Python-formatted text, resets availability cache
  - Empty response → returns Python-formatted text with a warning
"""

import logging
import re
from collections import deque

import requests


# ---------------------------------------------------------------------------
# Spoken punctuation conversion
# Multi-word patterns must come before their component single-word patterns.
# ---------------------------------------------------------------------------

_SPOKEN_PUNCTUATION: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\bopen\s+quote\b', re.IGNORECASE),           '"'),
    (re.compile(r'\b(?:close|end)\s+quote\b', re.IGNORECASE),  '"'),
    (re.compile(r'\b(?:open|left)\s+paren(?:thesis)?\b', re.IGNORECASE),  '('),
    (re.compile(r'\b(?:close|right)\s+paren(?:thesis)?\b', re.IGNORECASE), ')'),
    (re.compile(r'\bquestion\s+mark\b', re.IGNORECASE),        '?'),
    (re.compile(r'\bexclamation\s+(?:point|mark)\b', re.IGNORECASE), '!'),
    (re.compile(r'\bnew\s+paragraph\b', re.IGNORECASE),        '\n\n'),
    (re.compile(r'\bnew\s+line\b', re.IGNORECASE),             '\n'),
    (re.compile(r'\bperiod\b', re.IGNORECASE),                 '.'),
    (re.compile(r'\bcomma\b', re.IGNORECASE),                  ','),
    (re.compile(r'\bcolon\b', re.IGNORECASE),                  ':'),
    (re.compile(r'\bsemicolon\b', re.IGNORECASE),              ';'),
    (re.compile(r'\bquote\b', re.IGNORECASE),                  '"'),
    (re.compile(r'\bunquote\b', re.IGNORECASE),                '"'),
    (re.compile(r'\bdash\b', re.IGNORECASE),                   ' — '),
    (re.compile(r'\bellipsis\b', re.IGNORECASE),               '...'),
]


def _strip_whisper_punctuation(text: str) -> str:
    """Remove Whisper-generated punctuation and lowercase everything.

    Capitalisation is re-applied deterministically by _capitalize_sentences.
    The only capital preserved mid-sentence is the pronoun 'I'.
    Apostrophes inside words (contractions: don't, I'm) and hyphens inside
    hyphenated words are preserved.
    """
    # Remove sentence-end marks and inline punctuation
    text = re.sub(r'[.!?,;:]', ' ', text)
    # Strip standalone quotes
    text = re.sub(r'(?<!\w)["\u201c\u201d]|["\u201c\u201d](?!\w)', ' ', text)
    # Lowercase everything, then restore the standalone pronoun 'I'
    text = text.lower()
    text = re.sub(r'\bi\b', 'I', text)
    # Collapse whitespace
    return re.sub(r'\s+', ' ', text).strip()


def _apply_spoken_punctuation(text: str) -> str:
    """Replace spoken punctuation words with their symbol equivalents."""
    for pattern, symbol in _SPOKEN_PUNCTUATION:
        text = pattern.sub(symbol, text)
    return text


def _fix_punctuation_spacing(text: str) -> str:
    """Normalize whitespace around punctuation symbols."""
    # No space before closing punctuation
    text = re.sub(r'\s+([.!?,;:])', r'\1', text)
    # No space after '(' and no space before ')'
    text = re.sub(r'\(\s+', '(', text)
    text = re.sub(r'\s+\)', ')', text)
    # Ensure exactly one space after sentence-ending punctuation (not at end of string)
    text = re.sub(r'([.!?])(?=[^\s\n])', r'\1 ', text)
    # Collapse any double spaces
    return re.sub(r'  +', ' ', text).strip()


def _capitalize_sentences(text: str) -> str:
    """Capitalize the first letter of the text and after each sentence-ending mark."""
    if not text:
        return text
    # Capitalize very first character
    text = text[0].upper() + text[1:]
    # Capitalize the first letter following '. ', '! ', or '? '
    text = re.sub(
        r'([.!?]\s+)([a-z])',
        lambda m: m.group(1) + m.group(2).upper(),
        text,
    )
    return text


def _python_format(text: str) -> str:
    """Full deterministic formatting pipeline."""
    text = _strip_whisper_punctuation(text)
    text = _apply_spoken_punctuation(text)
    text = _fix_punctuation_spacing(text)
    text = _capitalize_sentences(text)
    return text


# ---------------------------------------------------------------------------
# TextStructurer
# ---------------------------------------------------------------------------

class TextStructurer:
    def __init__(self, config: dict):
        self.enabled: bool = config.get('enabled', True)
        self.ollama_url: str = config.get('ollama_url', 'http://localhost:11434')
        self.model: str = config.get('model', 'mistral')
        self.timeout: float = float(config.get('timeout_seconds', 10))
        self.prompt_template: str = config.get(
            'prompt_template',
            'Clean up this speech transcription, fixing punctuation:\n{context_section}{text}',
        )
        context_window_size: int = config.get('context_window', 3)
        self._context: deque[str] = deque(maxlen=context_window_size)
        # None = unchecked, True = available, False = unavailable
        self._available: bool | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, text: str) -> str:
        """Format and return cleaned text."""
        if not text.strip():
            return text

        # Stage 1: deterministic Python formatting (always runs)
        text = _python_format(text)

        # Stage 2: optional Ollama LLM cleanup
        if not self.enabled or not self._check_availability():
            return text

        cleaned = self._call_ollama(text)
        self._context.append(cleaned)
        return cleaned

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _check_availability(self) -> bool:
        """Ping Ollama once and cache the result."""
        if self._available is not None:
            return self._available

        try:
            r = requests.get(f"{self.ollama_url}/api/tags", timeout=2.0)
            if r.status_code == 200:
                tags = r.json().get('models', [])
                model_names = [m.get('name', '').split(':')[0] for m in tags]
                if self.model not in model_names:
                    logging.warning(
                        f"Ollama is running but model '{self.model}' is not pulled. "
                        f"Run: ollama pull {self.model}"
                    )
                self._available = True
                logging.info(f"Ollama available at {self.ollama_url}")
            else:
                self._available = False
                logging.warning(f"Ollama returned status {r.status_code}")
        except requests.exceptions.ConnectionError:
            self._available = False
            logging.warning(
                "Ollama not reachable — structuring disabled. "
                "Start Ollama to enable text cleanup."
            )

        return self._available

    def _call_ollama(self, text: str) -> str:
        if self._context:
            context_section = (
                'Recent context (already cleaned — use for grammatical understanding only, '
                'do not repeat in output):\n'
                + '\n'.join(self._context)
                + '\n\n'
            )
        else:
            context_section = ''
        prompt = self.prompt_template.format(text=text, context_section=context_section)

        payload = {
            'model': self.model,
            'prompt': prompt,
            'stream': False,
            'options': {
                'temperature': 0.1,
                'num_predict': 512,
            },
        }

        try:
            r = requests.post(
                f"{self.ollama_url}/api/generate",
                json=payload,
                timeout=self.timeout,
            )
            r.raise_for_status()
            cleaned = r.json().get('response', '').strip()

            if cleaned:
                logging.debug(f"Structured: {text!r} → {cleaned!r}")
                return cleaned

            logging.warning("Ollama returned an empty response — using raw text")
            return text

        except requests.exceptions.Timeout:
            logging.warning(
                f"Ollama timeout ({self.timeout}s) — using raw text. "
                "Will retry on next flush."
            )
            self._available = None
            return text

        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                logging.error(
                    f"Ollama model '{self.model}' not found. "
                    f"Run: ollama pull {self.model}"
                )
            else:
                logging.error(f"Ollama HTTP error: {e}")
            self._available = None
            return text

        except requests.exceptions.RequestException as e:
            logging.error(f"Ollama request failed: {e}")
            self._available = None
            return text
