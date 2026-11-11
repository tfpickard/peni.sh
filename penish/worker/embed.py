import asyncio
import os
import threading
from typing import TYPE_CHECKING, Any

import numpy as np

from penish.config import Settings, get_settings

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

# Leaves a core for the api and db on the shared box.
_DEFAULT_THREADS = 3


class Embedder:
    """Process-wide singleton; the model loads on first use, off the event loop."""

    # Keyed by model name so a differently configured Embedder never reuses a
    # model loaded for another one.
    _instances: "dict[str, Embedder]" = {}
    _instance_lock = threading.Lock()

    def __new__(cls, settings: Settings | None = None) -> "Embedder":
        s = settings or get_settings()
        with cls._instance_lock:
            inst = cls._instances.get(s.embed_model)
            if inst is None:
                inst = super().__new__(cls)
                inst._model = None
                inst._load_lock = threading.Lock()
                cls._instances[s.embed_model] = inst
            inst._settings = s
            return inst

    _model: "SentenceTransformer | None"
    _load_lock: threading.Lock
    _settings: Settings

    @property
    def model_name(self) -> str:
        return self._settings.embed_model

    @property
    def dim(self) -> int:
        return self._settings.embed_dim

    def _load(self) -> "SentenceTransformer":
        with self._load_lock:
            if self._model is None:
                # Imported here so the api image, which lacks the worker extra,
                # can still import this module.
                import torch
                from sentence_transformers import SentenceTransformer

                torch.set_num_threads(int(os.environ.get("EMBED_THREADS", _DEFAULT_THREADS)))
                model = SentenceTransformer(
                    self.model_name, device="cpu", model_kwargs={"dtype": torch.float32}
                )
                # The model card recommends left padding for Qwen3-Embedding.
                model.tokenizer.padding_side = "left"
                got = model.get_embedding_dimension()
                assert got == self.dim, f"model dim {got} != EMBED_DIM {self.dim}"
                self._model = model
            return self._model

    def _encode(self, texts: list[str], **kwargs: Any) -> np.ndarray:
        model = self._load()
        out = model.encode(
            texts,
            convert_to_numpy=True,
            show_progress_bar=False,
            **kwargs,
        )
        # Normalize in float32 ourselves: the checkpoint's native bf16 output leaves norms
        # off by ~0.2%, which skews cosine math downstream.
        vecs = np.asarray(out, dtype=np.float32)
        normed: np.ndarray = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
        return normed

    async def embed_documents(self, texts: list[str]) -> np.ndarray:
        return await asyncio.to_thread(self._encode, texts)

    # Reserved for Phase 2 search.
    async def embed_query(self, text: str) -> np.ndarray:
        out = await asyncio.to_thread(self._encode, [text], prompt_name="query")
        return np.asarray(out[0], dtype=np.float32)
