from __future__ import annotations

from dataclasses import dataclass
import shutil
import subprocess
import time
from typing import Collection, Sequence


@dataclass(frozen=True)
class AgentSpec:
    name: str
    argv_template: tuple[str, ...]

    def render(self, prompt: str) -> list[str]:
        return [part.replace("{prompt}", prompt) for part in self.argv_template]


@dataclass
class AgentLease:
    agent_name: str
    process: subprocess.Popen[str]

    @property
    def is_alive(self) -> bool:
        return self.process.poll() is None

    def close(self, timeout: float = 1.0) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=timeout)


class AgentPool:
    def __init__(
        self,
        candidates: Sequence[AgentSpec],
        startup_probe_seconds: float = 0.25,
    ) -> None:
        self._candidates = tuple(candidates)
        self._startup_probe_seconds = startup_probe_seconds

    @property
    def candidate_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self._candidates)

    def try_acquire(
        self,
        prompt: str,
        exclude: Collection[str] = (),
    ) -> AgentLease | None:
        excluded = set(exclude)
        for spec in self._candidates:
            if spec.name in excluded:
                continue
            argv = spec.render(prompt)
            if not argv:
                continue
            executable = shutil.which(argv[0])
            if executable is None:
                continue
            argv[0] = executable
            try:
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            except OSError:
                continue
            time.sleep(self._startup_probe_seconds)
            if process.poll() is None:
                return AgentLease(agent_name=spec.name, process=process)
        return None
