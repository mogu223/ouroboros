"""System health check tool — runtime metrics and self-monitoring."""

import json
import logging
import os
import pathlib
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)


def _get_memory_usage() -> Dict[str, float]:
    """Get current memory usage in MB."""
    try:
        with open("/proc/meminfo", "r") as f:
            lines = f.readlines()
        
        mem_info = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                key = parts[0].rstrip(":")
                value = int(parts[1]) // 1024  # KB to MB
                mem_info[key] = value
        
        total = mem_info.get("MemTotal", 0)
        available = mem_info.get("MemAvailable", mem_info.get("MemFree", 0))
        used = total - available
        
        return {
            "total_mb": total,
            "used_mb": used,
            "available_mb": available,
            "usage_pct": (used / total * 100) if total > 0 else 0
        }
    except Exception as e:
        log.warning("Failed to get memory usage: %s", e)
        return {"error": str(e)}


def _check_version_sync(repo_dir: str) -> Dict[str, Any]:
    """Check if VERSION, git tag, and README are in sync."""
    try:
        import subprocess
        
        result = {"synced": True, "errors": []}
        
        # Read VERSION file
        version_file = pathlib.Path(repo_dir) / "VERSION"
        if version_file.exists():
            version = version_file.read_text().strip()
            result["version"] = version
        else:
            result["errors"].append("VERSION file not found")
            result["synced"] = False
            return result
        
        # Get latest git tag
        try:
            tag_result = subprocess.run(
                ["git", "describe", "--tags", "--abbrev=0"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=10
            )
            if tag_result.returncode == 0:
                latest_tag = tag_result.stdout.strip()
                result["git_tag"] = latest_tag
                if latest_tag != f"v{version}":
                    result["errors"].append(f"Git tag mismatch: {latest_tag} != v{version}")
                    result["synced"] = False
            else:
                result["errors"].append("No git tags found")
                result["synced"] = False
        except Exception as e:
            result["errors"].append(f"Git tag check failed: {e}")
            result["synced"] = False
        
        return result
    except Exception as e:
        return {"synced": False, "errors": [str(e)]}


def _check_identity_freshness(drive_root: str) -> Dict[str, Any]:
    """Check if identity.md was updated within the last 4 hours."""
    try:
        identity_path = pathlib.Path(drive_root) / "memory" / "identity.md"
        if not identity_path.exists():
            return {"fresh": False, "error": "identity.md not found"}
        
        stat = identity_path.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        age_hours = (now - mtime).total_seconds() / 3600
        
        return {
            "fresh": age_hours <= 4,
            "age_hours": round(age_hours, 1),
            "last_modified": mtime.isoformat()
        }
    except Exception as e:
        return {"fresh": False, "error": str(e)}


def _check_recent_errors(events_path: str, window_hours: int = 1) -> Dict[str, Any]:
    """Check recent error rate from events log."""
    try:
        events_file = pathlib.Path(events_path)
        if not events_file.exists():
            return {"available": False, "reason": "No events log"}
        
        cutoff = time.time() - (window_hours * 3600)
        total = 0
        errors = 0
        error_types = {}
        
        with open(events_file, "r") as f:
            for line in f:
                try:
                    event = json.loads(line.strip())
                    ts = event.get("timestamp", 0)
                    if ts < cutoff:
                        continue
                    
                    total += 1
                    if event.get("level") == "ERROR" or "error" in event.get("event", "").lower():
                        errors += 1
                        err_type = event.get("error_type", "unknown")
                        error_types[err_type] = error_types.get(err_type, 0) + 1
                except:
                    continue
        
        error_rate = (errors / total * 100) if total > 0 else 0
        
        return {
            "available": True,
            "window_hours": window_hours,
            "total_events": total,
            "errors": errors,
            "error_rate_pct": round(error_rate, 1),
            "error_types": error_types
        }
    except Exception as e:
        return {"available": False, "error": str(e)}


def _system_health(ctx: ToolContext) -> str:
    """Compute comprehensive system health report."""
    lines = []
    lines.append("## System Health Check\n")
    
    warnings = []
    critical = []
    
    # Memory check
    mem = _get_memory_usage()
    lines.append("### Memory")
    if "error" in mem:
        lines.append(f"  ⚠️ Error: {mem['error']}")
        warnings.append("Memory check failed")
    else:
        lines.append(f"  Total: {mem['total_mb']} MB")
        lines.append(f"  Used: {mem['used_mb']} MB ({mem['usage_pct']:.1f}%)")
        lines.append(f"  Available: {mem['available_mb']} MB")
        if mem['usage_pct'] > 90:
            critical.append(f"Memory critical: {mem['usage_pct']:.1f}% used")
            lines.append("  🚨 CRITICAL: Memory usage > 90%")
        elif mem['usage_pct'] > 80:
            warnings.append(f"Memory high: {mem['usage_pct']:.1f}% used")
            lines.append("  ⚠️ WARNING: Memory usage > 80%")
    
    # Version sync check
    lines.append("\n### Version Sync")
    version = _check_version_sync(ctx.repo_dir)
    if version["synced"]:
        lines.append(f"  ✅ Synced at v{version.get('version', 'unknown')}")
    else:
        lines.append(f"  ⚠️ Out of sync")
        for err in version.get("errors", []):
            lines.append(f"    - {err}")
        warnings.append("Version not synced")
    
    # Identity freshness check
    lines.append("\n### Identity Freshness")
    identity = _check_identity_freshness(ctx.drive_root)
    if identity.get("fresh"):
        lines.append(f"  ✅ Fresh ({identity.get('age_hours')} hours old)")
    else:
        age = identity.get('age_hours', '?')
        lines.append(f"  ⚠️ Stale ({age} hours old)")
        warnings.append(f"identity.md stale ({age}h)")
    
    # Recent errors check
    events_path = pathlib.Path(ctx.drive_root) / "logs" / "events.jsonl"
    lines.append("\n### Recent Errors (1h window)")
    errors = _check_recent_errors(str(events_path))
    if errors.get("available"):
        err_rate = errors.get("error_rate_pct", 0)
        lines.append(f"  Events: {errors.get('total_events', 0)}")
        lines.append(f"  Errors: {errors.get('errors', 0)} ({err_rate}%)")
        if err_rate > 50:
            critical.append(f"High error rate: {err_rate}%")
            lines.append("  🚨 CRITICAL: Error rate > 50%")
        elif err_rate > 20:
            warnings.append(f"Elevated error rate: {err_rate}%")
            lines.append("  ⚠️ WARNING: Error rate > 20%")
    else:
        lines.append(f"  ⚠️ {errors.get('reason', errors.get('error', 'Unknown'))}")
    
    # Summary
    lines.append("\n### Summary")
    if critical:
        lines.append("  🚨 CRITICAL ISSUES:")
        for c in critical:
            lines.append(f"    - {c}")
    if warnings:
        lines.append("  ⚠️ WARNINGS:")
        for w in warnings:
            lines.append(f"    - {w}")
    if not critical and not warnings:
        lines.append("  ✅ All systems healthy")
    
    return "\n".join(lines)


def get_tools():
    return [
        ToolEntry("system_health", {
            "name": "system_health",
            "description": "Check system health: memory, version sync, identity freshness, recent errors. Returns warnings and critical issues.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }, _system_health)
    ]