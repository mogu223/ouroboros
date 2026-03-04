#!/usr/bin/env python3
"""
Ouroboros Snapshot Creator

Creates a timestamped backup of the current state before making changes.
Backups include:
- Current git SHA and branch
- VERSION file content
- Key config files
- Option to restore from a previous snapshot

Usage:
  python snapshot.py create   # Create new snapshot
  python snapshot.py list     # List available snapshots
  python snapshot.py restore <timestamp>  # Restore from snapshot
"""

import sys
import pathlib
import subprocess
import shutil
import json
from datetime import datetime

DRIVE_ROOT = pathlib.Path("/content/drive/MyDrive/Ouroboros")
REPO_DIR = pathlib.Path("/opt/ouroboros")
BACKUP_DIR = DRIVE_ROOT / "backups" / "snapshots"

def get_git_info():
    """Get current git branch and SHA."""
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=REPO_DIR,
            capture_output=True,
            text=True
        ).stdout.strip()
        
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_DIR,
            capture_output=True,
            text=True
        ).stdout.strip()
        
        return branch, sha
    except Exception as e:
        return f"error: {e}", "unknown"

def create_snapshot(reason: str = "") -> pathlib.Path:
    """Create a new snapshot."""
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    snapshot_dir = BACKUP_DIR / timestamp
    
    # Create backup directory
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    
    # Get git info
    branch, sha = get_git_info()
    
    # Create metadata
    metadata = {
        "timestamp": timestamp,
        "branch": branch,
        "sha": sha,
        "reason": reason,
        "created_at": datetime.utcnow().isoformat(),
    }
    
    # Save metadata
    with open(snapshot_dir / "metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)
    
    # Copy VERSION file
    version_file = REPO_DIR / "VERSION"
    if version_file.exists():
        shutil.copy(version_file, snapshot_dir / "VERSION")
    
    # Copy BIBLE.md
    bible_file = REPO_DIR / "BIBLE.md"
    if bible_file.exists():
        shutil.copy(bible_file, snapshot_dir / "BIBLE.md")
    
    # Copy identity.md if exists
    identity_file = DRIVE_ROOT / "memory" / "identity.md"
    if identity_file.exists():
        shutil.copy(identity_file, snapshot_dir / "identity.md")
    
    # Copy state.json
    state_file = DRIVE_ROOT / "state" / "state.json"
    if state_file.exists():
        shutil.copy(state_file, snapshot_dir / "state.json")
    
    # Create restore script
    restore_script = f'''#!/bin/bash
# Restore snapshot {timestamp}
# Branch: {branch}, SHA: {sha}

echo "Restoring snapshot {timestamp}..."
echo "This will checkout SHA {sha} on branch {branch}"

cd {REPO_DIR}
git checkout {branch}
git reset --hard {sha}

echo "Restore complete!"
'''
    with open(snapshot_dir / "restore.sh", "w") as f:
        f.write(restore_script)
    
    print(f"✅ Snapshot created: {snapshot_dir}")
    print(f"   Branch: {branch}")
    print(f"   SHA: {sha}")
    print(f"   Reason: {reason or 'manual'}")
    
    return snapshot_dir

def list_snapshots():
    """List all available snapshots."""
    if not BACKUP_DIR.exists():
        print("No snapshots found.")
        return
    
    snapshots = sorted([d for d in BACKUP_DIR.iterdir() if d.is_dir()])
    
    if not snapshots:
        print("No snapshots found.")
        return
    
    print(f"Available snapshots ({len(snapshots)}):")
    print("-" * 60)
    
    for snapshot_dir in snapshots[-10:]:  # Show last 10
        metadata_file = snapshot_dir / "metadata.json"
        if metadata_file.exists():
            with open(metadata_file) as f:
                meta = json.load(f)
            print(f"  {meta['timestamp']}")
            print(f"    Branch: {meta['branch']}, SHA: {meta['sha'][:8]}")
            print(f"    Reason: {meta.get('reason', 'manual')}")
            print(f"    Created: {meta.get('created_at', 'unknown')}")
            print()
        else:
            print(f"  {snapshot_dir.name} (no metadata)")

def restore_snapshot(timestamp: str):
    """Restore from a snapshot."""
    snapshot_dir = BACKUP_DIR / timestamp
    
    if not snapshot_dir.exists():
        print(f"❌ Snapshot not found: {timestamp}")
        return
    
    metadata_file = snapshot_dir / "metadata.json"
    if not metadata_file.exists():
        print(f"❌ No metadata found for snapshot: {timestamp}")
        return
    
    with open(metadata_file) as f:
        meta = json.load(f)
    
    print(f"Restoring snapshot {timestamp}...")
    print(f"  Branch: {meta['branch']}")
    print(f"  SHA: {meta['sha']}")
    print()
    
    # Checkout the branch and reset to SHA
    try:
        subprocess.run(
            ["git", "checkout", meta['branch']],
            cwd=REPO_DIR,
            check=True
        )
        
        subprocess.run(
            ["git", "reset", "--hard", meta['sha']],
            cwd=REPO_DIR,
            check=True
        )
        
        print(f"✅ Restored to {meta['sha'][:8]}")
        print()
        print("⚠️  You may need to restart the ouroboros process for changes to take effect.")
        
    except subprocess.CalledProcessError as e:
        print(f"❌ Restore failed: {e}")

def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python snapshot.py create [reason]  - Create new snapshot")
        print("  python snapshot.py list             - List snapshots")
        print("  python snapshot.py restore <ts>     - Restore from snapshot")
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "create":
        reason = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        create_snapshot(reason)
    
    elif command == "list":
        list_snapshots()
    
    elif command == "restore":
        if len(sys.argv) < 3:
            print("❌ Please specify snapshot timestamp")
            sys.exit(1)
        restore_snapshot(sys.argv[2])
    
    else:
        print(f"❌ Unknown command: {command}")
        sys.exit(1)

if __name__ == "__main__":
    main()
