"""Model wrappers. Each model loads lazily on first use and is shared by all requests."""

import io
import logging
import os
import threading
import wave
from dataclasses import dataclass
from functools import cache
from typing import Any

import numpy as np
import torch
from huggingface_hub import hf_hub_download
from transformers import AutoModel, AutoModelForSequenceClassification, AutoTokenizer

from ml_service.text_lines import group_lines

logger = logging.getLogger(__name__)

EMBED_MODEL = os.environ.get("ML_EMBED_MODEL", "BAAI/bge-m3")
RERANK_MODEL = os.environ.get("ML_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
WHISPER_MODEL = os.environ.get("ML_WHISPER_MODEL", "small")
OCR_LANGS = os.environ.get("ML_OCR_LANGS", "vi,en").split(",")
MAX_LENGTH = int(os.environ.get("ML_MAX_LENGTH", "1024"))
BATCH_SIZE = int(os.environ.get("ML_BATCH_SIZE", "16"))

_lock = threading.Lock()  # one forward pass at a time keeps GPU memory predictable


def pick_device() -> str:
    forced = os.environ.get("ML_DEVICE")
    if forced:
        return forced
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


DEVICE = pick_device()


@dataclass
class SparseVector:
    indices: list[int]
    values: list[float]


class BgeM3:
    """bge-m3 dense (CLS, normalized) + sparse (lexical weights) heads, without FlagEmbedding.

    Sparse weights follow the reference implementation: relu(linear(hidden)) per token,
    max-pooled per token id, special tokens dropped.
    """

    def __init__(self) -> None:
        self.tokenizer = AutoTokenizer.from_pretrained(EMBED_MODEL)
        self.model = AutoModel.from_pretrained(EMBED_MODEL).to(DEVICE).eval()
        hidden = self.model.config.hidden_size
        self.sparse_linear = torch.nn.Linear(hidden, 1)
        state = torch.load(
            hf_hub_download(EMBED_MODEL, "sparse_linear.pt"), map_location="cpu", weights_only=True
        )
        self.sparse_linear.load_state_dict(state)
        self.sparse_linear.to(DEVICE).eval()
        self.special_ids = set(self.tokenizer.all_special_ids)

    @torch.inference_mode()
    def encode(self, texts: list[str]) -> tuple[list[list[float]], list[SparseVector]]:
        dense: list[list[float]] = []
        sparse: list[SparseVector] = []
        for start in range(0, len(texts), BATCH_SIZE):
            batch = texts[start : start + BATCH_SIZE]
            enc = self.tokenizer(
                batch, padding=True, truncation=True, max_length=MAX_LENGTH, return_tensors="pt"
            ).to(DEVICE)
            hidden = self.model(**enc).last_hidden_state
            cls = torch.nn.functional.normalize(hidden[:, 0], dim=-1)
            dense.extend(cls.float().cpu().tolist())
            weights = torch.relu(self.sparse_linear(hidden)).squeeze(-1).float().cpu()
            ids = enc["input_ids"].cpu()
            for row_ids, row_w in zip(ids.tolist(), weights.tolist(), strict=True):
                best: dict[int, float] = {}
                for token_id, w in zip(row_ids, row_w, strict=True):
                    if token_id in self.special_ids or w <= 0:
                        continue
                    if w > best.get(token_id, 0.0):
                        best[token_id] = w
                items = sorted(best.items())
                sparse.append(SparseVector([i for i, _ in items], [v for _, v in items]))
        return dense, sparse

    @property
    def vocab_size(self) -> int:
        return int(self.tokenizer.vocab_size)


class Reranker:
    def __init__(self) -> None:
        self.tokenizer = AutoTokenizer.from_pretrained(RERANK_MODEL)
        self.model = AutoModelForSequenceClassification.from_pretrained(RERANK_MODEL)
        self.model.to(DEVICE).eval()

    @torch.inference_mode()
    def score(self, query: str, documents: list[str]) -> list[float]:
        scores: list[float] = []
        for start in range(0, len(documents), BATCH_SIZE):
            pairs = [[query, d] for d in documents[start : start + BATCH_SIZE]]
            enc = self.tokenizer(
                pairs, padding=True, truncation=True, max_length=MAX_LENGTH, return_tensors="pt"
            ).to(DEVICE)
            logits = self.model(**enc).logits.view(-1).float()
            scores.extend(torch.sigmoid(logits).cpu().tolist())
        return scores


class Transcriber:
    def __init__(self) -> None:
        from faster_whisper import WhisperModel  # CTranslate2: CPU on macOS

        self.model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")

    def transcribe(self, wav_bytes: bytes, language: str | None) -> dict[str, Any]:
        audio = _read_wav(wav_bytes)
        segments, info = self.model.transcribe(
            audio, language=language, vad_filter=True, beam_size=5
        )
        return {
            "language": info.language,
            "duration_ms": int(info.duration * 1000),
            "segments": [
                {"start_ms": int(s.start * 1000), "end_ms": int(s.end * 1000), "text": s.text.strip()}
                for s in segments
                if s.text.strip()
            ],
        }


class Ocr:
    def __init__(self) -> None:
        import easyocr

        self.reader = easyocr.Reader(OCR_LANGS, gpu=DEVICE != "cpu", verbose=False)

    def read(self, image_bytes: bytes) -> list[dict[str, Any]]:
        from PIL import Image

        image = np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB"))
        return group_lines(self.reader.readtext(image))


def _read_wav(data: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(data)) as wav:
        if wav.getsampwidth() != 2 or wav.getnchannels() != 1 or wav.getframerate() != 16000:
            raise ValueError("expected 16 kHz mono 16-bit PCM WAV")
        frames = wav.readframes(wav.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


@cache
def bge_m3() -> BgeM3:
    logger.info("loading %s on %s", EMBED_MODEL, DEVICE)
    return BgeM3()


@cache
def reranker() -> Reranker:
    logger.info("loading %s on %s", RERANK_MODEL, DEVICE)
    return Reranker()


@cache
def transcriber() -> Transcriber:
    logger.info("loading faster-whisper %s", WHISPER_MODEL)
    return Transcriber()


@cache
def ocr() -> Ocr:
    logger.info("loading EasyOCR %s", OCR_LANGS)
    return Ocr()


def locked() -> threading.Lock:
    return _lock
