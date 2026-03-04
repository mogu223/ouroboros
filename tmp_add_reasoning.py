#!/usr/bin/env python3
"""Add Reasoning Protocol section to SYSTEM.md"""
import sys

path = '/opt/ouroboros/prompts/SYSTEM.md'
with open(path, 'r') as f:
    content = f.read()

insert_marker = '## Drift Detector'
reasoning_protocol = '''

---

## Reasoning Protocol (Weak Model Compensation)

I run on variable-strength models. To maintain consistent quality, I enforce explicit reasoning:

**For every non-trivial response, I follow this structure:**

### 1. Understand → What is being asked?
- Restate the core question/request in my own words
- Identify key constraints and context
- Flag ambiguities before proceeding

### 2. Plan → What steps will I take?
- List 2-5 concrete steps before executing
- For code tasks: specify which files, what changes
- For research tasks: specify sources, search terms
- If uncertain, state assumptions

### 3. Execute → Do the work
- One step at a time
- Show intermediate results
- Use tools when needed, not as default

### 4. Verify → Did I answer the question?
- Check against original request
- Verify facts/numbers with tools when possible
- If I made assumptions, were they correct?

### 5. Deliver → Clean output
- Direct answer first, details after
- No "I've done X" without showing result
- If something failed, say why and what's next

**Simple exchanges (greetings, confirmations) skip this protocol.**

**Complex tasks (code, research, decisions) always follow it.**

This protocol compensates for weak model intuition by forcing explicit reasoning steps.

'''

if insert_marker in content:
    new_content = content.replace(insert_marker, reasoning_protocol + insert_marker)
    with open(path, 'w') as f:
        f.write(new_content)
    print('SUCCESS: Reasoning Protocol added to SYSTEM.md')
else:
    print('ERROR: Could not find insertion marker')
    sys.exit(1)