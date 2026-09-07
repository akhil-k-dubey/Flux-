"""
KG Garbage Collector — removes sentence fragments and noise nodes.

Protected: source=="seed" OR starts with "flux_"
Everything else is judged on concept_id quality + definition presence.
"""
from __future__ import annotations
import json, logging, re, shutil, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from flux.kg.store import KGStore

log = logging.getLogger("kg_cleaner")
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    level=logging.INFO, datefmt="%H:%M:%S")

_STOPWORDS = {
    "we","if","for","the","a","an","and","or","is","it","this","that","in",
    "of","to","as","at","by","on","with","from","be","are","was","were",
    "let","so","but","then","when","which","where","since","while","hence",
    "thus","therefore","note","recall","observe","suppose","consider",
    "assume","assuming","implies","asserts","claim","now","next","here",
    "also","such","these","those","each","every","both","either","neither",
    "its","their","our","your","all","some","any","no","not","can","will",
    "shall","may","must","should","would","could","need","do","does","did",
    "done","has","have","had","been","being","get","got","put","say","said",
    "make","made","use","used","see","seen","know","show","shows","shown",
    "take","taken","give","given","find","found","hold","holds","follow",
    "follows","there","again","once","still","always","only","just","even",
    "more","most","many","much","very","another","other","same","first",
    "last","new","old","given","using","applying","applies","apply",
    "consider","noting","notice","hence","moreover","furthermore",
    "however","therefore","similarly","conversely","equivalently",
    "recall","denote","define","let","set","call","write","put",
    "take","choose","pick","fix","prove","proof","claim","shows","holds",
    "satisfies","reduces","follows","implies","yields","gives",
}

_FRAG_PATTERNS = [
    re.compile(r"^\d"),                                  # starts with digit
    re.compile(r"^__"),                                  # dunder artifact
    re.compile(r"^\d+[a-z]"),                            # digit+letter OCR
    re.compile(r"\d{4,}"),                               # long number inside
    re.compile(r"[a-z]_[a-z]vail|[a-z]_[a-z]ble"),      # OCR split-word artifacts
    re.compile(r"_is_a$|_is_the$|_of_a$|_of_the$"),     # sentence-end fragment
    re.compile(r"_to$|_by$|_at$|_in$|_on$"),            # ends in preposition
    re.compile(r"^(?:any|such|every|each|all)_"),        # quantifier lead-ins
    re.compile(r"proposition\d+_\d+|theorem\d+_\d+"),   # numbered theorem refs
    re.compile(r"nding_|claimsin|oglkl|concaue"),        # OCR typo artifacts
]

_MAX_TOKENS = 4

# Real technical terms that are 1-4 tokens and should always be kept
_KNOWN_GOOD_RE = re.compile(
    r"^(entropy|eigenvalue|eigenvector|gradient|backprop|momentum|"
    r"convolution|pooling|softmax|sigmoid|relu|attention|transformer|"
    r"bayesian|markov|gaussian|poisson|integral|derivative|topology|"
    r"functor|morphism|adjunction|isomorphism|homomorphism|coset|"
    r"dynamic_programming|bfs|dfs|quicksort|mergesort|heapsort|"
    r"algorithm|recursion|induction|axiom|lemma|theorem|corollary|"
    r"definition|proposition|proof|metric|norm|inner_product|"
    r"vector_space|matrix|determinant|eigenspace|rank|kernel|"
    r"entropy_rate|mutual_information|channel_capacity|"
    r"lagrangian|hamiltonian|partition_function|"
    r"reinforcement_learning|policy|reward|value_function|"
    r"regularization|overfitting|underfitting|bias|variance)$",
    re.I
)


def is_protected(concept_id: str, node: dict) -> bool:
    """Nodes that must NEVER be removed."""
    if concept_id.startswith("flux_"):
        return True
    if node.get("source") in {"seed"}:
        return True
    # Known-good short technical terms
    if _KNOWN_GOOD_RE.match(concept_id):
        return True
    return False


def is_garbage(concept_id: str, node: dict) -> bool:
    """Return True if this node should be removed."""
    if is_protected(concept_id, node):
        return False

    tokens = concept_id.split("_")

    # Too many tokens = sentence fragment
    if len(tokens) > _MAX_TOKENS:
        return True

    # Starts with stopword
    if tokens[0].lower() in _STOPWORDS:
        return True

    # Fragment patterns
    for pat in _FRAG_PATTERNS:
        if pat.search(concept_id):
            return True

    # Trivially short or purely numeric
    if len(concept_id) <= 2 or concept_id.replace("_","").isdigit():
        return True

    # Has a definition longer than 20 chars? Strong signal it's real
    defn = node.get("definition", "") or ""
    if len(defn) > 50 and len(tokens) <= 3:
        return False  # has a real definition and short ID → keep

    # Short concept (≤2 tokens) with no definition → probably garbage
    if len(tokens) <= 2 and len(defn) < 15:
        return True

    return False


def clean_kg(store: KGStore, dry_run: bool = False) -> dict:
    all_ids = store.all_ids()
    to_remove, to_keep = [], []

    for nid in all_ids:
        node = store.get(nid)
        if not node:
            continue
        (to_remove if is_garbage(nid, node) else to_keep).append(nid)

    log.info("Audit: %d total | %d garbage | %d clean",
             len(all_ids), len(to_remove), len(to_keep))

    if dry_run:
        print(f"\nWould REMOVE {len(to_remove)}:")
        for nid in sorted(to_remove)[:30]: print(f"  - {nid}")
        print(f"\nWould KEEP {len(to_keep)}:")
        non_flux = [k for k in sorted(to_keep) if not k.startswith("flux_")]
        for nid in non_flux[30:55]: print(f"  + {nid}")
        return {"would_remove": len(to_remove), "would_keep": len(to_keep)}

    kg_path = Path("logs/flux_kg_live.jsonl")
    if not kg_path.exists():
        return {"error": "KG not found"}

    backup = kg_path.with_suffix(f".backup_{int(time.time())}.jsonl")
    shutil.copy2(kg_path, backup)
    log.info("Backup: %s", backup)

    clean_set = set(to_keep)
    kept = removed = 0
    with open(kg_path) as f:
        lines = f.readlines()
    with open(kg_path, "w") as f:
        for line in lines:
            try:
                e = json.loads(line.strip())
                cid = (e.get("concept_id") or e.get("id") or "").strip()
                if not cid or cid in clean_set:
                    f.write(line); kept += 1
                else:
                    removed += 1
            except json.JSONDecodeError:
                f.write(line); kept += 1

    log.info("Done: %d kept lines, %d removed lines", kept, removed)
    return {"removed": len(to_remove), "kept": len(to_keep)}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    stats = clean_kg(KGStore(), dry_run=args.dry_run)
    print(f"\nResult: {stats}")
