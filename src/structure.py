"""
structure.py — Text cleanup via Ollama local LLM.

TextStructurer is a synchronous callable (not a thread). It sends raw
transcription text to a local Ollama instance and returns the cleaned result.

Failure modes are handled gracefully:
  - Ollama not running → returns original text immediately
  - Request timeout → returns original text, resets availability cache
  - Empty response → returns original text with a warning

Availability is checked once per session (cached). On timeout/error, the cache
is reset so the next call re-checks, allowing recovery after Ollama restarts.
"""

import logging

import requests


class TextStructurer:
    def __init__(self, config: dict):
        self.enabled: bool = config.get('enabled', True)
        self.ollama_url: str = config.get('ollama_url', 'http://localhost:11434')
        self.model: str = config.get('model', 'mistral')
        self.timeout: float = float(config.get('timeout_seconds', 10))
        self.prompt_template: str = config.get(
            'prompt_template',
            'Clean up this speech transcription, fixing punctuation:\n{text}',
        )
        # None = unchecked, True = available, False = unavailable
        self._available: bool | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, text: str) -> str:
        """Return cleaned text, or the original if structuring is unavailable."""
        if not self.enabled or not text.strip():
            return text

        if not self._check_availability():
            return text

        return self._call_ollama(text)

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
                # Verify the requested model is actually pulled
                tags = r.json().get('models', [])
                model_names = [m.get('name', '').split(':')[0] for m in tags]
                if self.model not in model_names:
                    logging.warning(
                        f"Ollama is running but model '{self.model}' is not pulled. "
                        f"Run: ollama pull {self.model}"
                    )
                    # Still mark as available — Ollama will return an error we handle
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
        prompt = self.prompt_template.format(text=text)

        payload = {
            'model': self.model,
            'prompt': prompt,
            'stream': False,
            'options': {
                'temperature': 0.1,   # Low temperature for deterministic cleanup
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
            self._available = None  # Reset so we re-check next time
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
