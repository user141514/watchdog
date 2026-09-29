from __future__ import annotations

import unittest

from chat_watchdog.prompt_contract import (
    BASE_CONTINUATION_PROMPT,
    render_continuation_prompt,
)


class PromptContractTests(unittest.TestCase):
    def test_rendered_prompt_preserves_watchdog_envelope_and_adaptive_step(self) -> None:
        rendered = render_continuation_prompt(
            task_id="task-alpha",
            version=4,
            step_index=7,
            step_prompt="只做 EXP-004 的最小判别实验，不扩展其它方向。",
        )

        self.assertIn(BASE_CONTINUATION_PROMPT, rendered)
        self.assertIn("SUPERVISOR_DONE", rendered)
        self.assertIn("[SUPERVISOR_STATE: NEED_INPUT]", rendered)
        self.assertIn("task_id=task-alpha", rendered)
        self.assertIn("prompt_version=4", rendered)
        self.assertIn("step_index=7", rendered)
        self.assertIn("只做 EXP-004 的最小判别实验", rendered)


if __name__ == "__main__":
    unittest.main()
