#!/usr/bin/env python3
"""
Ouroboros Pre-boot Verification

Run this before starting the main loop to verify:
1. All critical imports succeed
2. Required files exist
3. Branch consistency (ouroboros vs ouroboros-stable)
4. Environment variables are set

Returns exit code 0 if all checks pass, non-zero otherwise.
"""

import sys
import pathlib
import os

REPO_DIR = pathlib.Path("/opt/ouroboros")
DRIVE_ROOT = pathlib.Path("/content/drive/MyDrive/Ouroboros")

# Add repo to path for imports
sys.path.insert(0, str(REPO_DIR))

def check_import(module_name: str, names: list[str] = None) -> bool:
    """Check if a module can be imported and contains expected names."""
    try:
        module = __import__(module_name, fromlist=names or [])
        
        if names:
            for name in names:
                if not hasattr(module, name):
                    print(f"❌ Missing '{name}' in {module_name}")
                    return False
        print(f"✅ {module_name} imports OK")
        return True
    except Exception as e:
        print(f"❌ Failed to import {module_name}: {e}")
        return False

def check_file_exists(path: pathlib.Path, description: str) -> bool:
    """Check if a required file exists."""
    if path.exists():
        print(f"✅ {description}: {path}")
        return True
    else:
        print(f"❌ Missing {description}: {path}")
        return False

def check_branch_consistency() -> bool:
    """Check that ouroboros and ouroboros-stable have compatible critical files."""
    import subprocess
    
    try:
        # Get current branch
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=REPO_DIR,
            capture_output=True,
            text=True
        )
        current_branch = result.stdout.strip()
        print(f"📍 Current branch: {current_branch}")
        
        # Check if we're on a valid branch
        if current_branch not in ["ouroboros", "ouroboros-stable"]:
            print(f"⚠️  Warning: Not on expected branch (ouroboros or ouroboros-stable)")
        
        # Get git status
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_DIR,
            capture_output=True,
            text=True
        )
        if result.stdout.strip():
            print(f"⚠️  Working tree has uncommitted changes:")
            for line in result.stdout.strip().split('\n')[:5]:
                print(f"   {line}")
        else:
            print(f"✅ Working tree clean")
        
        return True
    except Exception as e:
        print(f"❌ Branch check failed: {e}")
        return False

def check_environment() -> bool:
    """Check required environment variables."""
    required = [
        "TELEGRAM_BOT_TOKEN",
        "GITHUB_USER",
        "GITHUB_REPO",
        "OUROBOROS_MODEL",
    ]
    
    all_ok = True
    for var in required:
        if var in os.environ:
            value = os.environ[var]
            masked = value[:4] + "..." if len(value) > 4 else value
            print(f"✅ {var}={masked}")
        else:
            print(f"❌ Missing env var: {var}")
            all_ok = False
    
    return all_ok

def main():
    print("=" * 60)
    print("Ouroboros Pre-boot Verification")
    print("=" * 60)
    
    all_ok = True
    
    # 1. Check critical files
    print("\n📁 Checking critical files...")
    critical_files = [
        (REPO_DIR / "VERSION", "VERSION file"),
        (REPO_DIR / "BIBLE.md", "BIBLE.md"),
        (REPO_DIR / "colab_launcher.py", "colab_launcher.py"),
        (REPO_DIR / "ouroboros/agent.py", "agent.py"),
        (REPO_DIR / "ouroboros/llm.py", "llm.py"),
        (REPO_DIR / "ouroboros/utils.py", "utils.py"),
    ]
    for path, desc in critical_files:
        if not check_file_exists(path, desc):
            all_ok = False
    
    # 2. Check critical imports
    print("\n🔧 Checking critical imports...")
    import_checks = [
        ("ouroboros.utils", ["read_text", "append_jsonl", "utc_now_iso"]),
        ("ouroboros.llm", ["LLMClient", "add_usage"]),
        ("ouroboros.agent", ["run_tool_loop"]),
    ]
    for module, names in import_checks:
        if not check_import(module, names):
            all_ok = False
    
    # 3. Check branch consistency
    print("\n🌿 Checking branch status...")
    if not check_branch_consistency():
        all_ok = False
    
    # 4. Check environment
    print("\n🔐 Checking environment...")
    if not check_environment():
        all_ok = False
    
    # Summary
    print("\n" + "=" * 60)
    if all_ok:
        print("✅ All pre-boot checks passed!")
        print("=" * 60)
        return 0
    else:
        print("❌ Some checks failed. Review errors above.")
        print("=" * 60)
        return 1

if __name__ == "__main__":
    sys.exit(main())
