"""Agent operating-system context settings.

This module stores the OS mode that should be injected into Director and Worker
prompts. It defaults to auto-detection but can be overridden from Settings.
"""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Literal, TypedDict

from app.services import settings_store

AgentOsMode = Literal["auto", "windows", "linux", "macos"]
AgentOsName = Literal["windows", "linux", "macos", "unknown"]

OS_SETTING_KEY = "agent_os_mode"
VALID_OS_MODES: set[str] = {"auto", "windows", "linux", "macos"}


class AgentOsConfig(TypedDict):
    mode: AgentOsMode
    detected_os: AgentOsName
    selected_os: AgentOsName
    source: Literal["auto", "manual"]
    label: str
    shell: str
    path_examples: list[str]
    prompt_context: str


def detect_current_os() -> AgentOsName:
    """Return the current host OS normalized for agent prompts."""
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "macos"
    if system == "linux":
        return "linux"
    return "unknown"


def normalize_os_mode(value: str | None) -> AgentOsMode:
    """Normalize a persisted/API OS mode value."""
    mode = (value or "auto").strip().lower()
    if mode not in VALID_OS_MODES:
        return "auto"
    return mode  # type: ignore[return-value]


def _label(os_name: AgentOsName) -> str:
    return {
        "windows": "Windows",
        "linux": "Linux",
        "macos": "macOS",
        "unknown": "Unknown OS",
    }[os_name]


def _shell(os_name: AgentOsName) -> str:
    if os_name == "windows":
        return "Windows cmd.exe / PowerShell commands; do not use POSIX-only commands like mkdir -p, touch, rm, cp, mv, grep, sed, awk, or Unix /home paths."
    if os_name == "macos":
        return "POSIX shell commands for macOS; user paths normally start with the exact Home path shown below."
    if os_name == "linux":
        return "POSIX shell commands for Linux; user paths normally start with the exact Home path shown below."
    return "Unknown shell; prefer backend file tools over shell commands."


def _path_examples(os_name: AgentOsName) -> list[str]:
    home = Path.home()
    desktop = home / "Desktop"
    if os_name == "windows":
        return [
            f"Home={home}",
            f"Desktop={desktop}",
            f"To create a Desktop file with write_file, use an absolute path like: {desktop / 'hello.txt'}",
            "Use backslash or escaped absolute Windows paths when writing direct paths.",
            "Never output placeholder paths like C:\\Users\\<user>, /home/<user>, or /Users/<user>; use the exact Home/Desktop paths above.",
            "For simple file creation, prefer the write_file tool over run_command.",
        ]
    if os_name == "macos":
        return [
            f"Home={home}",
            f"Desktop={desktop}",
            f"To create a Desktop file with write_file, use an absolute path like: {desktop / 'hello.txt'}",
            "Use POSIX absolute paths for shell commands.",
            "Never output placeholder paths like /Users/<user> or /home/<user>; use the exact Home/Desktop paths above.",
        ]
    if os_name == "linux":
        return [
            f"Home={home}",
            f"Desktop={desktop}",
            f"To create a Desktop file with write_file, use an absolute path like: {desktop / 'hello.txt'}",
            "Use POSIX absolute paths for shell commands.",
            "Never output placeholder paths like /home/<user> or /Users/<user>; use the exact Home/Desktop paths above.",
        ]
    return [
        f"Home={home}",
        f"Desktop={desktop}",
        "Ask for clarification before emitting OS-specific shell commands.",
    ]


def build_prompt_context(config: AgentOsConfig) -> str:
    """Render OS settings as a compact prompt block for agents."""
    examples = "\n".join(f"- {item}" for item in config["path_examples"])
    return (
        "Runtime operating system context (mandatory):\n"
        f"- Selected OS for agent execution: {config['label']} ({config['selected_os']}).\n"
        f"- OS source: {config['source']} (detected host: {config['detected_os']}, setting mode: {config['mode']}).\n"
        f"- Shell/path rules: {config['shell']}\n"
        f"{examples}\n"
        "When creating/editing files, prefer backend file tools. If you must run terminal commands, they MUST match this OS. "
        "Never copy placeholder tokens such as <user>; use exact paths from Home/Desktop above."
    )


async def get_agent_os_config() -> AgentOsConfig:
    """Load the effective agent OS config from settings plus host detection."""
    detected = detect_current_os()
    mode = normalize_os_mode(await settings_store.get_setting(OS_SETTING_KEY))
    selected: AgentOsName = detected if mode == "auto" else mode
    if selected == "unknown":
        selected = detected
    source = "auto" if mode == "auto" else "manual"
    config: AgentOsConfig = {
        "mode": mode,
        "detected_os": detected,
        "selected_os": selected,
        "source": source,
        "label": _label(selected),
        "shell": _shell(selected),
        "path_examples": _path_examples(selected),
        "prompt_context": "",
    }
    config["prompt_context"] = build_prompt_context(config)
    return config


async def set_agent_os_mode(mode: str) -> AgentOsConfig:
    """Persist the desired agent OS mode and return the effective config."""
    normalized = normalize_os_mode(mode)
    await settings_store.set_setting(OS_SETTING_KEY, normalized)
    return await get_agent_os_config()


async def agent_os_prompt_context() -> str:
    """Return only the prompt-ready OS context block."""
    return (await get_agent_os_config())["prompt_context"]


__all__ = [
    "AgentOsConfig",
    "AgentOsMode",
    "OS_SETTING_KEY",
    "agent_os_prompt_context",
    "detect_current_os",
    "get_agent_os_config",
    "normalize_os_mode",
    "set_agent_os_mode",
]
