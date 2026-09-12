function requireText(value, name, maxBytes = 65536) {
  if (typeof value !== 'string' || !value.trim()) {
    throw new TypeError(`${name} must be non-empty text`)
  }
  if (Buffer.byteLength(value, 'utf8') > maxBytes) {
    throw new RangeError(`${name} exceeds ${maxBytes} UTF-8 bytes`)
  }
  return value
}

function requireRunId(response) {
  const runId = response?.result?.runId
  if (response?.ok !== true || typeof runId !== 'string' || !runId) {
    throw new Error('Orca mission did not return a successful runId')
  }
  return runId
}

function workerReportSource(task, runId) {
  if (!task || typeof task.id !== 'string' || typeof task.result !== 'string' || !task.result) {
    return null
  }
  let report
  try {
    report = JSON.parse(task.result)
  } catch {
    return null
  }
  if (
    !report ||
    report.provenance !== 'worker_report' ||
    typeof report.messageId !== 'string' ||
    !report.messageId ||
    typeof report.body !== 'string' ||
    !report.body.trim()
  ) {
    return null
  }
  requireText(report.body, `worker report ${report.messageId}`)
  const filesModified = Array.isArray(report.filesModified)
    ? report.filesModified.filter((value) => typeof value === 'string').slice(0, 128)
    : []
  return {
    id: `orca-worker-report:${report.messageId}`,
    role: 'worker',
    text: report.body,
    origin: {
      kind: 'orca-worker-report',
      runId,
      taskId: task.id,
      messageId: report.messageId,
      outcome: typeof report.outcome === 'string' ? report.outcome : null,
      reportedBy: typeof report.reportedBy === 'string' ? report.reportedBy : null,
      subject: typeof report.subject === 'string' ? report.subject : null,
      completedBy: typeof report.completedBy === 'string' ? report.completedBy : null,
      filesModified,
      reportPath: typeof report.reportPath === 'string' ? report.reportPath : null,
      completedAt: typeof report.completedAt === 'string' ? report.completedAt : null
    }
  }
}

function missionArgs({ missionText, agent, worktree, from }) {
  const args = ['mission', 'start', '--text', missionText]
  if (agent) args.push('--agent', requireText(agent, 'agent', 256))
  if (worktree) args.push('--worktree', requireText(worktree, 'worktree', 4096))
  if (from) args.push('--from', requireText(from, 'from', 4096))
  args.push('--json')
  return args
}

export async function runOrcaMissionWithReanchor({
  engine,
  scope,
  owner,
  mission,
  runCli,
  maxBytes = 32768,
  agent,
  worktree,
  from
}) {
  if (!engine || typeof engine.handoff !== 'function' || typeof engine.recordObservation !== 'function') {
    throw new TypeError('engine must provide handoff() and recordObservation()')
  }
  if (typeof runCli !== 'function') {
    throw new TypeError('runCli must be a function')
  }
  const missionText = requireText(mission, 'mission')
  const handoff = await engine.handoff({ scope, owner, maxBytes })
  const combinedMission = `${missionText}\n\n${handoff.prompt}`

  const missionResponse = await runCli(missionArgs({
    missionText: combinedMission,
    agent,
    worktree,
    from
  }))
  const runId = requireRunId(missionResponse)
  const taskResponse = await runCli(['orchestration', 'task-list', '--run', runId, '--json'])
  if (taskResponse?.ok !== true || !Array.isArray(taskResponse?.result?.tasks)) {
    throw new Error(`Orca task-list failed for ${runId}`)
  }

  const sources = taskResponse.result.tasks
    .map((task) => workerReportSource(task, runId))
    .filter(Boolean)

  for (const source of sources) {
    await engine.recordObservation({ scope, owner, source })
  }

  return {
    runId,
    parentTip: handoff.parentTip,
    missionResult: missionResponse.result,
    workerObservationIds: sources.map((source) => source.id)
  }
}
