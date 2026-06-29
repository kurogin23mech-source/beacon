import { spawn, spawnSync } from 'node:child_process'

function killProcessTree(child) {
  if (!child.pid) return
  try {
    if (process.platform === 'win32') {
      const result = spawnSync(
        'taskkill', ['/pid', String(child.pid), '/t', '/f'],
        { stdio: 'ignore', windowsHide: true },
      )
      if (result.error) child.kill('SIGKILL')
    } else {
      process.kill(-child.pid, 'SIGKILL')
    }
  } catch (error) {
    if (error?.code !== 'ESRCH') throw error
  }
}

export function runCommandWithTimeout(command, args, options = {}) {
  const timeout = options.timeout ?? 15000
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: options.cwd,
      env: options.env,
      detached: process.platform !== 'win32',
      stdio: 'ignore',
      windowsHide: true,
    })
    let timedOut = false
    let settled = false

    const timer = setTimeout(() => {
      timedOut = true
      try {
        killProcessTree(child)
      } catch (error) {
        finish(error)
      }
    }, timeout)

    const finish = (error) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (error) reject(error)
      else resolve()
    }

    child.on('error', finish)
    child.on('close', (code, signal) => {
      if (timedOut) {
        const error = new Error(`${command} timed out after ${timeout}ms`)
        error.code = 'ETIMEDOUT'
        finish(error)
      } else if (code !== 0) {
        finish(new Error(`${command} exited with code ${code} (signal=${signal || 'none'})`))
      } else {
        finish()
      }
    })
  })
}
