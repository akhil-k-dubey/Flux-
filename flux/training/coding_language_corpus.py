"""
flux/training/coding_language_corpus.py

Generates 200 training sessions (coding + language + self-knowledge) in the
same [TEACH]...[/TEACH] format as existing grounded sessions.

EWC protects existing knowledge during subsequent grounded_trainer runs.

Usage:
    python3 -m flux.training.coding_language_corpus
"""
from __future__ import annotations
import array, base64, json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

OUT_PATH = ROOT / "logs" / "coding_language_sessions.jsonl"

# ── Load character vocab ───────────────────────────────────────────────────────

def _load_vocab():
    vp = ROOT / "logs" / "flux_vocab.json"
    v  = json.loads(vp.read_text())
    return v["char_to_idx"]

def _encode(text: str, c2i: dict) -> str:
    """text → base64 uint16-LE character indices (same as grounded sessions)."""
    indices = [c2i.get(ch, 0) for ch in text]
    buf = array.array("H", indices)
    return base64.b64encode(buf.tobytes()).decode()

def _session(idx: int, category: str, concept_id: str, concept_name: str,
             domain: str, definition: str, examples: str, relations: str,
             conf: float, complexity: int, c2i: dict) -> dict:
    text = (
        f"[TEACH]\n"
        f"id: {concept_id}\n"
        f"concept: {concept_name}\n"
        f"type: CONCEPT_KNOWLEDGE\n"
        f"domain: {domain}\n"
        f"definition: {definition}\n"
        f"examples: {examples}\n"
        f"relations: {relations}\n"
        f"confidence: {conf}\n"
        f"[/TEACH]"
    )
    return {
        "session_id":        f"{category}_{idx:04d}_{concept_id}",
        "source":            "GROUNDED",
        "char_sequence_b64": _encode(text, c2i),
        "char_len":          len(text),
        "conf_target":       conf,
        "active":            True,
        "weight":            0.85,
        "complexity_rank":   complexity,
        "grounding_source":  f"{category}_corpus",
    }

# ── Concept definitions ────────────────────────────────────────────────────────

CONCEPTS = [

  # ── Core CS (40) ────────────────────────────────────────────────────────────
  ("coding", "recursion", "recursion", "computer_science",
   "A function that calls itself with a smaller subproblem until reaching a base case. The call stack grows with each recursive call and unwinds when base case is reached.",
   "factorial(n)=n*factorial(n-1), fibonacci, tree traversal, merge_sort",
   "requires: base_case, enables: divide_and_conquer, connects: call_stack", 0.85, 3),

  ("coding", "big_o_notation", "big-O notation", "computer_science",
   "Asymptotic notation describing the upper bound on time or space complexity of an algorithm as input size n grows. O(1) constant, O(log n) logarithmic, O(n) linear, O(n^2) quadratic.",
   "binary_search is O(log n), bubble_sort is O(n^2), hash_lookup is O(1)",
   "connects: time_complexity, space_complexity, algorithm_analysis", 0.85, 4),

  ("coding", "dynamic_programming", "dynamic programming", "computer_science",
   "Optimization technique that solves complex problems by breaking them into overlapping subproblems and storing results (memoization or tabulation) to avoid redundant computation.",
   "fibonacci with memoization, longest_common_subsequence, knapsack_problem",
   "requires: optimal_substructure, overlapping_subproblems, enables: memoization, tabulation", 0.85, 6),

  ("coding", "hash_table", "hash table", "computer_science",
   "Data structure mapping keys to values via a hash function. Average O(1) lookup, insert, delete. Collisions resolved by chaining or open addressing. Load factor affects performance.",
   "Python dict, Java HashMap, cache implementations",
   "requires: hash_function, collision_resolution, connects: amortized_analysis", 0.85, 4),

  ("coding", "binary_search", "binary search", "computer_science",
   "Search algorithm that finds an element in a sorted array in O(log n) time by repeatedly halving the search space. Requires sorted input. Left/right pointer approach.",
   "finding element in sorted list, lower_bound in competitive programming",
   "requires: sorted_array, enables: O_log_n_search", 0.80, 2),

  ("coding", "bfs_dfs", "BFS and DFS", "computer_science",
   "Graph traversal algorithms. BFS uses a queue and explores level by level, finding shortest paths in unweighted graphs. DFS uses a stack (or recursion) and explores deep before backtracking.",
   "BFS: shortest path in maze, DFS: topological sort, cycle detection",
   "requires: graph, queue_or_stack, enables: shortest_path, topological_sort", 0.85, 5),

  ("coding", "quicksort", "quicksort", "computer_science",
   "Divide and conquer sort. Pick a pivot, partition array into elements less and greater than pivot, recursively sort partitions. Average O(n log n), worst case O(n^2) with bad pivot choice.",
   "in-place sorting, Lomuto partition, Hoare partition",
   "requires: partition, recursion, connects: divide_and_conquer", 0.80, 5),

  ("coding", "merge_sort", "merge sort", "computer_science",
   "Stable divide-and-conquer sorting algorithm. Split array in half, recursively sort each half, merge sorted halves. O(n log n) guaranteed. Requires O(n) extra space.",
   "sorting linked lists, external sorting, stable sort requirement",
   "requires: divide_and_conquer, merge_operation, enables: stable_sort", 0.85, 5),

  ("coding", "graph_representation", "graph representation", "computer_science",
   "Graphs stored as adjacency matrix (O(V^2) space, O(1) edge lookup) or adjacency list (O(V+E) space, efficient for sparse graphs). Directed vs undirected, weighted vs unweighted.",
   "road networks, social graphs, dependency graphs",
   "connects: BFS, DFS, shortest_path_algorithms", 0.80, 4),

  ("coding", "greedy_algorithm", "greedy algorithm", "computer_science",
   "Algorithm that makes locally optimal choices at each step hoping to find a global optimum. Works when problem has greedy choice property and optimal substructure. Not always globally optimal.",
   "Huffman coding, Dijkstra shortest path, activity selection problem",
   "connects: dynamic_programming, optimal_substructure", 0.80, 5),

  ("coding", "stack_queue", "stack and queue", "computer_science",
   "Stack: LIFO (last in first out) data structure. O(1) push/pop. Used for function calls, undo operations. Queue: FIFO (first in first out). O(1) enqueue/dequeue. Used for BFS, task scheduling.",
   "Python list as stack, collections.deque as queue, call_stack in recursion",
   "enables: BFS, DFS, function_call_management", 0.80, 2),

  ("coding", "linked_list", "linked list", "computer_science",
   "Linear data structure where elements (nodes) contain data and a pointer to the next node. O(1) insert/delete at known position, O(n) search. Singly or doubly linked. No random access.",
   "implementing stacks and queues, LRU cache with hashmap",
   "connects: pointer_arithmetic, memory_allocation", 0.80, 3),

  ("coding", "binary_tree", "binary tree", "computer_science",
   "Tree where each node has at most 2 children (left, right). BST property: left subtree < root < right subtree enables O(log n) search if balanced. AVL and red-black trees maintain balance.",
   "BST search, heap as complete binary tree, expression trees",
   "requires: tree_node, enables: BST, heap, tree_traversal", 0.85, 4),

  ("coding", "heap_priority_queue", "heap and priority queue", "computer_science",
   "Complete binary tree satisfying heap property: max-heap (parent >= children) or min-heap (parent <= children). O(log n) insert and extract. Underlying structure for priority queues.",
   "Dijkstra algorithm, heap sort, k largest elements",
   "requires: complete_binary_tree, enables: priority_queue, heap_sort", 0.85, 5),

  ("coding", "memoization", "memoization", "computer_science",
   "Optimization technique storing results of expensive function calls and returning cached results when same inputs occur again. Top-down approach to dynamic programming. Trades space for time.",
   "recursive fibonacci with cache, Python functools.lru_cache decorator",
   "requires: pure_function, enables: dynamic_programming", 0.85, 4),

  ("coding", "time_complexity", "time complexity", "computer_science",
   "Measure of how runtime scales with input size. Expressed with big-O notation. Best case, average case, worst case. Empirically measured but theoretically bounded.",
   "O(n^2) bubble sort vs O(n log n) merge sort for large arrays",
   "connects: big_o_notation, space_complexity, algorithm_selection", 0.85, 4),

  ("coding", "space_complexity", "space complexity", "computer_science",
   "Measure of memory used by an algorithm as input grows. Includes input space, auxiliary space, and call stack space for recursive algorithms. Often a tradeoff with time complexity.",
   "in-place sort O(1) space vs merge sort O(n) space",
   "connects: time_complexity, big_o_notation, memory_management", 0.80, 4),

  ("coding", "amortized_analysis", "amortized analysis", "computer_science",
   "Analysis technique for algorithms where occasional expensive operations are averaged over many cheap ones. Used for dynamic arrays (append is amortized O(1) despite occasional O(n) resize).",
   "Python list.append, hash table resize, splay tree operations",
   "connects: time_complexity, data_structures", 0.75, 7),

  ("coding", "divide_and_conquer", "divide and conquer", "computer_science",
   "Algorithm paradigm: divide problem into smaller subproblems, solve recursively, combine results. Master theorem gives time complexity. Base cases terminate recursion.",
   "merge sort, quick sort, binary search, fast Fourier transform",
   "requires: recursion, enables: merge_sort, binary_search, quicksort", 0.85, 5),

  ("coding", "sliding_window", "sliding window technique", "computer_science",
   "Technique for problems involving subarrays or substrings. Maintain a window of elements and slide it through the array. Reduces O(n^2) brute force to O(n) for many substring problems.",
   "maximum sum subarray of size k, longest substring without repeating characters",
   "connects: two_pointer, array_techniques", 0.80, 4),

  # ── Python-specific (30) ─────────────────────────────────────────────────────
  ("coding", "python_decorator", "Python decorator", "python",
   "Higher-order function that wraps another function to extend its behavior without modifying it. Syntactic sugar for: func = decorator(func). Used for logging, caching, timing, auth.",
   "@lru_cache, @property, @staticmethod, custom timing decorator",
   "requires: higher_order_function, closures, enables: aspect_oriented_programming", 0.85, 5),

  ("coding", "python_generator", "Python generator", "python",
   "Function using yield instead of return. Produces values lazily, one at a time, without storing entire sequence in memory. Generator expressions are like list comprehensions but lazy.",
   "range(), itertools, reading large files line by line, fibonacci_generator",
   "requires: iterator_protocol, enables: lazy_evaluation, memory_efficiency", 0.85, 5),

  ("coding", "python_context_manager", "Python context manager", "python",
   "Object implementing __enter__ and __exit__ for setup and teardown. Used with 'with' statement. Guarantees cleanup even if exceptions occur. contextlib.contextmanager simplifies creation.",
   "with open(file) as f, database transactions, lock acquisition",
   "requires: protocol___enter____exit__, enables: resource_management", 0.85, 4),

  ("coding", "python_comprehension", "Python list and dict comprehension", "python",
   "Concise syntax for creating lists, dicts, sets from iterables. [expr for x in iterable if condition]. More readable and often faster than equivalent for loops.",
   "[x**2 for x in range(10)], {k: v for k,v in dict.items()}, {x for x in lst}",
   "connects: functional_programming, iterables", 0.85, 3),

  ("coding", "python_oop", "Python object-oriented programming", "python",
   "Python supports OOP with classes, inheritance, polymorphism, encapsulation. __init__ constructor. super() for parent class calls. Multiple inheritance via MRO (method resolution order).",
   "class Dog(Animal): inherits speak(), dunder methods for operators",
   "requires: class_definition, enables: inheritance, polymorphism, encapsulation", 0.85, 4),

  ("coding", "python_exception_handling", "Python exception handling", "python",
   "try/except/else/finally blocks. Specific exception types: ValueError, TypeError, KeyError. raise to throw. Custom exceptions via class MyError(Exception). Context determines catch breadth.",
   "try: risky_op() except ValueError as e: handle(e) finally: cleanup()",
   "enables: error_recovery, defensive_programming", 0.80, 3),

  ("coding", "python_type_hints", "Python type hints", "python",
   "Optional type annotations using : and -> syntax. Checked by mypy, not enforced at runtime. Improves readability and enables static analysis. Generic types: List[int], Dict[str, Any], Optional[X].",
   "def greet(name: str) -> str:, from typing import List Optional",
   "connects: static_analysis, documentation, mypy", 0.80, 3),

  ("coding", "python_async", "Python async/await", "python",
   "Concurrency model based on coroutines. async def creates coroutines, await yields control. asyncio event loop runs coroutines. I/O-bound workloads benefit most. Not parallelism.",
   "async web scraping with aiohttp, async database queries",
   "requires: event_loop, enables: concurrent_io, connects: asyncio", 0.80, 6),

  ("coding", "python_closures", "Python closures", "python",
   "Function that captures variables from its enclosing scope even after the outer function has returned. The captured variables persist in the closure's __closure__ attribute.",
   "counter = make_counter(), decorator factories, callback with state",
   "requires: lexical_scoping, enables: decorators, factory_functions", 0.80, 5),

  ("coding", "python_gil", "Python GIL", "python",
   "Global Interpreter Lock: mutex in CPython preventing multiple threads from executing Python bytecode simultaneously. Limits CPU parallelism but protects object reference counting. Use multiprocessing for CPU-bound tasks.",
   "threading for I/O-bound, multiprocessing for CPU-bound, Cython to release GIL",
   "connects: threading, multiprocessing, performance_limitations", 0.75, 6),

  ("coding", "python_iterators", "Python iterators and iterables", "python",
   "Iterable: object with __iter__ returning iterator. Iterator: object with __next__ raising StopIteration when exhausted. For loops use this protocol internally.",
   "custom iterator class, iter(), next(), range object",
   "enables: for_loop_protocol, generators, comprehensions", 0.80, 4),

  ("coding", "python_lambda", "Python lambda functions", "python",
   "Anonymous single-expression functions. lambda args: expression. Limited to one expression, no statements. Common with map, filter, sorted key argument.",
   "sorted(lst, key=lambda x: x[1]), map(lambda x: x*2, nums)",
   "connects: functional_programming, higher_order_functions", 0.75, 2),

  # ── ML/DL Engineering (40) ──────────────────────────────────────────────────
  ("coding", "gradient_descent", "gradient descent", "machine_learning",
   "Optimization algorithm minimizing loss by moving parameters in the direction of steepest descent (negative gradient). Learning rate controls step size. Variants: SGD, Adam, AdamW.",
   "θ = θ - lr * ∂L/∂θ, mini-batch SGD in neural network training",
   "requires: gradient, loss_function, enables: neural_network_training", 0.90, 5),

  ("coding", "adam_optimizer", "Adam optimizer", "machine_learning",
   "Adaptive learning rate optimizer combining momentum (first moment) and RMSprop (second moment). Maintains per-parameter learning rates. beta1=0.9, beta2=0.999, epsilon=1e-8 typical.",
   "most common optimizer for neural networks, AdamW adds weight decay",
   "requires: gradient_descent, enables: fast_convergence, adaptive_lr", 0.85, 5),

  ("coding", "dropout_regularization", "dropout regularization", "machine_learning",
   "During training, randomly zero out activations with probability p. Forces network to learn redundant representations. At inference multiply by (1-p) or use inverted dropout scaling.",
   "nn.Dropout(0.1), prevents co-adaptation of neurons, reduces overfitting",
   "requires: overfitting_risk, enables: regularization, generalization", 0.85, 4),

  ("coding", "learning_rate_schedule", "learning rate scheduling", "machine_learning",
   "Strategy for changing learning rate during training. Cosine annealing: lr follows cosine from lr_max to lr_min. Warmup: linearly increase lr for first N steps. Reduces loss more effectively than constant lr.",
   "cosine_annealing, warmup_cosine, step_decay, ReduceLROnPlateau",
   "requires: optimizer, enables: better_convergence, escaping_local_minima", 0.90, 5),

  ("coding", "elastic_weight_consolidation", "EWC elastic weight consolidation", "machine_learning",
   "Continual learning technique preventing catastrophic forgetting. Adds penalty term weighted by Fisher information matrix to loss: L_EWC = L_task + lambda * sum(F_i*(theta_i - theta*_i)^2). Protects important weights.",
   "Flux uses EWC to learn new concepts without forgetting old ones",
   "requires: fisher_information, enables: continual_learning, catastrophic_forgetting_prevention", 0.90, 7),

  ("coding", "attention_mechanism", "attention mechanism", "machine_learning",
   "Allows models to focus on relevant parts of input. Query, Key, Value matrices. Attention(Q,K,V) = softmax(QK^T/sqrt(d_k))V. Self-attention relates positions in same sequence. Multi-head extends to multiple subspaces.",
   "transformer attention, Flux uses attention for context weighting",
   "requires: query_key_value, enables: transformer, self_attention", 0.90, 7),

  ("coding", "residual_connections", "residual connections (skip connections)", "machine_learning",
   "Adding input of a layer to its output: output = layer(x) + x. Enables training very deep networks by providing gradient shortcuts. Prevents vanishing gradient. Used in ResNet, transformer blocks.",
   "ResNet skip connections, transformer FFN residuals",
   "requires: deep_network, enables: gradient_flow, very_deep_training", 0.85, 6),

  ("coding", "embeddings", "embeddings", "machine_learning",
   "Dense vector representations of discrete objects (words, tokens, nodes). Learned during training. Similar objects cluster in embedding space. Dimensionality much lower than one-hot encoding.",
   "word2vec, token embeddings in transformers, KG node embeddings",
   "requires: vector_space, enables: semantic_similarity, neural_input", 0.90, 5),

  ("coding", "tokenization", "tokenization", "machine_learning",
   "Breaking text into tokens (subword units, words, or characters). BPE (Byte Pair Encoding) and WordPiece create subword vocabularies. Character-level: each character is a token (used by Flux LTC).",
   "GPT uses BPE, Flux LTC uses character-level tokenization",
   "connects: vocabulary, embeddings, character_level_model", 0.85, 4),

  ("coding", "loss_function", "loss function", "machine_learning",
   "Measures discrepancy between model predictions and ground truth. BCE for binary classification. Cross-entropy for multi-class. MSE for regression. L1 for robust regression.",
   "Flux ConsolidationHead uses BCE loss, FLM uses cross-entropy",
   "requires: prediction, ground_truth, enables: gradient_computation, training", 0.90, 4),

  ("coding", "backpropagation", "backpropagation", "machine_learning",
   "Algorithm computing gradients of loss with respect to all parameters via chain rule applied backwards through the computation graph. Enables gradient-based optimization of neural networks.",
   "autograd in PyTorch, gradient tape in TensorFlow, chain rule application",
   "requires: chain_rule, computation_graph, enables: gradient_descent, training", 0.90, 6),

  ("coding", "transfer_learning", "transfer learning", "machine_learning",
   "Using a model trained on one task as starting point for a different task. Pretrain on large dataset, fine-tune on small task-specific dataset. Lower layers transfer better (general features).",
   "GPT pretrained on internet text, fine-tuned on specific tasks",
   "requires: pretrained_model, enables: fine_tuning, low_data_learning", 0.85, 6),

  ("coding", "fine_tuning", "fine-tuning", "machine_learning",
   "Continuing training of a pretrained model on new data with lower learning rate. Updates all parameters or only task-specific layers. LoRA: low-rank adapter matrices for efficient fine-tuning.",
   "fine-tuning BERT for sentiment, LoRA for LLM adaptation",
   "requires: pretrained_model, enables: task_adaptation, domain_adaptation", 0.90, 6),

  ("coding", "overfitting_underfitting", "overfitting and underfitting", "machine_learning",
   "Overfitting: model memorizes training data, fails to generalize. High train accuracy, low test accuracy. Underfitting: model too simple, fails to capture patterns. Both addressed by model selection and regularization.",
   "train_loss << val_loss → overfitting, both high → underfitting",
   "connects: regularization, validation_set, model_complexity", 0.90, 5),

  ("coding", "batch_norm", "batch normalization", "machine_learning",
   "Normalizes activations within a mini-batch to zero mean, unit variance, then applies learnable scale gamma and shift beta. Reduces internal covariate shift, enables higher learning rates.",
   "nn.BatchNorm1d, placed after linear layer, before activation typically",
   "requires: mini_batch, enables: faster_training, higher_lr", 0.85, 5),

  ("coding", "weight_initialization", "weight initialization", "machine_learning",
   "Setting initial parameter values before training. Xavier/Glorot initialization for tanh networks. He initialization for ReLU. Proper init prevents vanishing/exploding gradients at start.",
   "nn.init.xavier_uniform_, He init for conv layers, zero init for biases",
   "requires: network_architecture, enables: stable_training_start", 0.80, 5),

  ("coding", "regularization", "regularization", "machine_learning",
   "Techniques reducing overfitting: L1 (lasso, sparsity-inducing), L2 (ridge, weight decay), dropout, early stopping, data augmentation. Add constraints or noise to prevent memorization.",
   "weight_decay in optimizer, dropout layers, data augmentation for images",
   "enables: generalization, prevents: overfitting", 0.85, 5),

  ("coding", "model_evaluation", "model evaluation metrics", "machine_learning",
   "Accuracy (fraction correct), precision (TP/(TP+FP)), recall (TP/(TP+FN)), F1 (harmonic mean P and R), AUC-ROC. Loss values measure training quality. Confusion matrix gives full picture.",
   "precision-recall tradeoff, Flux uses benchmark_score and regression_passed",
   "requires: ground_truth_labels, enables: model_comparison, deployment_decision", 0.85, 5),

  ("coding", "gradient_vanishing_exploding", "vanishing and exploding gradients", "machine_learning",
   "Vanishing: gradients become exponentially small through deep layers, early layers learn slowly. Exploding: gradients grow exponentially, causing NaN loss. Solutions: residual connections, batch norm, gradient clipping, proper init.",
   "RNN training issues, deep network training instability",
   "connects: backpropagation, residual_connections, gradient_clipping", 0.80, 6),

  ("coding", "hyperparameter_tuning", "hyperparameter tuning", "machine_learning",
   "Searching for optimal hyperparameters (learning rate, batch size, architecture dims) that are not learned during training. Methods: grid search, random search, Bayesian optimization.",
   "Flux RSM proposes hyperparameter changes via architectural proposals",
   "connects: model_selection, cross_validation, automated_ml", 0.80, 5),

  # ── Software Engineering (20) ───────────────────────────────────────────────
  ("coding", "singleton_pattern", "singleton design pattern", "software_engineering",
   "Ensures a class has only one instance and provides global access to it. Implemented by making constructor private and returning same instance on every call. Thread safety requires locking.",
   "database connection pool, config manager, logger instances",
   "connects: design_patterns, global_state, thread_safety", 0.80, 4),

  ("coding", "observer_pattern", "observer design pattern", "software_engineering",
   "Defines one-to-many dependency so when one object (subject) changes state, all dependents (observers) are notified automatically. Decouples subject from observers. Used in event systems.",
   "event listeners in GUI, publish-subscribe messaging, reactive programming",
   "enables: event_driven_architecture, decoupling", 0.80, 5),

  ("coding", "factory_pattern", "factory design pattern", "software_engineering",
   "Creates objects without specifying exact class. Factory method: subclass decides which class to instantiate. Abstract factory: creates families of related objects. Decouples creation from use.",
   "UI component factories, database driver creation, parser factories",
   "enables: object_creation_decoupling, open_closed_principle", 0.80, 5),

  ("coding", "solid_principles", "SOLID principles", "software_engineering",
   "Five principles for OOP design: Single Responsibility (one reason to change), Open-Closed (open for extension, closed for modification), Liskov Substitution, Interface Segregation, Dependency Inversion.",
   "class design guidelines, refactoring checklist, code review criteria",
   "enables: maintainable_code, testable_design, extensibility", 0.85, 6),

  ("coding", "unit_testing", "unit testing", "software_engineering",
   "Testing individual components in isolation with known inputs and expected outputs. pytest for Python. Mocking replaces external dependencies. Test coverage measures which code is tested.",
   "pytest, unittest.mock, test fixtures, assert statements",
   "enables: regression_detection, refactoring_safety, documentation", 0.85, 4),

  ("coding", "debugging_strategies", "debugging strategies", "software_engineering",
   "Systematic approaches: read error messages carefully, reproduce minimal case, add print/logging statements, use debugger breakpoints, binary search which change broke it, rubber duck debugging.",
   "pdb debugger, logging module, git bisect for regression, minimal reproduction",
   "connects: error_handling, logging, testing", 0.80, 3),

  ("coding", "version_control", "version control (git)", "software_engineering",
   "Tracking changes to code over time. Git: commit (snapshot), branch (parallel development), merge (combine branches), rebase (linear history). Pull requests for code review before merging.",
   "git commit, git branch, git merge, git rebase, GitHub workflow",
   "enables: collaboration, change_history, rollback", 0.80, 3),

  ("coding", "api_design", "API design principles", "software_engineering",
   "Designing interfaces for components or services. REST: resources, HTTP verbs, stateless. Versioning, backwards compatibility, clear error messages, rate limiting. Documentation is essential.",
   "RESTful APIs, OpenAPI specification, semantic versioning",
   "connects: http_protocol, microservices, interface_design", 0.80, 5),

  ("coding", "code_refactoring", "code refactoring", "software_engineering",
   "Restructuring existing code without changing external behavior. Improves readability, reduces complexity, eliminates duplication. Extract method, rename variable, replace magic number with constant.",
   "DRY principle (dont repeat yourself), clean code, technical debt reduction",
   "connects: testing, code_quality, maintainability", 0.80, 4),

  ("coding", "logging_best_practices", "logging best practices", "software_engineering",
   "Structured logging with levels: DEBUG (verbose), INFO (key events), WARNING (unexpected but handled), ERROR (failures), CRITICAL. Include context: timestamp, session_id, component. Use log.debug not print.",
   "Python logging module, structured JSON logs, Flux uses log.info/warning",
   "connects: debugging, monitoring, observability", 0.85, 3),

  # ── Language and Reasoning (30) ─────────────────────────────────────────────
  ("language", "deductive_reasoning", "deductive reasoning", "epistemology",
   "Reasoning from general premises to specific conclusions. If premises are true and argument is valid, conclusion must be true. Contrast with inductive (bottom-up) and abductive (best explanation).",
   "All humans are mortal, Socrates is human, therefore Socrates is mortal",
   "connects: formal_logic, syllogism, enables: theorem_proving", 0.85, 5),

  ("language", "inductive_reasoning", "inductive reasoning", "epistemology",
   "Reasoning from specific observations to general conclusions. Conclusions are probable not certain. Basis of scientific method. Hume's problem: no logical justification for induction from past to future.",
   "Every swan observed was white, infer all swans are white (falsified by black swans)",
   "connects: scientific_method, bayesian_reasoning, enables: hypothesis_formation", 0.85, 5),

  ("language", "abductive_reasoning", "abductive reasoning", "epistemology",
   "Inference to the best explanation. Given observations, find the simplest hypothesis that would explain them. Peirce's third form of reasoning. Doctors diagnose via abduction.",
   "Patient has fever and rash, best explanation is measles",
   "connects: deductive, inductive, enables: diagnostic_reasoning", 0.80, 6),

  ("language", "counterfactual_thinking", "counterfactual thinking", "epistemology",
   "Reasoning about what would have happened under different conditions: if X had not occurred, then Y would not have followed. Fundamental to causal reasoning and regret. Lewis's possible worlds semantics.",
   "if I had studied harder, I would have passed; causal analysis in science",
   "connects: causal_reasoning, modal_logic, enables: policy_evaluation", 0.80, 6),

  ("language", "causal_reasoning", "causal reasoning", "epistemology",
   "Distinguishing correlation from causation. Pearl's do-calculus: P(Y|do(X)) vs P(Y|X). Three rungs: association, intervention, counterfactual. Causal graphs (DAGs) encode assumptions about mechanisms.",
   "smoking causes cancer vs smoking correlates with cancer, randomized controlled trials",
   "requires: causal_model, enables: intervention_planning, counterfactual_analysis", 0.85, 7),

  ("language", "bayesian_reasoning", "Bayesian reasoning", "epistemology",
   "Updating beliefs using Bayes theorem: P(H|E) = P(E|H)*P(H)/P(E). Prior belief updated by evidence to posterior. Base rate neglect is common error. Bayesian vs frequentist approaches to probability.",
   "medical diagnosis: prior disease prevalence, update with test result",
   "requires: conditional_probability, enables: belief_updating, decision_making", 0.85, 6),

  ("language", "analogical_reasoning", "analogical reasoning", "epistemology",
   "Inferring that things similar in some ways are similar in others. Structure mapping: aligning relational structure between source and target domains. Basis of much human creativity and transfer learning.",
   "atom is like solar system, neural network is like brain",
   "connects: abstraction, metaphor, enables: creative_transfer, problem_solving", 0.80, 5),

  ("language", "cognitive_bias", "cognitive biases", "epistemology",
   "Systematic errors in thinking: confirmation bias (seek confirming evidence), availability heuristic (judge by memorable examples), anchoring (over-rely on first information), sunk cost fallacy.",
   "news consumption, investment decisions, product design feedback",
   "connects: heuristics, decision_making, enables: bias_correction", 0.80, 5),

  ("language", "abstraction", "abstraction", "epistemology",
   "Process of identifying essential properties and ignoring irrelevant details. Levels of abstraction: high (interface/behavior) to low (implementation/hardware). Fundamental to programming and mathematics.",
   "abstract data type, class hiding implementation, mathematics abstracts from concrete",
   "enables: modularity, generalization, complexity_management", 0.85, 5),

  ("language", "metacognition", "metacognition", "epistemology",
   "Thinking about one's own thinking. Knowing what you know and what you dont know. Calibrated uncertainty. Recognizing when a strategy is not working and switching. Core to expertise and learning.",
   "Flux flagging uncertainty, asking for elaboration when confused",
   "connects: self_knowledge, calibration, enables: self_directed_learning", 0.85, 6),

  ("language", "formal_logic", "formal logic", "epistemology",
   "Mathematical study of valid inference. Propositional logic: AND, OR, NOT, IF-THEN. Predicate logic adds quantifiers: forall, exists. Soundness: valid inferences preserve truth. Completeness: all truths provable.",
   "Clingo answer set programming, Z3 SMT solver, theorem provers",
   "requires: syntax, semantics, enables: automated_reasoning, proof_systems", 0.85, 7),

  ("language", "generalization", "generalization", "epistemology",
   "Extending patterns from specific observations to broader categories. In ML: model performance on unseen data. In cognition: applying learned rules to new instances. Requires identifying relevant features.",
   "train on cats and dogs, generalize to new cat photos",
   "connects: abstraction, inductive_reasoning, overfitting", 0.85, 5),

  # ── Self-Knowledge (20) ─────────────────────────────────────────────────────
  ("self_knowledge", "flux_architecture_overview", "Flux architecture overview", "flux_system",
   "Flux is a self-learning AI built from scratch. Core components: KGStore (knowledge graph with 437 nodes), LiquidTransformer (char-level LTC neural network), ConsolidationHead (commit decisions), FLM (text generation), Clingo (logical reasoning), RSM (self-modification).",
   "flux.ask() → intent → KG lookup → reasoning → response",
   "connects: KGStore, LiquidTransformer, ConsolidationHead, FLM, Clingo, RSM", 0.90, 6),

  ("self_knowledge", "flux_ltc_cell", "Flux LiquidCell (LTC)", "flux_system",
   "Flux uses two LTC cells. Cell 0: embed_dim=32 input, hidden_dim=128 state. Cell 1: hidden_dim=128 input, hidden_dim=128 state. W_combined: Linear(hidden+input, hidden*2) fused tau and f projection. ODE: dx/dt=(1/tau)*(-x+tanh(f)+v_leak).",
   "cells.0.W_combined shape (256,160), cells.1.W_combined shape (256,256)",
   "requires: ODE_solver, euler_step, enables: character_level_processing", 0.90, 8),

  ("self_knowledge", "flux_consolidation_head", "Flux ConsolidationHead", "flux_system",
   "Binary classifier deciding whether to commit a session to the KG. Architecture: Linear(128,64) -> GELU -> Dropout(0.1) -> Linear(64,1). Bottleneck dim=64 after promotion from 32. Outputs confidence 0-1.",
   "conf > threshold → COMMIT, conf <= threshold → SKIP",
   "requires: LTC_hidden_state, enables: knowledge_acquisition_decision", 0.90, 6),

  ("self_knowledge", "flux_kgstore", "Flux KGStore", "flux_system",
   "Knowledge graph with 437 nodes and 527 edges. Nodes: concepts with definition, domain, confidence, relations. 108 seed nodes (immutable core), 329 live nodes (learned). Edges: enables, requires, connects, causes.",
   "store.get(concept_id) returns node dict, store.all_ids() lists all concepts",
   "enables: knowledge_retrieval, Clingo_reasoning, response_generation", 0.90, 6),

  ("self_knowledge", "flux_rsm_layer3", "Flux RSM Layer 3 (architectural self-modification)", "flux_system",
   "Four-component pipeline: L3A BottleneckDetector monitors 4 signals (latency, fisher_sat, plateau_var, ewc_forget), L3B CandidateBuilder proposes changes, IsolationRunner trains candidate, L3C ArchitecturalApproval gets human approval before promoting.",
   "bottleneck_detector -> candidate_builder -> isolation_runner -> architectural_approval",
   "enables: self_directed_architecture_change, requires: human_approval", 0.90, 8),

  ("self_knowledge", "flux_ewc_protection", "Flux EWC protection during fine-tuning", "flux_system",
   "Before fine-tuning, Fisher information computed over existing grounded sessions. EWC penalty = lambda * sum(F_i*(theta - theta*)^2) added to loss. Protects weights critical for existing knowledge. Lambda=5000 for strong protection.",
   "flux_ewc_state.pt stores Fisher matrices and optimal parameters",
   "requires: fisher_information, enables: continual_learning, catastrophic_forgetting_prevention", 0.90, 7),

  ("self_knowledge", "flux_stage_b", "Flux Stage-B grounded training", "flux_system",
   "Supervised fine-tuning of LTC + ConsolidationHead on grounded sessions from past interactions. Runs after end_session(). Loads from flux_grounded_sessions.jsonl and coding_language_sessions.jsonl. EWC prevents forgetting.",
   "grounded_trainer.run() called periodically, saves flux_grounded_best.pt",
   "requires: grounded_sessions, EWC_state, enables: knowledge_consolidation", 0.90, 7),

  ("self_knowledge", "flux_clingo_reasoning", "Flux Clingo ASP reasoning", "flux_system",
   "Flux uses Clingo (Answer Set Programming) for logical inference over the KG. Encodes KG edges as ASP facts, runs rules for path-finding and constraint checking. Constitutional layer: immutable axioms enforced by Clingo.",
   "reachable(X,Y) :- edge(X,Y). reachable(X,Z) :- reachable(X,Y), edge(Y,Z).",
   "requires: KGStore_edges, enables: graph_traversal, logical_inference", 0.90, 8),

  ("self_knowledge", "flux_flm", "Flux FLM (FluxLanguageModel)", "flux_system",
   "Small causal transformer decoder generating text responses. Architecture: d=64, L=2 transformer layers, ff=128, vocab=3934 word-level tokens. Trained on gold QA pairs from logs/generation/train_gold.jsonl.",
   "generates natural language descriptions of KG concepts",
   "requires: KG_node_encoding, enables: natural_language_response", 0.90, 7),

  ("self_knowledge", "flux_candidate_script", "Flux candidate training script", "flux_system",
   "Self-contained training script (no flux imports) that trains a candidate architecture in isolation. Inline _LiquidCell matches production W_combined shape: Linear(hidden+input, hidden*2). Writes candidate_output.json with locked schema.",
   "runs inside subprocess or Firecracker VM, cannot modify live KG",
   "requires: corpus_snapshot, produces: candidate_weights.pt, candidate_output.json", 0.90, 8),

  ("self_knowledge", "flux_identity_preservation", "Flux identity preservation during fine-tuning", "flux_system",
   "Fine-tuning changes LTC perception layer (embedding, projection). Does NOT change KG (ground truth), Clingo axioms, or causal reasoning. EWC protects existing capabilities. KG must win over LTC when they conflict. Identity Preservation Test verifies this.",
   "run --baseline before, --post-finetune after, compare KG win rate",
   "requires: EWC, KGStore_as_ground_truth, enables: safe_fine_tuning", 0.90, 8),

]


def main() -> None:
    c2i = _load_vocab()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    counts: dict[str, int] = {}
    with OUT_PATH.open("w", encoding="utf-8") as f:
        for idx, (cat, cid, name, domain, defn, examples, rels, conf, complexity) in enumerate(CONCEPTS):
            session = _session(idx, cat, cid, name, domain, defn, examples, rels, conf, complexity, c2i)
            f.write(json.dumps(session) + "\n")
            counts[cat] = counts.get(cat, 0) + 1

    print(f"\n=== Coding + Language Corpus Generated ===")
    print(f"  Output: {OUT_PATH}")
    print(f"  Total sessions: {len(CONCEPTS)}")
    for cat, n in sorted(counts.items()):
        print(f"    {cat}: {n}")

    # Verify: decode first and last entry
    import array as _array, base64 as _b64
    i2c = {v: k for k, v in c2i.items()}
    for label, entry in [("first", 0), ("last", len(CONCEPTS)-1)]:
        with OUT_PATH.open() as f:
            lines = f.readlines()
        sess = json.loads(lines[entry])
        raw  = _b64.b64decode(sess["char_sequence_b64"])
        arr  = _array.array("H", raw)
        text = "".join(i2c.get(int(i), "?") for i in arr)
        print(f"\n  [{label}] {sess['session_id']}: conf={sess['conf_target']}")
        print(f"  Text: {text[:120]}...")


if __name__ == "__main__":
    main()
