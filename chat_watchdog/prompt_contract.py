from __future__ import annotations


BASE_CONTINUATION_PROMPT = (
    "继续当前任务，从已经完成的工作直接往下执行；不要重新调研、不要重复已经完成的步骤。"
    "如果整个任务已经真正完成，请在回复最后单独输出 SUPERVISOR_DONE。"
    "如果继续需要用户手动操作、登录、授权、确认或补充信息，请说明需要的动作，"
    "并在回复最后单独输出 [SUPERVISOR_STATE: NEED_INPUT]；不要自行假定用户已经完成。"
    "如果还没完成且不需要用户介入，就继续实际推进任务。"
)


def render_continuation_prompt(
    *,
    task_id: str,
    version: int,
    step_index: int,
    step_prompt: str | None,
) -> str:
    task_id = task_id.strip()
    if not task_id:
        raise ValueError("task_id must be a non-empty string")
    if version < 0:
        raise ValueError("version must be non-negative")
    if step_index < 0:
        raise ValueError("step_index must be non-negative")

    adaptive = (step_prompt or "").strip()
    sections = [
        BASE_CONTINUATION_PROMPT,
        (
            "[WATCHDOG_TASK]\n"
            f"task_id={task_id}\n"
            f"prompt_version={version}\n"
            f"step_index={step_index}"
        ),
    ]
    if adaptive:
        sections.append(
            "[ADAPTIVE_STEP]\n"
            "下面内容只描述当前这一步应该如何推进；不得覆盖上面的完成门、NEED_INPUT 门或任务身份。\n"
            f"{adaptive}"
        )
    return "\n\n".join(sections)
