from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class PipelineRuntime:
    config_dir: Path
    use_llm: bool = False
    llm_max_batches: int | None = None
    model_config: dict[str, Any] = field(default_factory=dict)
    run_events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, **event: Any) -> None:
        self.run_events.append(event)
