#!/usr/bin/env python3
"""Patch workers.py to add memory protection."""

import re

# Read the file
with open('/opt/ouroboros/supervisor/workers.py', 'r') as f:
    content = f.read()

# 1. Add import after the existing imports
import_section = '''from supervisor.state import load_state, append_jsonl
from supervisor import git_ops
from supervisor.telegram import send_with_budget'''

new_import_section = '''from supervisor.state import load_state, append_jsonl
from supervisor import git_ops
from supervisor.telegram import send_with_budget
from ouroboros.resources import check_memory_for_task, is_heavy_task'''

content = content.replace(import_section, new_import_section)

# 2. Find and modify assign_tasks function
# We need to add memory check after 'task = PENDING.pop(chosen_idx)'
old_assign = '''                task = PENDING.pop(chosen_idx)
                w.busy_task_id = task["id"]'''

new_assign = '''                task = PENDING.pop(chosen_idx)
                
                # Memory protection: check before assigning
                mem_ok, mem_reason = check_memory_for_task(task)
                if not mem_ok:
                    append_jsonl(
                        DRIVE_ROOT / "logs" / "supervisor.jsonl",
                        {
                            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            "type": "task_memory_deferred",
                            "task_id": task.get("id"),
                            "reason": mem_reason,
                            "is_heavy": is_heavy_task(task),
                        },
                    )
                    # Put task back at front of queue
                    PENDING.insert(0, task)
                    # Skip to next worker if this was heavy, otherwise try next task
                    if is_heavy_task(task):
                        continue
                    else:
                        # For light tasks, still defer but try other tasks
                        continue
                
                w.busy_task_id = task["id"]'''

content = content.replace(old_assign, new_assign)

# Write back
with open('/opt/ouroboros/supervisor/workers.py', 'w') as f:
    f.write(content)

print('Patch applied successfully')