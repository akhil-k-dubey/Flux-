#!/usr/bin/env python3
import sys
import os
import re
import hashlib

def parse_yaml(yaml_str):
    data = {}
    lines = yaml_str.split('\n')
    i = 0
    current_key = None
    in_multiline = False
    multiline_key = None
    multiline_lines = []
    
    while i < len(lines):
        line = lines[i]
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
            
        if line_stripped.startswith('- '):
            if current_key and isinstance(data[current_key], list):
                val = line_stripped[2:].strip().strip('"').strip("'")
                data[current_key].append(val)
            i += 1
            continue
            
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
                boolean_keys = {'immutable', 'requires_verification', 'requires_sandbox', 'auditor_mandatory', 'do_calculus'}
                if key in boolean_keys:
                    if val.lower() in ('true', 'yes'):
                        val = True
                    elif val.lower() in ('false', 'no'):
                        val = False
                data[key] = val
                current_key = key
        i += 1
        
    if in_multiline:
        data[multiline_key] = "\n".join(multiline_lines)
        
    return data

def extract_blocks(content, block_type):
    pattern = rf"```{block_type}\s*\n(.*?)\n```"
    return re.findall(pattern, content, re.DOTALL)

def calculate_hash(content):
    # Find the line containing hash: and replace the value with PENDING_CALCULATION
    pattern = r"(hash:\s*)(\S+)"
    match = re.search(pattern, content)
    if not match:
        raise ValueError("Could not find hash line in METADATA block")
    
    current_hash = match.group(2)
    normalized_content = content[:match.start(2)] + "PENDING_CALCULATION" + content[match.end(2):]
    computed_hash = hashlib.sha256(normalized_content.encode('utf-8')).hexdigest()
    return computed_hash, current_hash, normalized_content

def update_file_hash(filepath, computed_hash):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    pattern = r"(hash:\s*)(\S+)"
    match = re.search(pattern, content)
    if not match:
        raise ValueError("Could not find hash line in METADATA block to update")
    
    updated_content = content[:match.start(2)] + computed_hash + content[match.end(2):]
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(updated_content)
    print(f"Successfully updated hash in {filepath} to: {computed_hash}")

def main():
    filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'FLUX_PRIMITIVES.md')
    if not os.path.exists(filepath):
        print(f"Error: {filepath} does not exist.")
        sys.exit(1)
        
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
        
    # 1. HASH VERIFICATION / UPDATE
    try:
        computed_hash, current_hash, normalized_content = calculate_hash(content)
    except Exception as e:
        print(f"Validation failed during hashing phase: {e}")
        sys.exit(1)
        
    update_mode = "--update-hash" in sys.argv
    
    if update_mode:
        update_file_hash(filepath, computed_hash)
        # Re-read content to ensure validation runs on the latest
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        computed_hash, current_hash, normalized_content = calculate_hash(content)
    else:
        if current_hash == "PENDING_CALCULATION":
            print(f"Warning: Hash is set to PENDING_CALCULATION. Run 'python validate_primitives.py --update-hash' to compute and write it.")
        elif current_hash != computed_hash:
            print(f"Error: File integrity check failed!")
            print(f"  Current hash in file:  {current_hash}")
            print(f"  Expected computed hash: {computed_hash}")
            print("If you edited the file, update the hash using 'python validate_primitives.py --update-hash'")
            sys.exit(1)
        else:
            print("File integrity check: PASSED")
            
    # 2. PARSE METADATA
    metadata_blocks = extract_blocks(content, 'yaml')
    if len(metadata_blocks) != 1:
        print(f"Error: Expected exactly 1 yaml metadata block, found {len(metadata_blocks)}")
        sys.exit(1)
    metadata = parse_yaml(metadata_blocks[0])
    required_meta = ['version', 'created', 'author', 'flux_version', 'status', 'hash']
    for field in required_meta:
        if field not in metadata:
            print(f"Error: Metadata missing required field: {field}")
            sys.exit(1)
    print("Metadata structure: PASSED")
    
    # 3. PARSE PRIMITIVES
    primitive_blocks = extract_blocks(content, 'primitive')
    if not primitive_blocks:
        print("Error: No primitive blocks found.")
        sys.exit(1)
        
    primitives = {}
    errors = 0
    
    for i, block in enumerate(primitive_blocks):
        try:
            prim = parse_yaml(block)
        except Exception as e:
            print(f"Error: Failed to parse primitive block {i}: {e}")
            errors += 1
            continue
            
        # Check required fields
        for field in ['name', 'type', 'definition']:
            if field not in prim:
                print(f"Error: Primitive block {i} is missing field: {field}")
                errors += 1
                
        name = prim.get('name')
        if not name:
            continue
            
        # Check uppercase name
        if not name.isupper():
            print(f"Error: Primitive name '{name}' must be uppercase.")
            errors += 1
            
        # Check uniqueness
        if name in primitives:
            print(f"Error: Duplicate primitive name found: '{name}'")
            errors += 1
        else:
            primitives[name] = prim
            
        # Validate category type
        prim_type = prim.get('type')
        valid_types = ['LOGICAL', 'EPISTEMIC', 'RELATIONAL', 'ACTION', 'DOMAIN']
        if prim_type not in valid_types:
            print(f"Error: Primitive '{name}' has invalid type '{prim_type}'. Must be one of {valid_types}.")
            errors += 1
            
        # Validate immutability
        if 'immutable' not in prim:
            print(f"Error: Primitive '{name}' missing 'immutable' field.")
            errors += 1
            
    print(f"Parsed {len(primitives)} primitives.")
    
    # 4. PARSE GRAMMAR
    grammar_blocks = extract_blocks(content, 'grammar')
    print(f"Found {len(grammar_blocks)} grammar blocks.")
    for i, block in enumerate(grammar_blocks):
        g = parse_yaml(block)
        if 'block' not in g or 'syntax' not in g or 'produces' not in g:
            print(f"Error: Grammar block {i} missing required fields (block, syntax, produces).")
            errors += 1
            
    # 5. PARSE TEACH BLOCKS
    teach_blocks = extract_blocks(content, 'teach_block')
    print(f"Found {len(teach_blocks)} initial teaching corpus blocks.")
    for i, block in enumerate(teach_blocks):
        t = parse_yaml(block)
        if 'id' not in t:
            print(f"Error: Teach block {i} missing ID.")
            errors += 1
            
    # 6. PARSE BENCHMARKS
    benchmark_blocks = extract_blocks(content, 'benchmark')
    print(f"Found {len(benchmark_blocks)} evaluation benchmark blocks.")
    for i, block in enumerate(benchmark_blocks):
        b = parse_yaml(block)
        for field in ['id', 'tier', 'question', 'expected_primitives', 'correct_answer']:
            if field not in b:
                print(f"Error: Benchmark block {i} missing required field: {field}")
                errors += 1
                
    if errors > 0:
        print(f"Validation failed with {errors} errors.")
        sys.exit(1)
        
    print("All syntax and validation checks: PASSED")
    sys.exit(0)

if __name__ == '__main__':
    main()
