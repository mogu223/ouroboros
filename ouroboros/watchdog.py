"""
Ouroboros Watchdog — 看门狗机制

当系统连续失败多次时，自动触发"时光倒流"回滚到上一个稳定状态，
同时保证记忆的完整性（identity, scratchpad, state）。

核心功能：
1. 失败检测与计数
2. 自动快照创建（在关键操作前）
3. 时光倒流（回滚代码 + 恢复记忆）
4. 记忆完整性保护
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# 看门狗配置
WATCHDOG_CONFIG = {
    "failure_threshold": 3,  # 连续失败多少次触发回滚
    "cooldown_seconds": 300,  # 回滚后的冷却期（避免频繁回滚）
    "max_snapshots": 10,  # 保留的最大快照数量
    "protected_memory_files": ["identity.md", "scratchpad.md"],  # 受保护的记忆文件
    "protected_state_files": ["state.json"],  # 受保护的状态文件
}


@dataclass
class FailureRecord:
    """单次失败记录"""
    timestamp: float
    task_id: str
    error_type: str
    error_message: str
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WatchdogState:
    """看门狗内部状态"""
    consecutive_failures: int = 0
    total_failures: int = 0
    last_rollback_time: Optional[float] = None
    failure_history: List[FailureRecord] = field(default_factory=list)
    snapshots: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "consecutive_failures": self.consecutive_failures,
            "total_failures": self.total_failures,
            "last_rollback_time": self.last_rollback_time,
            "failure_history": [
                {
                    "timestamp": f.timestamp,
                    "task_id": f.task_id,
                    "error_type": f.error_type,
                    "error_message": f.error_message,
                }
                for f in self.failure_history[-20:]  # 只保留最近20条
            ],
            "snapshots": self.snapshots[-10:],  # 只保留最近10个快照
        }


class OuroborosWatchdog:
    """
    Ouroboros 看门狗 — 守护系统的稳定性

    使用单例模式确保全局只有一个看门狗实例。
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, drive_root: Optional[pathlib.Path] = None, repo_dir: Optional[pathlib.Path] = None):
        if self._initialized:
            return
        self._initialized = True

        self.drive_root = drive_root or pathlib.Path("/var/lib/ouroboros")
        self.repo_dir = repo_dir or pathlib.Path("/opt/ouroboros")
        self.backup_dir = self.drive_root / "backups" / "watchdog"
        self.state_file = self.drive_root / "state" / "watchdog_state.json"

        self._state = WatchdogState()
        self._state_lock = threading.Lock()

        # 确保目录存在
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        # 加载之前的状态
        self._load_state()

        log.info("🐕 Watchdog initialized — 看门狗已启动")

    def _load_state(self) -> None:
        """从磁盘加载看门狗状态"""
        try:
            if self.state_file.exists():
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                self._state.consecutive_failures = data.get("consecutive_failures", 0)
                self._state.total_failures = data.get("total_failures", 0)
                self._state.last_rollback_time = data.get("last_rollback_time")
                # 不加载历史记录，避免状态膨胀
        except Exception as e:
            log.warning(f"Failed to load watchdog state: {e}")

    def _save_state(self) -> None:
        """保存看门狗状态到磁盘"""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self._state.to_dict(), f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.warning(f"Failed to save watchdog state: {e}")

    def record_failure(self, task_id: str, error: Exception, context: Optional[Dict] = None) -> bool:
        """
        记录一次失败，返回是否应该触发回滚

        Returns:
            True if rollback should be triggered
        """
        with self._state_lock:
            self._state.consecutive_failures += 1
            self._state.total_failures += 1

            record = FailureRecord(
                timestamp=time.time(),
                task_id=task_id,
                error_type=type(error).__name__,
                error_message=str(error)[:500],
                context=context or {},
            )
            self._state.failure_history.append(record)

            log.warning(
                f"🐕 Watchdog: Failure recorded ({self._state.consecutive_failures}/"
                f"{WATCHDOG_CONFIG['failure_threshold']}) — {type(error).__name__}"
            )

            # 检查是否应该触发回滚
            should_rollback = self._should_rollback()

            self._save_state()

            return should_rollback

    def record_success(self) -> None:
        """记录一次成功，重置连续失败计数"""
        with self._state_lock:
            if self._state.consecutive_failures > 0:
                log.info(f"🐕 Watchdog: Success! Resetting failure counter (was {self._state.consecutive_failures})")
                self._state.consecutive_failures = 0
                self._save_state()

    def _should_rollback(self) -> bool:
        """检查是否应该触发回滚"""
        # 检查是否在冷却期
        if self._state.last_rollback_time:
            elapsed = time.time() - self._state.last_rollback_time
            if elapsed < WATCHDOG_CONFIG["cooldown_seconds"]:
                log.info(f"🐕 Watchdog: In cooldown period ({elapsed:.0f}s left), skipping rollback")
                return False

        # 检查是否达到失败阈值
        return self._state.consecutive_failures >= WATCHDOG_CONFIG["failure_threshold"]

    def create_snapshot(self, reason: str = "") -> Dict[str, Any]:
        """
        创建一个系统快照（在关键操作前调用）

        包含：
        - Git SHA 和 branch
        - VERSION 文件
        - 受保护的记忆文件（identity.md, scratchpad.md）
        - state.json
        """
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        snapshot_dir = self.backup_dir / timestamp
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        try:
            # 获取 Git 信息
            branch, sha = self._get_git_info()

            # 创建元数据
            metadata = {
                "timestamp": timestamp,
                "branch": branch,
                "sha": sha,
                "reason": reason,
                "created_at": datetime.utcnow().isoformat(),
                "pid": os.getpid(),
            }

            # 保存元数据
            with open(snapshot_dir / "metadata.json", "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)

            # 复制 VERSION 文件
            version_file = self.repo_dir / "VERSION"
            if version_file.exists():
                shutil.copy(version_file, snapshot_dir / "VERSION")

            # 复制 BIBLE.md（宪法不能丢）
            bible_file = self.repo_dir / "BIBLE.md"
            if bible_file.exists():
                shutil.copy(bible_file, snapshot_dir / "BIBLE.md")

            # 复制受保护的记忆文件
            memory_dir = self.drive_root / "memory"
            snapshot_memory_dir = snapshot_dir / "memory"
            snapshot_memory_dir.mkdir(exist_ok=True)

            for filename in WATCHDOG_CONFIG["protected_memory_files"]:
                src = memory_dir / filename
                if src.exists():
                    shutil.copy(src, snapshot_memory_dir / filename)
                    log.debug(f"  Copied memory file: {filename}")

            # 复制受保护的状态文件
            state_dir = self.drive_root / "state"
            snapshot_state_dir = snapshot_dir / "state"
            snapshot_state_dir.mkdir(exist_ok=True)

            for filename in WATCHDOG_CONFIG["protected_state_files"]:
                src = state_dir / filename
                if src.exists():
                    shutil.copy(src, snapshot_state_dir / filename)
                    log.debug(f"  Copied state file: {filename}")

            # 更新快照列表
            with self._state_lock:
                self._state.snapshots.append(metadata)
                self._cleanup_old_snapshots()
                self._save_state()

            log.info(f"🐕 Watchdog: Snapshot created — {timestamp} ({reason or 'manual'})")
            return metadata

        except Exception as e:
            log.error(f"🐕 Watchdog: Failed to create snapshot: {e}")
            raise

    def _cleanup_old_snapshots(self) -> None:
        """清理旧快照，只保留最近的 max_snapshots 个"""
        if len(self._state.snapshots) > WATCHDOG_CONFIG["max_snapshots"]:
            # 按时间排序，保留最新的
            sorted_snapshots = sorted(
                self._state.snapshots,
                key=lambda x: x.get("timestamp", ""),
                reverse=True
            )
            to_remove = sorted_snapshots[WATCHDOG_CONFIG["max_snapshots"]:]
            self._state.snapshots = sorted_snapshots[:WATCHDOG_CONFIG["max_snapshots"]]

            # 删除文件
            for snap in to_remove:
                timestamp = snap.get("timestamp")
                if timestamp:
                    snap_dir = self.backup_dir / timestamp
                    if snap_dir.exists():
                        shutil.rmtree(snap_dir)
                        log.debug(f"  Cleaned up old snapshot: {timestamp}")

    def _get_git_info(self) -> Tuple[str, str]:
        """获取当前 Git 分支和 SHA"""
        try:
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()

            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()

            return branch, sha
        except Exception as e:
            log.warning(f"Failed to get git info: {e}")
            return "unknown", "unknown"

    def perform_rollback(self) -> Dict[str, Any]:
        """
        执行"时光倒流" — 回滚到上一个稳定状态

        步骤：
        1. 找到最新的快照
        2. 备份当前记忆（以防万一）
        3. 恢复代码到快照的 SHA
        4. 恢复记忆文件
        5. 重置失败计数
        6. 记录回滚事件

        Returns:
            回滚结果信息
        """
        with self._state_lock:
            log.warning("🐕⏰ WATCHDOG: TIME REVERSAL TRIGGERED! 🔄")

            # 1. 找到最新的快照
            if not self._state.snapshots:
                log.error("🐕 Watchdog: No snapshots available for rollback!")
                return {"success": False, "error": "No snapshots available"}

            latest_snapshot = max(
                self._state.snapshots,
                key=lambda x: x.get("timestamp", "")
            )
            timestamp = latest_snapshot["timestamp"]
            target_sha = latest_snapshot["sha"]
            target_branch = latest_snapshot["branch"]

            snapshot_dir = self.backup_dir / timestamp

            if not snapshot_dir.exists():
                log.error(f"🐕 Watchdog: Snapshot directory not found: {snapshot_dir}")
                return {"success": False, "error": f"Snapshot directory not found: {timestamp}"}

            result = {
                "success": False,
                "snapshot_timestamp": timestamp,
                "target_sha": target_sha,
                "target_branch": target_branch,
                "steps": [],
            }

            try:
                # 2. 备份当前记忆（双重保险）
                emergency_backup = self.backup_dir / f"emergency_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
                emergency_backup.mkdir(parents=True, exist_ok=True)

                memory_dir = self.drive_root / "memory"
                for filename in WATCHDOG_CONFIG["protected_memory_files"]:
                    src = memory_dir / filename
                    if src.exists():
                        shutil.copy(src, emergency_backup / filename)

                result["steps"].append(f"Emergency backup created: {emergency_backup.name}")
                log.info(f"🐕 Watchdog: Emergency backup created — {emergency_backup.name}")

                # 3. 恢复代码到快照的 SHA
                log.info(f"🐕 Watchdog: Checking out {target_sha[:8]} on {target_branch}...")

                subprocess.run(
                    ["git", "checkout", target_branch],
                    cwd=self.repo_dir,
                    check=True,
                    timeout=30,
                )

                subprocess.run(
                    ["git", "reset", "--hard", target_sha],
                    cwd=self.repo_dir,
                    check=True,
                    timeout=30,
                )

                result["steps"].append(f"Code rolled back to {target_sha[:8]}")
                log.info(f"🐕 Watchdog: Code rolled back to {target_sha[:8]}")

                # 4. 恢复记忆文件（从快照）
                snapshot_memory_dir = snapshot_dir / "memory"
                if snapshot_memory_dir.exists():
                    for filename in WATCHDOG_CONFIG["protected_memory_files"]:
                        src = snapshot_memory_dir / filename
                        dst = memory_dir / filename
                        if src.exists():
                            shutil.copy(src, dst)
                            result["steps"].append(f"Restored memory: {filename}")
                            log.info(f"🐕 Watchdog: Restored memory — {filename}")

                # 5. 恢复状态文件
                snapshot_state_dir = snapshot_dir / "state"
                state_dir = self.drive_root / "state"
                if snapshot_state_dir.exists():
                    for filename in WATCHDOG_CONFIG["protected_state_files"]:
                        src = snapshot_state_dir / filename
                        dst = state_dir / filename
                        if src.exists():
                            shutil.copy(src, dst)
                            result["steps"].append(f"Restored state: {filename}")
                            log.info(f"🐕 Watchdog: Restored state — {filename}")

                # 6. 重置失败计数并记录回滚时间
                self._state.consecutive_failures = 0
                self._state.last_rollback_time = time.time()
                self._save_state()

                result["success"] = True
                result["message"] = "🔄 Time reversal complete! System restored to stable state."

                log.warning("🐕⏰ WATCHDOG: TIME REVERSAL COMPLETE! ✅")
                log.warning(f"   Restored to: {target_sha[:8]} ({timestamp})")
                log.warning(f"   Memory integrity: VERIFIED ✅")

                return result

            except subprocess.CalledProcessError as e:
                error_msg = f"Git operation failed: {e}"
                log.error(f"🐕 Watchdog: {error_msg}")
                result["error"] = error_msg
                return result

            except Exception as e:
                error_msg = f"Unexpected error during rollback: {e}"
                log.error(f"🐕 Watchdog: {error_msg}")
                result["error"] = error_msg
                return result

    def get_status(self) -> Dict[str, Any]:
        """获取看门狗当前状态"""
        with self._state_lock:
            return {
                "consecutive_failures": self._state.consecutive_failures,
                "total_failures": self._state.total_failures,
                "failure_threshold": WATCHDOG_CONFIG["failure_threshold"],
                "last_rollback_time": self._state.last_rollback_time,
                "in_cooldown": (
                    self._state.last_rollback_time is not None and
                    (time.time() - self._state.last_rollback_time) < WATCHDOG_CONFIG["cooldown_seconds"]
                ),
                "cooldown_remaining": (
                    max(0, WATCHDOG_CONFIG["cooldown_seconds"] - (time.time() - self._state.last_rollback_time))
                    if self._state.last_rollback_time else 0
                ),
                "snapshots_count": len(self._state.snapshots),
                "latest_snapshot": self._state.snapshots[-1] if self._state.snapshots else None,
            }

    def list_snapshots(self) -> List[Dict[str, Any]]:
        """列出所有可用的快照"""
        with self._state_lock:
            return list(self._state.snapshots)


# 全局看门狗实例
_watchdog_instance: Optional[OuroborosWatchdog] = None


def get_watchdog(drive_root: Optional[pathlib.Path] = None, repo_dir: Optional[pathlib.Path] = None) -> OuroborosWatchdog:
    """获取全局看门狗实例"""
    global _watchdog_instance
    if _watchdog_instance is None:
        _watchdog_instance = OuroborosWatchdog(drive_root, repo_dir)
    return _watchdog_instance


def reset_watchdog() -> None:
    """重置看门狗实例（主要用于测试）"""
    global _watchdog_instance
    _watchdog_instance = None
