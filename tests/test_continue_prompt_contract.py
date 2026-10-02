from chat_watchdog.supervisor import CONTINUE_PROMPT


def test_continue_prompt_requires_progress_audit_and_drift_correction() -> None:
    assert "快速审计真实进度与目标漂移" in CONTINUE_PROMPT
    assert "重新规划" in CONTINUE_PROMPT
    assert "最小可验证步骤" in CONTINUE_PROMPT
    assert "不要重新调研" in CONTINUE_PROMPT
    assert "SUPERVISOR_DONE" in CONTINUE_PROMPT
    assert "[SUPERVISOR_STATE: NEED_INPUT]" in CONTINUE_PROMPT
