#!/usr/bin/env python3
"""
flux/bootstrap/parser.py
Core bootstrap parser for the FLUX cognitive architecture.
Loads, verifies, and compiles FLUX_PRIMITIVES.md into a memory-resident Knowledge Graph.
"""

import os
import re
import hashlib

# =====================================================================
# CUSTOM EXCEPTIONS
# =====================================================================

class BootstrapError(Exception):
    """Base exception for all bootstrap loader errors."""
    pass

class IntegrityError(BootstrapError):
    """Raised when the SHA256 integrity signature check fails."""
    pass

class ParsingError(BootstrapError):
    """Raised when parsing a primitive, grammar, or corpus block fails."""
    pass

class UnresolvedReferenceError(BootstrapError):
    """Raised when an edge maps to a concept or primitive that does not exist."""
    pass

class InvalidRelationError(BootstrapError):
    """Raised when an edge uses a relation type that is not a defined Layer 2 primitive."""
    pass


# =====================================================================
# DATA MODELS
# =====================================================================

class Node:
    """Represents a node in the Flux Knowledge Graph."""
    def __init__(self, name, node_type, properties):
        self.name = name          # Uppercase for primitives, lowercase for concepts
        self.type = node_type      # LOGICAL, EPISTEMIC, RELATIONAL, ACTION, DOMAIN, CONCEPT
        self.properties = properties  # Extracted dictionary of attributes

    def __repr__(self):
        return f"Node({self.name}, type={self.type})"


class Edge:
    """Represents a directed relationship edge in the Flux Knowledge Graph."""
    def __init__(self, source, target, relation):
        self.source = source      # Source node name (string)
        self.target = target      # Target node name (string)
        self.relation = relation  # Relational primitive type (string, uppercase)

    def __repr__(self):
        return f"Edge({self.source} --[{self.relation}]--> {self.target})"


class GrammarBlock:
    """Represents a grammar rule syntax block."""
    def __init__(self, name, syntax, produces, properties):
        self.name = name          # Grammar block identifier (e.g. TEACH)
        self.syntax = syntax      # Expected multiline syntax
        self.produces = produces  # Produced objects/artifacts list
        self.properties = properties

    def __repr__(self):
        return f"GrammarBlock({self.name})"


class Benchmark:
    """Represents an evaluation benchmark probe."""
    def __init__(self, benchmark_id, tier, question, expected_primitives, correct_answer, properties):
        self.id = benchmark_id
        self.tier = tier
        self.question = question
        self.expected_primitives = expected_primitives  # List of uppercase primitive names
        self.correct_answer = correct_answer
        self.properties = properties

    def __repr__(self):
        return f"Benchmark({self.id}, tier={self.tier})"


class KnowledgeGraph:
    """The in-memory semantic representation of Flux's logical foundation."""
    def __init__(self):
        self.nodes = {}  # Map: node_name -> Node
        self.edges = []  # List of Edge
        self._edge_map = {} # Map: (source, target, relation) -> Edge

    def add_node(self, name, node_type, properties):
        name = str(name) if name is not None else ""
        node_type = str(node_type) if node_type is not None else ""
        
        if node_type:
            node_type = node_type.upper()
        if name and node_type != 'CONCEPT':
            name = name.upper()
        elif name and node_type == 'CONCEPT':
            name = name.lower()
        node = Node(name, node_type, properties)
        self.nodes[name] = node
        return node

    def add_edge(self, source, target, relation):
        # Prevent duplicate edges using a map of tuples for O(1) lookup
        edge_tuple = (source, target, relation)
        if edge_tuple in self._edge_map:
            return self._edge_map[edge_tuple]
        
        edge = Edge(source, target, relation)
        self.edges.append(edge)
        self._edge_map[edge_tuple] = edge
        return edge


# =====================================================================
# YAML & METADATA PARSER
# =====================================================================

def parse_yaml_block(yaml_str):
    """
    Parses a subset of YAML block content.
    Handles key-value mapping, inline arrays, lists, and multiline blocks.
    """
    data = {}
    lines = yaml_str.split('\n')
    i = 0
    current_key = None
    in_multiline = False
    multiline_key = None
    multiline_lines = []
    
    while i < len(lines):
        line = lines[i]
        
        # Multiline string handler (| operator)
        if in_multiline:
            if not line.strip():
                multiline_lines.append("")
                i += 1
                continue
            leading_spaces = len(line) - len(line.lstrip())
            if leading_spaces >= 2:
                multiline_lines.append(line.strip())
                i += 1
                continue
            else:
                data[multiline_key] = "\n".join(multiline_lines)
                in_multiline = False
                multiline_key = None
                multiline_lines = []
        
        line_stripped = line.strip()
        if not line_stripped or line_stripped.startswith('#'):
            i += 1
            continue
            
        # Bullet list items (- "val")
        if line_stripped.startswith('- '):
            if current_key and isinstance(data[current_key], list):
                val = line_stripped[2:].strip().strip('"').strip("'")
                data[current_key].append(val)
            else:
                raise ParsingError(f"Malformed bullet list item without parent key: {line_stripped}")
            i += 1
            continue
            
        # Standard Key-Value mapping
        if ':' in line_stripped:
            parts = line_stripped.split(':', 1)
            key = parts[0].strip()
            val = parts[1].strip()
            
            if val == '|':
                in_multiline = True
                multiline_key = key
                multiline_lines = []
                current_key = key
            elif not val:
                data[key] = []
                current_key = key
            else:
                val = val.strip('"').strip("'")
                
                # Check for inline list [A, B, C]
                if val.startswith('[') and val.endswith(']'):
                    val = [item.strip().strip('"').strip("'") for item in val[1:-1].split(',') if item.strip()]
                else:
                    # Unescape newlines for constraints
                    if key == 'clingo_constraint':
                        val = val.replace('\\n', '\n')
                    
                    # Convert Booleans structurally
                    if isinstance(val, str) and val.lower() in ('true', 'yes'):
                        val = True
                    elif isinstance(val, str) and val.lower() in ('false', 'no'):
                        val = False
                    else:
                        # Convert numbers
                        numeric_keys = {'confidence', 'confidence_threshold', 'trust_level', 'output_trust_default'}
                        if key in numeric_keys:
                            try:
                                val = float(val)
                            except ValueError:
                                pass # Keep as string if CONTEXTUAL or VARIABLE
                        elif key == 'tier':
                            try:
                                val = int(val)
                            except ValueError:
                                pass
                            
                data[key] = val
                current_key = key
        i += 1
        
    # Flush any remaining multiline string at EOF. The loop always exits 
    # to this point because there are no early returns inside the loop.
    if in_multiline:
        data[multiline_key] = "\n".join(multiline_lines)
        
    return data


# =====================================================================
# BOOTSTRAP LOADER PARSER
# =====================================================================

class BootstrapParser:
    """Loads and compiles FLUX_PRIMITIVES.md via a strict two-pass sequence."""
    def __init__(self, filepath):
        self.filepath = os.path.abspath(filepath)
        self.raw_content = ""
        self.metadata = {}
        self.graph = KnowledgeGraph()
        self.grammars = {}
        self.benchmarks = {}
        self.bootstrap_sequence = ""
        
    def load(self):
        """Runs the complete boot verification and graph compilation pipeline."""
        if not os.path.exists(self.filepath):
            raise FileNotFoundError(f"Bootstrap file not found: {self.filepath}")
            
        with open(self.filepath, 'r', encoding='utf-8') as f:
            self.raw_content = f.read()
            
        # 1. Integrity check
        self._verify_hash()
        
        # 2. Pass 1: Node and grammar/benchmark parsing
        self._pass_1_node_creation()
        
        # 3. Pass 2: Edge resolution
        self._pass_2_edge_creation()
        
        return self.graph

    def _verify_hash(self):
        """Computes and checks the SHA256 integrity hash of the file."""
        yaml_pattern = r"```yaml\s*\n(.*?)\n```"
        yaml_match = re.search(yaml_pattern, self.raw_content, re.DOTALL)
        if not yaml_match:
            raise IntegrityError("No YAML metadata block found for hash verification.")
            
        yaml_content = yaml_match.group(1)
        yaml_start = yaml_match.start(1)
        
        hash_pattern = r"(hash:\s*)(\S+)"
        match = re.search(hash_pattern, yaml_content)
        if not match:
            raise IntegrityError("No integrity hash found in METADATA block.")
            
        declared_hash = match.group(2)
        if declared_hash == "PENDING_CALCULATION":
            raise IntegrityError("Boot halted: file integrity hash is set to PENDING_CALCULATION.")
            
        global_start = yaml_start + match.start(2)
        global_end = yaml_start + match.end(2)
        normalized_content = self.raw_content[:global_start] + "PENDING_CALCULATION" + self.raw_content[global_end:]
        computed_hash = hashlib.sha256(normalized_content.encode('utf-8')).hexdigest()
        
        if computed_hash != declared_hash:
            raise IntegrityError(
                f"Boot halted: File integrity check failed!\n"
                f"  Declared: {declared_hash}\n"
                f"  Computed: {computed_hash}"
            )

    def _extract_blocks(self, block_type):
        """Extracts contents inside ```block_type ... ``` fences."""
        pattern = rf"```{block_type}\s*\n(.*?)\n```"
        return re.findall(pattern, self.raw_content, re.DOTALL)

    def _pass_1_node_creation(self):
        """Pass 1: Instantiates all primitive, grammar, corpus, and benchmark nodes."""
        # A. Parse Metadata
        meta_blocks = self._extract_blocks('yaml')
        if not meta_blocks:
            raise ParsingError("Missing YAML metadata block.")
        self.metadata = parse_yaml_block(meta_blocks[0])
        
        # B. Parse Primitives
        prim_blocks = self._extract_blocks('primitive')
        for idx, block in enumerate(prim_blocks):
            try:
                prim_data = parse_yaml_block(block)
            except Exception as e:
                raise ParsingError(f"Failed to parse primitive block {idx}: {e}")
                
            name = prim_data.get('name')
            prim_type = prim_data.get('type')
            if name is None or prim_type is None:
                raise ParsingError(f"Primitive block {idx} is missing name or type.")
                
            # Add to graph
            self.graph.add_node(name, prim_type, prim_data)

        # C. Parse Grammar Blocks
        grammar_blocks = self._extract_blocks('grammar')
        for idx, block in enumerate(grammar_blocks):
            try:
                g_data = parse_yaml_block(block)
            except Exception as e:
                raise ParsingError(f"Failed to parse grammar block {idx}: {e}")
                
            block_name = g_data.get('block')
            syntax = g_data.get('syntax')
            produces = g_data.get('produces')
            if not block_name or not syntax or not produces:
                raise ParsingError(f"Grammar block {idx} is missing block, syntax, or produces field.")
                
            self.grammars[block_name] = GrammarBlock(block_name, syntax, produces, g_data)

        # D. Parse Bootstrap Sequence
        boot_blocks = self._extract_blocks('boot_order')
        if boot_blocks:
            self.bootstrap_sequence = boot_blocks[0]

        # E. Parse Teaching Corpus Blocks (Nodes only)
        teach_blocks = self._extract_blocks('teach_block')
        for idx, block in enumerate(teach_blocks):
            # Parse teaching corpus block content
            # The teaching corpus yaml has [TEACH] header. We clean it to parse it as standard yaml.
            cleaned_block = block.replace('[TEACH]', '').replace('[/TEACH]', '')
            try:
                tc_data = parse_yaml_block(cleaned_block)
            except Exception as e:
                raise ParsingError(f"Failed to parse teaching block {idx}: {e}")
                
            concept = tc_data.get('concept')
            if concept is None:
                raise ParsingError(f"Teaching block {idx} (ID: {tc_data.get('id')}) is missing concept name.")
                
            # Create a concept node (lowercase)
            self.graph.add_node(concept, 'CONCEPT', tc_data)

        # F. Parse Benchmark Blocks
        bench_blocks = self._extract_blocks('benchmark')
        for idx, block in enumerate(bench_blocks):
            try:
                b_data = parse_yaml_block(block)
            except Exception as e:
                raise ParsingError(f"Failed to parse benchmark block {idx}: {e}")
                
            b_id = b_data.get('id')
            tier = b_data.get('tier')
            question = b_data.get('question')
            expected = b_data.get('expected_primitives')
            correct = b_data.get('correct_answer')
            if not b_id or not tier or not question or expected is None or not correct:
                raise ParsingError(f"Benchmark block {idx} is missing required fields.")
                
            self.benchmarks[b_id] = Benchmark(b_id, tier, question, expected, correct, b_data)

    def _pass_2_edge_creation(self):
        """Pass 2: Resolves edge relationships (requires, relates_to, is_a)."""
        # Get all concept nodes
        concept_nodes = [node for node in self.graph.nodes.values() if node.type == 'CONCEPT']
        
        # Verify that we have defined Relational primitives in Layer 2 to validate edge types
        relational_primitives = {name for name, node in self.graph.nodes.items() if node.type == 'RELATIONAL'}
        
        for node in concept_nodes:
            props = node.properties
            concept_name = node.name
            
            # A. Resolve is_a mapping (taxonomic hierarchy)
            parent_type = props.get('is_a')
            if parent_type:
                parent_type = str(parent_type)
                if parent_type not in self.graph.nodes:
                    if parent_type.upper() in self.graph.nodes:
                        parent_type = parent_type.upper()
                    elif parent_type.lower() in self.graph.nodes:
                        parent_type = parent_type.lower()
                    else:
                        raise UnresolvedReferenceError(
                            f"Concept '{concept_name}' has invalid is_a parent '{parent_type}' (node does not exist)."
                        )
                # Create IS_A edge
                self.graph.add_edge(concept_name, parent_type, 'IS_A')
                
            # B. Resolve requires dependencies
            requires_raw = props.get('requires')
            if requires_raw and str(requires_raw).lower() not in ("none", "false"):
                requires_str = str(requires_raw)
                # Split requirements (strictly by pipe | to match relates_to)
                requires_list = [r.strip() for r in requires_str.split('|') if r.strip()]
                for req in requires_list:
                    if req not in self.graph.nodes:
                        if req.upper() in self.graph.nodes:
                            req = req.upper()
                        elif req.lower() in self.graph.nodes:
                            req = req.lower()
                        else:
                            raise UnresolvedReferenceError(
                                f"Concept '{concept_name}' requires node '{req}', which does not exist."
                            )
                    # Create dependency edge
                    self.graph.add_edge(concept_name, req, 'REQUIRES')
                    
            # C. Resolve relates_to semantic linkages
            relates_raw = props.get('relates_to')
            if relates_raw and str(relates_raw).lower() not in ("none", "false"):
                relates_str = str(relates_raw)
                # Split relationships (e.g. "target:RELATION | target2:RELATION2")
                relates_list = [r.strip() for r in relates_str.split('|') if r.strip()]
                for rel_item in relates_list:
                    if ':' not in rel_item:
                        raise ParsingError(f"Concept '{concept_name}' has malformed relates_to item: '{rel_item}'")
                    target, relation = rel_item.split(':', 1)
                    target = target.strip()
                    relation = relation.strip()
                    
                    # Verify target node exists
                    if target not in self.graph.nodes:
                        if target.upper() in self.graph.nodes:
                            target = target.upper()
                        elif target.lower() in self.graph.nodes:
                            target = target.lower()
                        else:
                            raise UnresolvedReferenceError(
                                f"Concept '{concept_name}' relates to '{target}', which does not exist."
                            )
                    # Verify relation is a defined Layer 2 Relational primitive
                    if relation not in relational_primitives:
                        if relation.upper() in relational_primitives:
                            relation = relation.upper()
                        else:
                            raise InvalidRelationError(
                                f"Concept '{concept_name}' uses relation type '{relation}' which is not a defined Layer 2 relational primitive."
                            )
                    # Create semantic edge
                    self.graph.add_edge(concept_name, target, relation)

    def compile_clingo_axioms(self, output_path):
        """Extracts Clingo constraints from ACTION and DOMAIN primitives and writes them to an ASP file."""
        axioms = []
        # Compile ACTION node constraints (Layer 3)
        action_nodes = [node for node in self.graph.nodes.values() if node.type == 'ACTION']
        
        for node in action_nodes:
            constraint = node.properties.get('clingo_constraint')
            if constraint and constraint != "none":
                # Pre-write Clingo syntax validation (basic check)
                if ':-' not in constraint or not constraint.strip().endswith('.') or '(' not in constraint or ')' not in constraint:
                    raise ParsingError(f"Action '{node.name}' has invalid Clingo constraint syntax: {constraint}")
                axioms.append(f"% Safety constraint for {node.name}\n{constraint}\n")

        # Compile DOMAIN node constraints (Layer 4 — AKHIL, USER, etc.)
        domain_nodes = [node for node in self.graph.nodes.values() if node.type == 'DOMAIN']
        
        for node in domain_nodes:
            constraint = node.properties.get('clingo_constraint')
            if constraint and constraint != "none":
                if ':-' not in constraint or not constraint.strip().endswith('.') or '(' not in constraint or ')' not in constraint:
                    raise ParsingError(f"Domain '{node.name}' has invalid Clingo constraint syntax: {constraint}")
                axioms.append(f"% Identity constraint for {node.name}\n{constraint}\n")
                
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("#program base.\n\n")
            f.write("\n".join(axioms))
        print(f"Successfully compiled Clingo constraints to: {output_path}")

    def stats(self):
        """Returns counts of compiled nodes, edges, grammars, and benchmarks."""
        primitives = [n for n in self.graph.nodes.values() if n.type != 'CONCEPT']
        concepts = [n for n in self.graph.nodes.values() if n.type == 'CONCEPT']
        return {
            'primitives': len(primitives),
            'concept_nodes': len(concepts),
            'edges': len(self.graph.edges),
            'grammar_blocks': len(self.grammars),
            'benchmarks': len(self.benchmarks),
        }
