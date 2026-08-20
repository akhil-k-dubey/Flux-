"""
flux/language/generation/vocab_builder.py

Build a word-level vocabulary from Flux's own KG corpus.

Sources: definitions, examples, non_examples across all KG nodes.
Special tokens: [PAD]=0, [UNK]=1, [BOS]=2, [EOS]=3.
Output: logs/generation/vocab.json  {word → idx}

Why word-level:
  Character-level with 30 training pairs memorizes, not generalizes.
  Word-level vocabulary from the domain corpus (~3,500 tokens) gives
  the decoder prior structure and prevents hallucination of out-of-domain
  terminology — it can only produce words Flux has already seen.
"""
from __future__ import annotations

import json
import re
import logging
from collections import Counter
from pathlib import Path

log = logging.getLogger(__name__)

VOCAB_PATH = Path("logs/generation/vocab.json")

SPECIAL_TOKENS = ["[PAD]", "[UNK]", "[BOS]", "[EOS]"]
PAD_ID  = 0
UNK_ID  = 1
BOS_ID  = 2
EOS_ID  = 3


def _tokenize(text: str) -> list[str]:
    """Split text into word-level tokens, preserving punctuation as tokens."""
    # Split on whitespace, keep punctuation as separate tokens
    return re.findall(r"[a-zA-Z']+|[0-9]+(?:\.[0-9]+)?|[^\s\w]", text.lower())


def build_vocab(
    kg_nodes: dict,
    min_freq:  int = 1,
    max_vocab: int = 4000,
    gold_texts: list[str] | None = None,
) -> dict[str, int]:
    """
    Build vocabulary from all KG node text fields + optional gold output texts.

    Parameters
    ----------
    kg_nodes   : dict  — output of store.all_nodes()
    min_freq   : int   — minimum frequency to include a word
    max_vocab  : int   — max vocabulary size (including special tokens)
    gold_texts : list  — raw output strings from training pairs; ensures
                         every target word is in vocab (zero OOV during training)

    Returns
    -------
    dict mapping word → index
    """
    word_counts: Counter = Counter()

    text_fields = ["definition", "examples", "non_examples", "note", "layer"]

    for cid, node in kg_nodes.items():
        # Include the concept name itself
        word_counts.update(_tokenize(cid.replace("_", " ")))

        for field in text_fields:
            val = node.get(field)
            if not val:
                continue
            if isinstance(val, list):
                for item in val:
                    word_counts.update(_tokenize(str(item)))
            else:
                word_counts.update(_tokenize(str(val)))

    # Add gold output texts — ensures training targets have zero OOV
    if gold_texts:
        for text in gold_texts:
            word_counts.update(_tokenize(text))

    # Build vocab: special tokens first, then most-frequent words
    max_regular = max_vocab - len(SPECIAL_TOKENS)
    vocab_words = SPECIAL_TOKENS + [
        word
        for word, count in word_counts.most_common(max_regular)
        if count >= min_freq
    ]

    vocab = {word: idx for idx, word in enumerate(vocab_words)}
    log.info(
        "Vocabulary built: %d tokens from %d KG nodes (top-word freq=%d)",
        len(vocab), len(kg_nodes),
        word_counts.most_common(1)[0][1] if word_counts else 0,
    )
    return vocab


def save_vocab(vocab: dict[str, int], path: Path = VOCAB_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(vocab, ensure_ascii=False, indent=2))
    log.info("Vocabulary saved: %d tokens → %s", len(vocab), path)


def load_vocab(path: Path = VOCAB_PATH) -> dict[str, int]:
    if not path.exists():
        raise FileNotFoundError(f"Vocabulary not found at {path}. Run build first.")
    return json.loads(path.read_text(encoding="utf-8"))


def encode(text: str, vocab: dict[str, int]) -> list[int]:
    """Encode a text string to token IDs using the vocabulary."""
    return [vocab.get(tok, UNK_ID) for tok in _tokenize(text)]


def decode(ids: list[int], vocab: dict[str, int]) -> str:
    """Decode token IDs back to a text string."""
    idx_to_word = {v: k for k, v in vocab.items()}
    tokens = [idx_to_word.get(i, "[UNK]") for i in ids if i not in (PAD_ID, BOS_ID, EOS_ID)]
    # Simple detokenize: join with spaces, fix spacing around punctuation
    text = " ".join(tokens)
    text = re.sub(r" ([.,!?;:'])", r"\1", text)
    text = re.sub(r"' s\b", "'s", text)
    return text


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    sys.path.insert(0, ".")
    from flux.kg.store import get_store

    store = get_store()
    vocab = build_vocab(store.all_nodes())
    save_vocab(vocab)
    print(f"Vocabulary: {len(vocab)} tokens saved to {VOCAB_PATH}")
    print(f"Sample tokens: {list(vocab.items())[:20]}")
