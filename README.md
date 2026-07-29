# FLUX: Fluid Logic & Knowledge Representation System

FLUX is a recursive knowledge representation, liquid neural network architecture, and symbolic reasoning engine.

## Requirements & Prerequisites

### Python Version
- **Python >= 3.10** (Recommended: Python 3.11, 3.12, or 3.13)

### Required Pip Packages
The project dependencies are listed in `requirements.txt`:

- **`torch`** (`>= 2.0.0`): Core deep learning framework for Liquid Transformer cells, field encoders, and consolidation models.
- **`z3-solver`** (`>= 4.12.0`): SMT constraint solver integration for symbolic verification and reasoning (`flux/reasoning/z3_bridge.py`).
- **`clingo`** (`>= 5.6.0`): Answer Set Programming (ASP) solver integration for rule-based logic evaluation (`flux/reasoning/clingo_bridge.py`).
- **`numpy`** (`>= 1.24.0`): Numerical tensor and matrix utility calculations.

---

## Installation Guide

1. **Clone the Repository**
   ```bash
   git clone https://github.com/akhil-k-dubey/Flux-.git
   cd Flux-
   ```

2. **Set up a Virtual Environment (Recommended)**
   ```bash
   python -m venv venv
   # On Windows (PowerShell):
   .\venv\Scripts\Activate.ps1
   # On Linux/macOS:
   source venv/bin/activate
   ```

3. **Install Required Packages**
   ```bash
   pip install -r requirements.txt
   ```

---

## Project Structure Overview

- `flux/model/`: Liquid Transformer, Liquid Cell, Field Encoder, Embedding, and Model Trainer.
- `flux/reasoning/`: Logic inference engines (Z3 SMT Solver & Clingo ASP bridges).
- `flux/kg/`: Knowledge Graph storage, indexing, and consistency engine.
- `flux/query/`: Query parser, traversal engine, and ranking logic.
- `flux/language/`: Natural language intent parsing and response assembly.
- `flux/rsm/`: Recursive Self-Modification proposal generator, gap analysis, and approval handler.
- `checkpoints/`: Trained model checkpoints (`.pt` files).
- `logs/`: Conversation transcripts, interaction logs, and knowledge graph commits.