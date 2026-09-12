import { spawn } from 'node:child_process'

const DEFAULT_OUTPUT_LIMIT = 8 * 1024 * 1024

function boundedString(value, name) {
  if (typeof value !== 'string' || !value) throw new TypeError(`${name} must be non-empty text`)
  return value
}

export function runJsonCli({
  executable,
  argsPrefix = [],
  args = [],
  cwd,
  env = {},
  maxOutputBytes = DEFAULT_OUTPUT_LIMIT
}) {
  boundedString(executable, 'executable')
  if (!Array.isArray(argsPrefix) || !Array.isArray(args)) throw new TypeError('argsPrefix and args must be arrays')
  if (!Number.isSafeInteger(maxOutputBytes) || maxOutputBytes < 1024) {
    throw new RangeError('maxOutputBytes must be an integer >= 1024')
  }

  return new Promise((resolve, reject) => {
    let settled = false
    let stdout = ''
    let stderr = ''
    const child = spawn(executable, [...argsPrefix, ...args], {
      cwd,
      env: { ...process.env, ...env },
      shell: false,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe']
    })

    const fail = (error) => {
      if (settled) return
      settled = true
      reject(error)
    }
    const append = (current, chunk, streamName) => {
      const next = current + chunk.toString('utf8')
      if (Buffer.byteLength(next, 'utf8') > maxOutputBytes) {
        child.kill()
        fail(new Error(`${streamName} exceeded ${maxOutputBytes} bytes`))
      }
      return next
    }

    child.stdout.on('data', (chunk) => {
      if (!settled) stdout = append(stdout, chunk, 'stdout')
    })
    child.stderr.on('data', (chunk) => {
      if (!settled) stderr = append(stderr, chunk, 'stderr')
    })
    child.on('error', (error) => fail(error))
    child.on('close', (code, signal) => {
      if (settled) return
      if (code !== 0) {
        fail(new Error(`CLI exited with code ${code ?? 'null'}${signal ? ` (${signal})` : ''}: ${stderr.trim()}`))
        return
      }
      const raw = stdout.trim()
      let value
      try {
        value = JSON.parse(raw)
      } catch (error) {
        fail(new Error(`CLI stdout was not valid JSON: ${error.message}`))
        return
      }
      settled = true
      resolve(value)
    })
  })
}
