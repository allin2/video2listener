"""配置加载模块。读取 config.yaml，环境变量覆盖敏感字段。"""

import os
from pathlib import Path
from typing import Any

import yaml


def _find_project_root() -> Path:
    """从当前文件向上查找包含 config.yaml 的目录作为项目根。"""
    current = Path(__file__).resolve().parent.parent
    if (current / "config.yaml").exists():
        return current
    # fallback: cwd
    cwd = Path.cwd()
    if (cwd / "config.yaml").exists():
        return cwd
    raise FileNotFoundError(
        "无法找到 config.yaml。请确保从项目根目录运行，"
        "或 config.yaml 与 src/ 同级。"
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _apply_env_overrides(cfg: dict[str, Any]) -> dict[str, Any]:
    """用环境变量覆盖配置中的敏感字段。"""
    env_map = {
        "VIDEO2LISTENER_DEEPSEEK_API_KEY": ("llm", "api_key"),
        "VIDEO2LISTENER_DEEPSEEK_BASE_URL": ("llm", "base_url"),
        "VIDEO2LISTENER_FISH_API_KEY": ("tts", "api_key"),
        "VIDEO2LISTENER_FISH_BASE_URL": ("tts", "base_url"),
        "VIDEO2LISTENER_MIMI_API_KEY": ("tts", "api_key"),
    }
    for env_var, (section, key) in env_map.items():
        value = os.environ.get(env_var)
        if value:
            cfg.setdefault(section, {})[key] = value
    return cfg


def _ensure_dirs(cfg: dict[str, Any]) -> None:
    """确保数据目录存在。"""
    root = cfg["_project_root"]
    data_dir = root / cfg["app"]["data_dir"]
    db_dir = (root / cfg["app"]["db_path"]).parent
    data_dir.mkdir(parents=True, exist_ok=True)
    db_dir.mkdir(parents=True, exist_ok=True)


_PROJECT_ROOT = _find_project_root()
_config_data = _load_yaml(_PROJECT_ROOT / "config.yaml")
_config_data["_project_root"] = _PROJECT_ROOT
_config_data = _apply_env_overrides(_config_data)
_ensure_dirs(_config_data)


def get_config() -> dict[str, Any]:
    """返回完整配置字典。"""
    return _config_data


def get_project_root() -> Path:
    """返回项目根目录路径。"""
    return _PROJECT_ROOT
