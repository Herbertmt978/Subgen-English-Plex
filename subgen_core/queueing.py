"""Queue contracts and mechanics for Subgen tasks."""

import hashlib
import queue
import time
from threading import Event, Lock


class TaskResult:
    """Stores the result of a queued task for blocking retrieval"""
    def __init__(self):
        self.result = None
        self.error = None
        self.done = Event()

    def set_result(self, result):
        self.result = result
        self.done.set()

    def set_error(self, error):
        self.error = error
        self.done.set()

    def wait(self, timeout=None):
        """Block until result is ready. Returns True if completed, False if timeout."""
        return self.done.wait(timeout)


def task_event_id(task: dict) -> str:
    identity = f"{task.get('type', 'transcribe')}:{task.get('path', 'unknown')}"
    return hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]


def generate_audio_hash(
    audio_content: bytes,
    task: str = None,
    language: str = None,
    output_format: str = None,
    word_timestamps: bool = None,
    initial_prompt: str = None,
    encode: bool = None,
) -> str:
    """
    Generate a deterministic hash from audio content and optional parameters.

    Same audio and inference-affecting options always produce the same hash.
    This ensures duplicate requests are caught by the queue.

    Args:
        audio_content: Raw audio bytes from uploaded file
        task: Optional task type ('transcribe' or 'translate')
        language: Optional target language code
        output_format: Optional response format
        word_timestamps: Whether word-level timestamps are requested
        initial_prompt: Optional model prompt
        encode: Whether the legacy ASR request re-encodes uploaded audio

    Returns:
        SHA256 hash (first 16 chars for brevity in logs)
    """
    hash_input = audio_content

    # Include task and language for fine-grained deduplication
    if task:
        hash_input += task.encode('utf-8')
    if language:
        hash_input += language.encode('utf-8')
    if output_format:
        hash_input += output_format.encode('utf-8')
    if word_timestamps is not None:
        hash_input += str(word_timestamps).encode('utf-8')
    if initial_prompt:
        hash_input += initial_prompt.encode('utf-8')
    if encode is not None:
        hash_input += str(encode).encode('utf-8')

    full_hash = hashlib.sha256(hash_input).hexdigest()
    return full_hash[:16] # Use first 16 chars for shorter IDs in logs


class DeduplicatedQueue(queue.PriorityQueue):
    """Queue that prevents duplicates, handles priority, and tracks status."""
    def __init__(self):
        super().__init__()
        self._queued = set()     # Tracks task IDs waiting in queue
        self._processing = set() # Tracks task IDs currently being handled
        self._lock = Lock()

    def put(self, item, block=True, timeout=None):
        with self._lock:
            task_id = item["path"]
            if task_id not in self._queued and task_id not in self._processing:
                # Priority: 0 (Detect), 1 (ASR), 2 (Transcribe)
                task_type = item.get("type", "transcribe")
                priority = 0 if task_type == "detect_language" else (1 if task_type == "asr" else 2)

                # PriorityQueue requires a tuple: (priority, tie_breaker, item)
                super().put((priority, time.time(), item), block, timeout)
                self._queued.add(task_id)
                return True
            return False

    def get(self, block=True, timeout=None):
        # PriorityQueue returns the tuple, we want just the item
        priority, timestamp, item = super().get(block, timeout)
        with self._lock:
            task_id = item["path"]
            self._queued.discard(task_id)
            self._processing.add(task_id)
        return item

    def mark_done(self, item):
        with self._lock:
            task_id = item["path"]
            self._processing.discard(task_id)

    def is_idle(self):
        with self._lock:
            return self.empty() and len(self._processing) == 0

    def is_active(self, task_id):
        """Checks if a task_id is currently queued or processing."""
        with self._lock:
            return task_id in self._queued or task_id in self._processing

    def get_queued_tasks(self):
        with self._lock:
            return list(self._queued)

    def get_processing_tasks(self):
        with self._lock:
            return list(self._processing)
