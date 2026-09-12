#!/usr/bin/env node
import { readFile, stat } from 'node:fs/promises'
import { Reanchor } from '../src/engine.mjs'
import { runJsonCli } from '../src/json-cli-runner.mjs'
import { runOrcaMissionWithReanchor } from '../src/orca-mission-adapter.mjs'

const HELP = `reanchor-orca-mission --store <memory-directory> [--input <request.json>]

Request JSON:
{
  "scope": "parent-scope",
  "owner": "coordinator",
  "mission": "bounded worker mission",
  "orca": {
    "executable": "absolute executable path",
    "argsPrefix": ["optional", "argv", "prefix"],
    "cwd": "optional working directory",
    "env": { "OPTIONAL_ENV": "value" }
  },
  "agent": "optional-agent",
  "worktree": "optional-selector",
  "from": "optional-coordinator-handle",
  "maxBytes": 32768
}

The adapter uses shell=false. Parent handoff is read-only; worker reports return as observations.
`

function usage(message) {
  const error = new Error(message)
  error.code = 'USAGE'
  throw error
}

async function readInput(file) {
  const limit = 2 * 1024 * 1024
  if (file) {
    if ((await stat(file)).size > limit) usage('Input file exceeds 2 MiB')
    return readFile(file, 'utf8')
  }
  const chunks = []
  let size = 0
  for await (const chunk of process.stdin) {
    size += chunk.length
    if (size > limit) usage('stdin exceeds 2 MiB')
    chunks.push(chunk)
  }
  return Buffer.concat(chunks).toString('utf8')
}

function stringArray(value, name) {
  if (value === undefined) return []
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) {
    usage(`${name} must be an array of strings`)
  }
  return value
}

function stringMap(value, name) {
  if (value === undefined) return {}
  if (!value || typeof value !== 'object' || Array.isArray(value)) usage(`${name} must be an object`)
  for (const [key, item] of Object.entries(value)) {
    if (typeof item !== 'string') usage(`${name}.${key} must be a string`)
  }
  return value
}

async function main() {
  const args = process.argv.slice(2)
  if (args.length === 1 && ['--help', '-h'].includes(args[0])) {
    process.stdout.write(HELP)
    return
  }
  const options = {}
  while (args.length) {
    const option = args.shift()
    if (!['--store', '--input'].includes(option) || Object.hasOwn(options, option)) {
      usage(`Invalid/duplicate option: ${option}`)
    }
    const value = args.shift()
    if (!value || value.startsWith('--')) usage(`Value required for ${option}`)
    options[option] = value
  }
  if (!options['--store']) usage('--store is required')

  let request
  try {
    request = JSON.parse(await readInput(options['--input']))
  } catch (error) {
    if (error.code) throw error
    const invalid = new Error(`Invalid request JSON: ${error.message}`)
    invalid.code = 'INVALID_JSON'
    throw invalid
  }
  if (!request || typeof request !== 'object' || Array.isArray(request)) usage('Request must be one JSON object')
  const orca = request.orca
  if (!orca || typeof orca !== 'object' || Array.isArray(orca) || typeof orca.executable !== 'string' || !orca.executable) {
    usage('orca.executable is required')
  }
  const argsPrefix = stringArray(orca.argsPrefix, 'orca.argsPrefix')
  const env = stringMap(orca.env, 'orca.env')
  if (orca.cwd !== undefined && (typeof orca.cwd !== 'string' || !orca.cwd)) usage('orca.cwd must be non-empty text')
  if (request.maxBytes !== undefined && (!Number.isSafeInteger(request.maxBytes) || request.maxBytes < 1024 || request.maxBytes > 524288)) {
    usage('maxBytes must be 1024..524288')
  }

  const engine = new Reanchor(options['--store'])
  const runCli = (argv) => runJsonCli({
    executable: orca.executable,
    argsPrefix,
    args: argv,
    cwd: orca.cwd,
    env,
    maxOutputBytes: 8 * 1024 * 1024
  })
  const result = await runOrcaMissionWithReanchor({
    engine,
    scope: request.scope,
    owner: request.owner,
    mission: request.mission,
    runCli,
    maxBytes: request.maxBytes ?? 32768,
    agent: request.agent,
    worktree: request.worktree,
    from: request.from
  })
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`)
}

main().catch((error) => {
  process.stderr.write(`${JSON.stringify({ error: { code: error.code ?? 'INTERNAL_ERROR', message: error.message } })}\n`)
  process.exitCode = 1
})
