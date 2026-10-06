from chat_watchdog.fixed_action_timer import FIXED_ACTION_PROMPT
from chat_watchdog.simple_watchdog import (
    ACTION_RECOVERY_PROMPT,
    REVIEW_PROMPT,
    REVIEW_RECOVERY_PROMPT,
)
from chat_watchdog.supervisor import CONTINUE_PROMPT


_PROVIDER_RULE_FRAGMENT = "必须使用 OMP，不要使用 Claude"
_NO_FALLBACK_FRAGMENT = "OMP 不可用时不要自动降级到 Claude"


def test_watchdog_prompts_pin_orca_subagents_to_omp() -> None:
    prompts = (
        FIXED_ACTION_PROMPT,
        CONTINUE_PROMPT,
        ACTION_RECOVERY_PROMPT,
        REVIEW_PROMPT,
        REVIEW_RECOVERY_PROMPT,
    )
    for prompt in prompts:
        assert _PROVIDER_RULE_FRAGMENT in prompt
        assert _NO_FALLBACK_FRAGMENT in prompt
