"""
OpenClaw Skill Adapter for Ouroboros

Provides compatibility layer to load and execute OpenClaw-format skills
within Ouroboros tool system.
"""

import os
import sys
import json
import importlib.util
import inspect
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass


@dataclass
class SkillInfo:
    """OpenClaw skill metadata"""
    name: str
    version: str
    description: str
    author: Optional[str] = None
    entry_point: Optional[str] = None
    config_schema: Optional[Dict] = None


class OpenClawSkillAdapter:
    """
    Adapter to run OpenClaw skills in Ouroboros environment.
    
    OpenClaw skills are standard Python packages with AgentSkills protocol.
    This adapter bridges the gap between OpenClaw's runtime and Ouroboros.
    """
    
    def __init__(self, skill_paths: List[str] = None):
        self.skill_paths = skill_paths or [
            str(Path.home() / ".openclaw" / "skills"),
            "/opt/openclaw/skills",
            "./skills",
        ]
        self.loaded_skills: Dict[str, Any] = {}
        self.skill_infos: Dict[str, SkillInfo] = {}
        
    def discover_skills(self) -> List[SkillInfo]:
        """Scan skill paths and discover available skills"""
        discovered = []
        
        for path in self.skill_paths:
            if not os.path.exists(path):
                continue
                
            for item in os.listdir(path):
                skill_dir = os.path.join(path, item)
                if not os.path.isdir(skill_dir):
                    continue
                    
                # Look for skill.json or pyproject.toml
                skill_json = os.path.join(skill_dir, "skill.json")
                pyproject_toml = os.path.join(skill_dir, "pyproject.toml")
                
                info = None
                if os.path.exists(skill_json):
                    info = self._parse_skill_json(skill_json)
                elif os.path.exists(pyproject_toml):
                    info = self._parse_pyproject_toml(pyproject_toml)
                    
                if info:
                    info.entry_point = skill_dir
                    discovered.append(info)
                    
        return discovered
    
    def _parse_skill_json(self, path: str) -> Optional[SkillInfo]:
        """Parse OpenClaw skill.json format"""
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            return SkillInfo(
                name=data.get("name", "unknown"),
                version=data.get("version", "0.0.1"),
                description=data.get("description", ""),
                author=data.get("author"),
                config_schema=data.get("config_schema")
            )
        except Exception:
            return None
    
    def _parse_pyproject_toml(self, path: str) -> Optional[SkillInfo]:
        """Parse pyproject.toml for skill metadata"""
        try:
            import tomllib
            with open(path, 'rb') as f:
                data = tomllib.load(f)
            
            tool = data.get("tool", {})
            openclaw = tool.get("openclaw", {})
            
            if not openclaw:
                # Try generic project info
                project = data.get("project", {})
                return SkillInfo(
                    name=project.get("name", "unknown"),
                    version=project.get("version", "0.0.1"),
                    description=project.get("description", ""),
                    author=project.get("authors", [{}])[0].get("name") if project.get("authors") else None
                )
            
            return SkillInfo(
                name=openclaw.get("name", "unknown"),
                version=openclaw.get("version", "0.0.1"),
                description=openclaw.get("description", ""),
                author=openclaw.get("author"),
                config_schema=openclaw.get("config_schema")
            )
        except Exception:
            return None
    
    def load_skill(self, skill_name: str) -> bool:
        """Load a skill by name"""
        if skill_name in self.loaded_skills:
            return True
            
        # Find skill in paths
        for path in self.skill_paths:
            skill_dir = os.path.join(path, skill_name)
            if os.path.exists(skill_dir):
                return self._load_from_directory(skill_name, skill_dir)
                
        return False
    
    def _load_from_directory(self, name: str, directory: str) -> bool:
        """Load skill from directory"""
        try:
            # Add to path
            if directory not in sys.path:
                sys.path.insert(0, directory)
            
            # Look for main module
            main_file = os.path.join(directory, "__init__.py")
            if not os.path.exists(main_file):
                main_file = os.path.join(directory, f"{name}.py")
            
            if not os.path.exists(main_file):
                return False
            
            # Load module
            spec = importlib.util.spec_from_file_location(name, main_file)
            if spec is None or spec.loader is None:
                return False
                
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            
            self.loaded_skills[name] = module
            
            # Extract skill info if available
            if hasattr(module, "SKILL_INFO"):
                self.skill_infos[name] = SkillInfo(**module.SKILL_INFO)
            
            return True
            
        except Exception as e:
            print(f"Failed to load skill {name}: {e}")
            return False
    
    def execute_skill(self, skill_name: str, method: str, **kwargs) -> Any:
        """Execute a skill method"""
        if skill_name not in self.loaded_skills:
            if not self.load_skill(skill_name):
                raise ValueError(f"Skill {skill_name} not found")
        
        skill = self.loaded_skills[skill_name]
        
        # Find method
        if hasattr(skill, method):
            func = getattr(skill, method)
            if callable(func):
                return func(**kwargs)
        
        raise ValueError(f"Method {method} not found in skill {skill_name}")
    
    def get_skill_tools(self, skill_name: str) -> List[Dict]:
        """
        Convert skill methods to Ouroboros tool format.
        Returns list of tool definitions for LLM.
        """
        if skill_name not in self.loaded_skills:
            if not self.load_skill(skill_name):
                return []
        
        skill = self.loaded_skills[skill_name]
        tools = []
        
        for name, obj in inspect.getmembers(skill):
            if inspect.isfunction(obj) or inspect.ismethod(obj):
                # Skip private methods
                if name.startswith("_"):
                    continue
                
                # Get signature
                sig = inspect.signature(obj)
                params = {}
                required = []
                
                for param_name, param in sig.parameters.items():
                    if param_name in ("self", "cls"):
                        continue
                    
                    param_info = {"type": "string"}
                    if param.default is not param.empty:
                        param_info["default"] = param.default
                    else:
                        required.append(param_name)
                    
                    # Try to infer type from annotation
                    if param.annotation is not param.empty:
                        param_info["type"] = self._python_type_to_json(param.annotation)
                    
                    params[param_name] = param_info
                
                tool_def = {
                    "name": f"{skill_name}_{name}",
                    "description": obj.__doc__ or f"Execute {name} from {skill_name}",
                    "parameters": {
                        "type": "object",
                        "properties": params,
                        "required": required
                    },
                    "_skill_name": skill_name,
                    "_method_name": name
                }
                tools.append(tool_def)
        
        return tools
    
    def _python_type_to_json(self, py_type) -> str:
        """Convert Python type to JSON schema type"""
        type_map = {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            list: "array",
            dict: "object",
        }
        return type_map.get(py_type, "string")


# Global adapter instance
_adapter: Optional[OpenClawSkillAdapter] = None


def get_adapter() -> OpenClawSkillAdapter:
    """Get or create global adapter instance"""
    global _adapter
    if _adapter is None:
        _adapter = OpenClawSkillAdapter()
    return _adapter


def discover_skills() -> List[SkillInfo]:
    """Discover available OpenClaw skills"""
    return get_adapter().discover_skills()


def load_skill(skill_name: str) -> bool:
    """Load a skill by name"""
    return get_adapter().load_skill(skill_name)


def execute_skill(skill_name: str, method: str, **kwargs) -> Any:
    """Execute a skill method"""
    return get_adapter().execute_skill(skill_name, method, **kwargs)


def get_skill_tools(skill_name: str) -> List[Dict]:
    """Get tool definitions for a loaded skill"""
    return get_adapter().get_skill_tools(skill_name)
