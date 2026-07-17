"""Load immutable packaged prompts by logical role."""

from __future__ import annotations

from importlib import resources
from typing import Any

import yaml

from chemex_lit.errors import ConfigurationError


class PromptRegistry:
    """Resolve prompt files from one packaged manifest."""

    def __init__(self) -> None:
        root = resources.files("chemex_lit.resources").joinpath("prompts")
        manifest = yaml.safe_load(root.joinpath("manifest.yaml").read_text(encoding="utf-8"))
        self._root = root
        self._entries: dict[str, dict[str, Any]] = manifest.get("prompts", {})

    @property
    def versions(self) -> dict[str, str]:
        return {name: str(entry["version"]) for name, entry in self._entries.items()}

    def render(self, name: str, replacements: dict[str, str]) -> str:
        if name not in self._entries:
            raise ConfigurationError(f"Unknown prompt: {name}")
        entry = self._entries[name]
        raw = yaml.safe_load(
            self._root.joinpath(entry["file"]).read_text(encoding="utf-8")
        )
        template = raw.get("template", "")
        if not template:
            raise ConfigurationError(f"Prompt {name} has no template")
        for token, value in replacements.items():
            template = template.replace(token, value)
        unresolved = [token for token in ("<<CONTENT>>", "<<PAGE_CONTEXT>>", "<<IMAGE_CONTEXT>>", "<<RECORD>>", "<<EVIDENCE>>") if token in template]
        if unresolved:
            raise ConfigurationError(f"Prompt {name} has unresolved tokens: {unresolved}")
        return template
