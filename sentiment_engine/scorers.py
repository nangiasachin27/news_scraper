"""Scorers turn texts into {positive, negative, neutral} probabilities.

Anything implementing ``Scorer`` plugs in: FinBERT (default), FinBERT-tone, a
fine-tuned model, or an LLM wrapper. The engine only ever calls ``score``.
"""
from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)

LABELS = ("positive", "negative", "neutral")


class Scorer(Protocol):
    name: str

    def score(self, texts: list[str]) -> list[dict[str, float]]:
        ...


class MaterialityScorer(Protocol):
    name: str

    def score(self, texts: list[str]) -> list[float]:
        """Probability in [0, 1] that each headline is material to the share price."""
        ...


# Fixed before any backtest results were seen; change them only as a new, separately
# tested experiment, never to improve a result already looked at.
MATERIAL_LABEL = "news that could move the company's share price"
ROUTINE_LABEL = "routine news with no effect on the share price"
HYPOTHESIS_TEMPLATE = "This headline is {}."


class ZeroShotMateriality:
    """Zero-shot NLI: does the headline entail 'news that could move the share price'
    rather than 'routine news'? Scores tone-free importance, which FinBERT does not.
    Works with any NLI checkpoint (facebook/bart-large-mnli by default)."""

    def __init__(self, model_name: str = "facebook/bart-large-mnli", device: str = "auto",
                 batch_size: int = 16, fp16: bool = True) -> None:
        try:
            import torch
            from transformers import pipeline
        except ImportError as exc:
            raise ImportError("materiality needs torch + transformers: pip install -r requirements-sentiment.txt") from exc
        use_cuda = torch.cuda.is_available() if device == "auto" else device == "cuda"
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("device='cuda' requested but torch.cuda.is_available() is False")
        kwargs = {"torch_dtype": torch.float16} if use_cuda and fp16 else {}
        self._pipe = pipeline("zero-shot-classification", model=model_name, tokenizer=model_name,
                              device=0 if use_cuda else -1, **kwargs)
        self.name = model_name
        self.device = "cuda" if use_cuda else "cpu"
        self.batch_size = batch_size
        log.info("loaded materiality model %s on %s", model_name, self.device)

    def score(self, texts: list[str]) -> list[float]:
        if not texts:
            return []
        rows = self._pipe(texts, candidate_labels=[MATERIAL_LABEL, ROUTINE_LABEL],
                          hypothesis_template=HYPOTHESIS_TEMPLATE, multi_label=False, batch_size=self.batch_size)
        if isinstance(rows, dict):
            rows = [rows]
        return [float(dict(zip(r["labels"], r["scores"]))[MATERIAL_LABEL]) for r in rows]


class FinBERTScorer:
    """Hugging Face sequence classifier. Works with ProsusAI/finbert and
    yiyanghkust/finbert-tone (labels are lower-cased; ``label_map`` covers models
    that emit LABEL_0/1/2)."""

    def __init__(
        self,
        model_name: str = "ProsusAI/finbert",
        device: str = "auto",
        batch_size: int = 32,
        max_length: int = 512,
        fp16: bool = True,
        label_map: dict[str, str] | None = None,
    ) -> None:
        try:
            import torch
            from transformers import pipeline
        except ImportError as exc:
            raise ImportError(
                "FinBERT needs torch + transformers: pip install -r requirements-sentiment.txt"
            ) from exc

        use_cuda = torch.cuda.is_available() if device == "auto" else device == "cuda"
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("device='cuda' requested but torch.cuda.is_available() is False")
        kwargs = {}
        if use_cuda and fp16:
            kwargs["torch_dtype"] = torch.float16
        self._pipe = pipeline(
            "text-classification",
            model=model_name,
            tokenizer=model_name,
            device=0 if use_cuda else -1,
            top_k=None,  # return all three label probabilities
            **kwargs,
        )
        self.name = model_name
        self.device = "cuda" if use_cuda else "cpu"
        self.batch_size = batch_size
        self.max_length = max_length
        self.label_map = {k.lower(): v for k, v in (label_map or {}).items()}
        log.info("loaded %s on %s", model_name, self.device)

    def score(self, texts: list[str]) -> list[dict[str, float]]:
        if not texts:
            return []
        rows = self._pipe(texts, batch_size=self.batch_size, truncation=True, max_length=self.max_length)
        out = []
        for row in rows:
            if isinstance(row, dict):
                row = [row]
            probs = {}
            for d in row:
                label = d["label"].lower()
                probs[self.label_map.get(label, label)] = float(d["score"])
            out.append({k: probs.get(k, 0.0) for k in LABELS})
        return out
