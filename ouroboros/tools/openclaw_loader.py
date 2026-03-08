"""OpenClaw Skill 加载器 - 最小可行版本

让 Ouroboros 能直接加载 OpenClaw 格式的 Skill 包。
OpenClaw Skill 是标准 Python 包，通过 __init__.py 暴露能力。
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional


class OpenClawSkillLoader:
    """加载 OpenClaw 格式的 Skill 包"""
    
    def __init__(self, skills_dir: Optional[Path] = None):
        self.skills_dir = skills_dir or Path.home() / ".openclaw" / "skills"
        self.loaded_skills: Dict[str, Any] = {}
    
    def load_skill(self, skill_name: str, skill_path: Optional[Path] = None) -> Dict[str, Callable]:
        """加载一个 Skill，返回其暴露的工具函数字典"""
        if skill_name in self.loaded_skills:
            return self.loaded_skills[skill_name]
        
        # 确定 Skill 路径
        if skill_path is None:
            skill_path = self.skills_dir / skill_name
        
        if not skill_path.exists():
            raise FileNotFoundError(f"Skill not found: {skill_path}")
        
        # 动态加载 Python 包
        init_file = skill_path / "__init__.py"
        if not init_file.exists():
            raise ValueError(f"Invalid Skill: no __init__.py in {skill_path}")
        
        spec = importlib.util.spec_from_file_location(skill_name, init_file)
        if not spec or not spec.loader:
            raise ImportError(f"Cannot load spec for {skill_name}")
        
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"openclaw_skill.{skill_name}"] = module
        spec.loader.exec_module(module)
        
        # 提取 Skill 暴露的工具
        tools = {}
        
        # OpenClaw Skill 通过 __all__ 或特定命名约定暴露工具
        if hasattr(module, '__all__'):
            for name in module.__all__:
                obj = getattr(module, name)
                if callable(obj):
                    tools[name] = obj
        else:
            # 默认：暴露所有 callable，排除私有和内置
            for name in dir(module):
                if not name.startswith('_'):
                    obj = getattr(module, name)
                    if callable(obj) and not isinstance(obj, type):
                        tools[name] = obj
        
        self.loaded_skills[skill_name] = tools
        return tools
    
    def list_available_skills(self) -> list:
        """列出可用的 Skill"""
        if not self.skills_dir.exists():
            return []
        return [d.name for d in self.skills_dir.iterdir() if d.is_dir() and (d / "__init__.py").exists()]


# 全局加载器实例
_skill_loader: Optional[OpenClawSkillLoader] = None


def get_skill_loader() -> OpenClawSkillLoader:
    """获取全局 Skill 加载器"""
    global _skill_loader
    if _skill_loader is None:
        _skill_loader = OpenClawSkillLoader()
    return _skill_loader


def load_skill(skill_name: str, skill_path: Optional[Path] = None) -> Dict[str, Callable]:
    """便捷函数：加载指定 Skill"""
    return get_skill_loader().load_skill(skill_name, skill_path)
