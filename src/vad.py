"""
vad.py — Voice Activity Detection using Silero VAD v5.

VADProcessor is a daemon thread that reads 512-sample float32 audio chunks from
input_queue, runs them through Silero's VADIterator, and emits complete speech
segments (as concatenated numpy arrays) to output_queue.

State machine:
  - 'start' key in iterator output → begin accumulating speech
  - 'end' key in iterator output → emit segment, reset buffer
  - In-speech with no transition → keep accumulating
  - Not in speech → discard chunk
"""

import logging
import queue
import threading

import numpy as np
import torch

# Sentinel posted to input_queue by flush() — VAD emits any partial speech buffer
# and then forwards a session_end dict downstream.
_FLUSH_SENTINEL = object()


# Whisper hallucinations that sometimes slip through silence padding.
# Filtered before output to prevent garbage in the pipeline.
_HALLUCINATION_TEXTS = frozenset({
    '[blank_audio]',
    '(music)',
    '(silence)',
    '[music]',
    '[silence]',
    'thank you.',
    'thanks for watching.',
    'you',
})


class VADProcessor(threading.Thread):
    def __init__(
        self,
        config: dict,
        input_queue: queue.Queue,
        output_queue: queue.Queue,
    ):
        super().__init__(name='VADThread', daemon=True)
        self.config = config
        self.input_queue = input_queue
        self.output_queue = output_queue
        self._stop_event = threading.Event()
        self._model = None
        self._iterator = None
        self.on_speech_detected = None  # Callable[[], None] | None

    # ------------------------------------------------------------------
    # Thread lifecycle
    # ------------------------------------------------------------------

    def run(self):
        self._load_model()

        speech_buffer: list[np.ndarray] = []
        in_speech = False

        while not self._stop_event.is_set():
            try:
                chunk = self.input_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if chunk is None:  # Shutdown sentinel
                break

            if chunk is _FLUSH_SENTINEL:
                # Graceful stop: emit partial buffer then forward session_end.
                if in_speech and speech_buffer:
                    self._maybe_emit(speech_buffer)
                    speech_buffer = []
                in_speech = False
                try:
                    self.output_queue.put({'type': 'session_end'}, timeout=1.0)
                except queue.Full:
                    logging.warning("VAD: speech_queue full, dropping session_end sentinel")
                continue

            # Silero requires a 1-D float32 torch tensor
            tensor = torch.from_numpy(chunk)

            try:
                speech_dict = self._iterator(tensor, return_seconds=False)
            except Exception as e:
                logging.error(f"VAD iterator error: {e}", exc_info=True)
                continue

            if speech_dict is not None:
                if 'start' in speech_dict:
                    in_speech = True
                    speech_buffer = []
                    if self.on_speech_detected:
                        self.on_speech_detected()

                if in_speech:
                    speech_buffer.append(chunk)

                if 'end' in speech_dict:
                    in_speech = False
                    self._maybe_emit(speech_buffer)
                    speech_buffer = []

            elif in_speech:
                # Mid-speech chunk with no state transition
                speech_buffer.append(chunk)

    def stop(self):
        self._stop_event.set()

    def flush(self):
        """Emit any in-progress speech buffer and thread session_end downstream.

        Posts a flush sentinel to input_queue. The VAD thread, when it processes
        the sentinel, emits whatever speech has accumulated (if in_speech) and
        then forwards {'type': 'session_end'} to output_queue. This ensures
        session_end arrives at OutputPipeline only after all transcription results.
        """
        self.input_queue.put(_FLUSH_SENTINEL)

    def reset(self):
        """Reset VAD state — call when toggling listening OFF to clear any
        partial speech buffer from the previous session."""
        if self._iterator is not None:
            self._iterator.reset_states()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_model(self):
        """Load Silero VAD v5. Runs on the VAD thread to avoid blocking main."""
        # silero-vad v5 ships a clean Python package (not torch.hub).
        from silero_vad import VADIterator, load_silero_vad  # type: ignore

        logging.info("Loading Silero VAD model...")
        self._model = load_silero_vad()
        self._iterator = VADIterator(
            model=self._model,
            threshold=self.config['threshold'],
            sampling_rate=16000,
            min_silence_duration_ms=self.config['min_silence_duration_ms'],
            speech_pad_ms=self.config['speech_pad_ms'],
        )
        logging.info("Silero VAD loaded")

    def _maybe_emit(self, speech_buffer: list[np.ndarray]):
        """Validate length then emit segment to output_queue."""
        if not speech_buffer:
            return

        segment = np.concatenate(speech_buffer)
        min_samples = int(16000 * self.config['min_speech_duration_ms'] / 1000)

        if len(segment) < min_samples:
            logging.debug(
                f"VAD: dropped short segment ({len(segment)/16000:.2f}s)"
            )
            return

        duration = len(segment) / 16000
        logging.debug(f"VAD: emitting {duration:.2f}s speech segment")

        try:
            self.output_queue.put(segment, timeout=1.0)
        except queue.Full:
            logging.warning("VAD: speech_queue full, dropping segment")
