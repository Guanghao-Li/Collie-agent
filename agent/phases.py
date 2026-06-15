from __future__ import annotations

from enum import Enum
from typing import Protocol
import inspect

from agent.frame import TurnFrame
from agent.slots import apply_abort_reply_slot


class PhaseName(str, Enum):
    BEFORE_TURN = "before_turn"
    BEFORE_REASONING = "before_reasoning"
    PROMPT_RENDER = "prompt_render"
    REASONER = "reasoner"
    AFTER_REASONING = "after_reasoning"
    AFTER_TURN = "after_turn"


PHASE_SEQUENCE: tuple[PhaseName, ...] = (
    PhaseName.BEFORE_TURN,
    PhaseName.BEFORE_REASONING,
    PhaseName.PROMPT_RENDER,
    PhaseName.REASONER,
    PhaseName.AFTER_REASONING,
    PhaseName.AFTER_TURN,
)


class PhaseModule(Protocol):
    phase: PhaseName | str
    priority: int

    def run(self, frame: TurnFrame) -> object:
        ...


class PhaseRunner:
    def __init__(self) -> None:
        self._modules: dict[PhaseName, list[PhaseModule]] = {
            phase: [] for phase in PhaseName
        }

    def register(self, module: PhaseModule) -> None:
        phase = PhaseName(module.phase)
        self._modules[phase].append(module)
        self._modules[phase].sort(key=lambda item: item.priority)

    def modules_for(self, phase: PhaseName | str) -> list[PhaseModule]:
        return list(self._modules[PhaseName(phase)])

    async def run(self, phase: PhaseName | str, frame: TurnFrame) -> None:
        was_aborted_at_start = frame.abort
        for module in self.modules_for(phase):
            result = module.run(frame)
            if inspect.isawaitable(result):
                await result
            apply_abort_reply_slot(frame)
            if frame.abort and not was_aborted_at_start:
                break
