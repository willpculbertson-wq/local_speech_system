"""
audio.py — Microphone capture via sounddevice.

AudioCapture wraps a sounddevice InputStream with a callback-based interface.
Audio frames are placed onto audio_queue as float32 numpy arrays of chunk_size
samples. The PortAudio callback thread is never blocked — if the queue is full,
the frame is silently dropped rather than stalling capture.
"""

import logging
import queue

import numpy as np
import sounddevice as sd


class AudioCapture:
    def __init__(self, config: dict, output_queue: queue.Queue):
        self.sample_rate: int = config['sample_rate']
        self.channels: int = config['channels']
        self.chunk_size: int = config['chunk_size']
        self.device_index = config.get('device_index')  # None = system default
        self.output_queue = output_queue
        self._stream: sd.InputStream | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype='float32',
            blocksize=self.chunk_size,
            device=self.device_index,
            callback=self._audio_callback,
        )
        self._stream.start()
        logging.info(
            f"Audio capture started — device={self.device_index}, "
            f"rate={self.sample_rate}Hz, chunk={self.chunk_size} samples"
        )

    def stop(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
            logging.info("Audio capture stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info,
        status: sd.CallbackFlags,
    ):
        """Called on the PortAudio thread — must be fast and non-blocking."""
        if status:
            logging.warning(f"Audio callback status: {status}")

        # indata shape: (frames, channels). Flatten to 1-D float32.
        # .copy() is MANDATORY — indata is a view into a reused PortAudio buffer.
        audio_chunk = indata[:, 0].copy()
        try:
            self.output_queue.put_nowait(audio_chunk)
        except queue.Full:
            pass  # Drop frame rather than block the audio thread

    # ------------------------------------------------------------------
    # Debug helpers
    # ------------------------------------------------------------------

    @staticmethod
    def list_devices():
        """Print all available audio devices. Call manually during setup."""
        print(sd.query_devices())
