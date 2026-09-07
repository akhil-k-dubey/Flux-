"""
flux/training/book_trainer/book_trainer.py

Main pipeline orchestrator — the entry point for book training.

Usage:
    python3 -m flux.training.book_trainer.book_trainer --book path/to/book.pdf --domain thermodynamics
    python3 -m flux.training.book_trainer.book_trainer --all  # process all books in priority order

Pipeline per book:
  1. Extract raw text + structure (PDFExtractor)
  2. Extract explicit concepts + formulas (ConceptExtractor)
  3. Generate edges — the learning step (DependencyMapper)
  4. Write concepts to KG (KGWriter)
  5. Write edges to KG (KGWriter)
  6. Update WorldModel (domains, node count, books processed)
  7. Run validation gate — stop if win_rate < 90%
  8. [After full book batch] Stage-B LTC training with EWC
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

log = logging.getLogger("book_trainer")
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    datefmt="%H:%M:%S",
)

# ── Book manifest (priority order) ────────────────────────────────────────────
BOOKS_DIR = Path("/home/akhil/Downloads/BOOKS")

BOOK_MANIFEST: list[dict] = [
    # Phase 1 — Math foundations (everything depends on this)
    {"path": "maths/pdfcoffee.com_tao-t-analysis-1-4-pdf-free.pdf",
     "domain": "mathematics",         "title": "Tao Analysis I"},
    {"path": "maths/kupdf.net_analysis-ii-terence-tao-pdf.pdf",
     "domain": "mathematics",         "title": "Tao Analysis II"},
    {"path": "maths/LADR4e.pdf",
     "domain": "linear_algebra",      "title": "Axler Linear Algebra Done Right"},
    {"path": "maths/pdfcoffee.com_david-s-dummit-richard-m-foote-abstract-algebra-2004-wiley-pdf-free.pdf",
     "domain": "abstract_algebra",    "title": "Dummit Foote Abstract Algebra"},
    {"path": "maths/9780201558029.pdf",
     "domain": "mathematics",         "title": "Concrete Mathematics Knuth"},
    {"path": "maths/Topology - I (7MMA3C2).pdf",
     "domain": "mathematics",         "title": "Topology I"},

    # Phase 2 — Information + Category Theory (bridge math → physics + CS)
    {"path": "maths/Elements_of_Information_Theory_Elements.pdf",
     "domain": "information_theory",  "title": "Cover Thomas Information Theory"},
    {"path": "maths/category-theory-for-programmers_compress.pdf",
     "domain": "abstract_algebra",    "title": "Category Theory for Programmers"},

    # Phase 3 — Physics (builds on math)
    {"path": "Physics/The Feynman Lectures on Physics, Vol. I,II,III The New Millennium Edition by Richard P. Feynman (z-lib.org).pdf",
     "domain": "physics",             "title": "Feynman Lectures I-II-III"},
    {"path": "Physics/Griffiths.pdf",
     "domain": "quantum_mechanics",   "title": "Griffiths Introduction to Quantum Mechanics"},
    {"path": "Physics/pdfcoffee.com_statistical-mechanics-3rd-editionpathria-pdf-free.pdf",
     "domain": "statistical_mechanics","title": "Pathria Statistical Mechanics"},
    {"path": "Physics/carroll-gr-textbook.pdf",
     "domain": "general_relativity",  "title": "Carroll General Relativity"},
    {"path": "Physics/pdfcoffee.com_an-introduction-to-quantum-field-theory-peskinschroederpdf-5-pdf-free.pdf",
     "domain": "quantum_mechanics",   "title": "Peskin Schroeder QFT"},
    {"path": "Physics/PH409[Aug2013].pdf",
     "domain": "physics",             "title": "PH409 Advanced Physics"},

    # Phase 4 — AI/ML (builds on math + physics)
    {"path": "AI and ML/pdfcoffee.com_bishop-pattern-recognition-and-machine-learning-pdf-free.pdf",
     "domain": "machine_learning",    "title": "Bishop PRML"},
    {"path": "AI and ML/ESLII_print12_toc.pdf",
     "domain": "machine_learning",    "title": "Hastie ESL"},
    {"path": "AI and ML/deep-learning-1nbsped-0262035618-9780262035613.pdf",
     "domain": "machine_learning",    "title": "Goodfellow Deep Learning"},
    {"path": "AI and ML/pdfcoffee.com_probabilistic-graphical-models-principles-and-techniques-pdf-free.pdf",
     "domain": "probabilistic_ml",    "title": "Koller Probabilistic Graphical Models"},
    {"path": "AI and ML/SuttonBartoIPRLBook2ndEd.pdf",
     "domain": "reinforcement_learning","title": "Sutton Barto Reinforcement Learning"},

    # Phase 5 — CS foundations
    {"path": "ComputerScience/The Art of Computer Programming, Volume 1 PDF.pdf",
     "domain": "algorithms",          "title": "Knuth TAOCP Vol 1"},
    {"path": "ComputerScience/Addison.Wesley.Donald.E.Knuth.The.Art.of.Computer.Programming.Volume.2.pdf",
     "domain": "algorithms",          "title": "Knuth TAOCP Vol 2"},
    {"path": "ComputerScience/donald-e-knuth-the-art-of-computer-programming-vol-3.pdf",
     "domain": "algorithms",          "title": "Knuth TAOCP Vol 3"},
    {"path": "ComputerScience/The Art of Computer Programming, Volume 4, Fascicle 6 Satisfiability (Donald E. Knuth) (z-lib.org).pdf",
     "domain": "algorithms",          "title": "Knuth TAOCP Vol 4 SAT"},
    {"path": "ComputerScience/Algorithms2022-[www.konkur.in].pdf",
     "domain": "algorithms",          "title": "Algorithms 2022"},
    {"path": "Coding (Thinking + learning)/sicp.pdf",
     "domain": "algorithms",          "title": "SICP"},
    {"path": "ComputerScience/compilers-principles-techniques-and-tools-1292024348-9781292024349_compress.pdf",
     "domain": "compiler_theory",     "title": "Dragon Book Compilers"},
    {"path": "ComputerScience/pierce_types_and_programming_languages_2002.pdf",
     "domain": "type_theory",         "title": "Pierce Types and Programming Languages"},
    {"path": "ComputerScience/Operating Systems_ Three Easy Pieces ( PDFDrive ).pdf",
     "domain": "operating_systems",   "title": "OS Three Easy Pieces"},
    {"path": "ComputerScience/computer-networks-tanenbaum-5th-edition.pdf",
     "domain": "operating_systems",   "title": "Tanenbaum Computer Networks"},

    # Phase 6 — Practical coding (last — apply theory)
    {"path": "Coding (Thinking + learning)/taoup.pdf",
     "domain": "software_engineering","title": "TAOUP Unix Programming"},
    {"path": "Coding(Tricks)/pdfcoffee.com_fluent-python-clear-concise-and-effective-programming-luciano-ramalho-pdf-free.pdf",
     "domain": "python",              "title": "Fluent Python"},
    {"path": "Coding(Tricks)/PythonTricksBookbyDanBader-1.pdf",
     "domain": "python",              "title": "Python Tricks"},
    {"path": "Coding(Tricks)/NumericalPython_A_Practical_Techniques_Approach_fo.pdf",
     "domain": "python",              "title": "Numerical Python"},
]


class BookTrainer:
    """
    Main orchestrator for the book training pipeline.
    """

    def __init__(self):
        # Lazy import to avoid loading everything on import
        from flux.kg.store import KGStore, get_store
        from .pdf_extractor     import PDFExtractor
        from .concept_extractor import ConceptExtractor
        from .dependency_mapper  import DependencyMapper
        from .kg_writer         import KGWriter
        from .validation_gate   import ValidationGate

        self.store     = get_store()
        self.extractor = PDFExtractor()
        self.concept_x = ConceptExtractor()
        self.kg_writer = KGWriter(self.store)
        self.validator = ValidationGate(self.store, threshold=0.90)

        # Load WorldModel for live updates
        try:
            from flux.nervous_system.world_model import WorldModel
            self._wm = WorldModel.instance()
        except Exception:
            self._wm = None

        self._processed_books: list[str] = []
        self._total_concepts  = 0
        self._total_edges     = 0

    def train_book(self, book_path: str, domain: str, title: str) -> dict:
        """
        Process a single book through the full pipeline.

        Returns
        -------
        dict with stats: concepts, edges, validation_passed, etc.
        """
        full_path = BOOKS_DIR / book_path
        if not full_path.exists():
            log.warning("Book not found: %s", full_path)
            return {"error": f"File not found: {full_path}"}

        log.info("═" * 60)
        log.info("📖 Processing: %s", title)
        log.info("   Domain: %s | Pages: ?", domain)
        log.info("═" * 60)
        t0 = time.time()

        # Step 1: Extract raw structure from PDF
        log.info("[1/5] Extracting PDF structure...")
        book = self.extractor.extract(str(full_path), domain=domain)
        log.info("      %d pages extracted", book.page_count)

        # Step 2: Extract concepts (strict mode)
        log.info("[2/5] Extracting concepts (strict mode)...")
        concepts = self.concept_x.extract_from_book_pages(
            book.pages, domain=domain, book_title=title
        )
        log.info("      %d concepts found", len(concepts))

        # Step 3: Generate edges (learning step)
        log.info("[3/5] Mapping dependencies (learning, not memorizing)...")
        from .dependency_mapper import DependencyMapper
        mapper = DependencyMapper(existing_kg_ids=set(self.store.all_ids()))
        edges  = mapper.map_edges(concepts, book_domain=domain)
        log.info("      %d edges generated (REQUIRES + ENABLES + ANALOGOUS_TO + ...)", len(edges))

        # Step 4: Write to KG (concepts + edges in one atomic pass)
        log.info("[4/5] Writing to KGStore...")
        concepts_written, edges_written = self.kg_writer.write_concepts_with_edges(concepts, edges)
        log.info("      Concepts: %d new | Edges: %d new", concepts_written, edges_written)

        # Step 5: Validation gate
        log.info("[5/5] Running validation gate...")
        passed, report = self.validator.check(title)
        print(self.validator.format_report(report))

        elapsed = round(time.time() - t0, 1)

        # Update WorldModel
        self._update_world_model(title, domain, concepts_written, edges_written, passed)

        self._total_concepts += concepts_written
        self._total_edges    += edges_written
        self._processed_books.append(title)

        result = {
            "book":               title,
            "domain":             domain,
            "pages":              book.page_count,
            "concepts_extracted": len(concepts),
            "concepts_written":   concepts_written,
            "edges_written":      edges_written,
            "validation_passed":  passed,
            "win_rate":           report["win_rate"],
            "elapsed_s":          elapsed,
        }

        if not passed:
            log.error("🛑 STOPPING — identity preservation below 90%%")
            log.error("   Manual review required before continuing.")

        return result

    def train_all(self, start_from: str | None = None) -> list[dict]:
        """
        Process all books in priority order.

        Parameters
        ----------
        start_from : book title to resume from (skip earlier books)
        """
        results = []
        skipping = start_from is not None

        for book_info in BOOK_MANIFEST:
            title = book_info["title"]

            # Resume support
            if skipping:
                if title == start_from:
                    skipping = False
                else:
                    log.info("Skipping (resume mode): %s", title)
                    continue

            result = self.train_book(
                book_info["path"],
                book_info["domain"],
                title,
            )
            results.append(result)

            # Stop if validation failed
            if result.get("error") or not result.get("validation_passed", True):
                log.error("Training halted at: %s", title)
                break

        # Print final summary
        self._print_summary(results)
        return results

    def _update_world_model(
        self,
        title: str,
        domain: str,
        concepts_written: int,
        edges_written: int,
        passed: bool,
    ) -> None:
        """Update WorldModel with current training state."""
        if not self._wm:
            return
        try:
            all_ids  = self.store.all_ids()
            domains: dict[str, int] = {}
            for nid in all_ids:
                node = self.store.get(nid)
                d    = (node or {}).get("domain", "unknown")
                domains[d] = domains.get(d, 0) + 1

            self._wm.update("knowledge.kg_nodes", len(all_ids))
            self._wm.update("knowledge.domains",  domains)

            # Track processed books
            processed = self._wm.get("knowledge.books_processed", [])
            if title not in processed:
                processed.append(title)
            self._wm.update("knowledge.books_processed", processed)

            # Update capabilities
            caps = self._wm.get("self.capabilities.confirmed", [])
            if domain not in caps:
                caps.append(domain)
                self._wm.update("self.capabilities.confirmed", caps)

        except Exception as exc:
            log.debug("WorldModel update failed: %s", exc)

    def _print_summary(self, results: list[dict]) -> None:
        """Print a summary table of all processed books."""
        print("\n" + "═" * 70)
        print("  BOOK TRAINING SUMMARY")
        print("═" * 70)
        print(f"  {'Book':<40} {'Concepts':>9} {'Edges':>6} {'Validated':>9}")
        print("─" * 70)
        for r in results:
            if "error" in r:
                print(f"  {'ERROR: ' + r['book']:<40} {'ERROR':>9}")
                continue
            status = "✅" if r["validation_passed"] else "❌"
            print(f"  {r['book']:<40} {r['concepts_written']:>9} {r['edges_written']:>6} {status:>9}")
        print("─" * 70)
        total_c = sum(r.get("concepts_written", 0) for r in results)
        total_e = sum(r.get("edges_written", 0) for r in results)
        print(f"  {'TOTAL':<40} {total_c:>9} {total_e:>6}")
        print("═" * 70)
        print(f"\n  KG grew by {total_c} concepts and {total_e} edges.")
        print(f"  WorldModel updated with {len(results)} books processed.\n")


# ── CLI entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Flux Book Trainer — KG extraction pipeline")
    parser.add_argument("--book",       help="Path to a single PDF (relative to BOOKS_DIR)")
    parser.add_argument("--domain",     default="unknown", help="Domain for single book")
    parser.add_argument("--title",      default="", help="Title for single book")
    parser.add_argument("--all",        action="store_true", help="Process all books in order")
    parser.add_argument("--resume",     help="Resume from this book title")
    parser.add_argument("--dry-run",    action="store_true", help="Extract only, don't write to KG")
    parser.add_argument("--list",       action="store_true", help="List all books in training order")
    args = parser.parse_args()

    if args.list:
        print("\nBooks in training order:")
        for i, b in enumerate(BOOK_MANIFEST, 1):
            full = BOOKS_DIR / b["path"]
            exists = "✓" if full.exists() else "✗"
            print(f"  {i:2}. {exists} [{b['domain']:<25}] {b['title']}")
        return

    trainer = BookTrainer()

    if args.all:
        trainer.train_all(start_from=args.resume)
    elif args.book:
        result = trainer.train_book(args.book, args.domain, args.title or Path(args.book).stem)
        print(json.dumps(result, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
