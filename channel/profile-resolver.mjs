// Beacon profile resolution (Node side mirror of lib/profile.py).
//
// A profile is the unit of "which Beacon backend am I talking to right now"
// (modeled after AWS CLI profiles and gcloud configurations).
//
// This file MUST stay behavior-equivalent to lib/profile.py for resolution.
// Cross-language drift is pinned by tests/test_profile_cross_lang.py
// (= same profile + same cwd + same env => same credentials_path / api_url
// on both Python and Node) as the merge gate.
//
// Resolution order for profile name (mirrors Python resolve_profile_name):
//   1. cliArg (--profile <name>)   ← bus.mjs does not pass one
//   2. BEACON_PROFILE env var
//   3. cwd/.beacon/cloud.json `profile` field
//   4. "default" (reserved name)
//
// Resolution order for api_url (mirrors Python _resolve_api_url):
//   1. BEACON_API_URL env var
//   2. cwd/.beacon/cloud.json.api_url
//   3. ~/.beacon/profiles/<name>/profile.json.api_url
//   4. "https://beacon-ai.dev"
//
// Silent migration: NOT performed here. Python CLI owns that write
// (lib/profile.py _maybe_silent_migrate_default). Node side reads only.
// To survive the brief window before any CLI invocation has migrated,
// loadToken() falls back to ~/.beacon/credentials.json for the "default"
// profile only.

import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'

export const DEFAULT_PROFILE = 'default'
export const DEFAULT_API_URL = 'https://beacon-ai.dev'

// Profile names: lowercase alphanumeric + hyphens, 1-63 chars, must start
// with alphanumeric. Mirrors _PROFILE_NAME_RE in lib/profile.py.
const PROFILE_NAME_RE = /^[a-z0-9][a-z0-9-]{0,62}$/

export class ProfileError extends Error {}

export function validateProfileName(name) {
  if (typeof name !== 'string' || !PROFILE_NAME_RE.test(name)) {
    throw new ProfileError(
      `Invalid profile name ${JSON.stringify(name)}: must be 1-63 chars, ` +
      `lowercase alphanumeric with hyphens, starting with alphanumeric`
    )
  }
}

function beaconHome(env = process.env) {
  return env.BEACON_HOME || path.join(os.homedir(), '.beacon')
}

function safeReadJson(p) {
  try {
    const txt = fs.readFileSync(p, 'utf8')
    const v = JSON.parse(txt)
    return (v && typeof v === 'object' && !Array.isArray(v)) ? v : {}
  } catch {
    return {}
  }
}

function readCwdCloudJson(cwd) {
  return safeReadJson(path.join(cwd, '.beacon', 'cloud.json'))
}

function profileDir(name, env = process.env) {
  return path.join(beaconHome(env), 'profiles', name)
}

function legacyCredentialsPath(env = process.env) {
  return path.join(beaconHome(env), 'credentials.json')
}

export function resolveProfileName({
  cliArg = null,
  cwd = process.cwd(),
  env = process.env,
} = {}) {
  if (cliArg) {
    validateProfileName(cliArg)
    return cliArg
  }
  if (env.BEACON_PROFILE) {
    validateProfileName(env.BEACON_PROFILE)
    return env.BEACON_PROFILE
  }
  const cloud = readCwdCloudJson(cwd)
  if (cloud.profile) {
    validateProfileName(cloud.profile)
    return cloud.profile
  }
  return DEFAULT_PROFILE
}

function resolveApiUrl(profileConfig, cwd, env) {
  if (env.BEACON_API_URL) return String(env.BEACON_API_URL).replace(/\/$/, '')
  const cloud = readCwdCloudJson(cwd)
  if (cloud.api_url) return String(cloud.api_url).replace(/\/$/, '')
  if (profileConfig.api_url) return String(profileConfig.api_url).replace(/\/$/, '')
  return DEFAULT_API_URL
}

export function loadProfile(name, {
  cwd = process.cwd(),
  env = process.env,
} = {}) {
  validateProfileName(name)
  const pdir = profileDir(name, env)
  const profileConfig = safeReadJson(path.join(pdir, 'profile.json'))
  const apiUrl = resolveApiUrl(profileConfig, cwd, env)
  const credentialsPath = path.join(pdir, 'credentials.json')
  const firebaseConfigPath = path.join(pdir, 'firebase_config.json')
  return {
    name,
    apiUrl,
    credentialsPath,
    firebaseConfigPath: fs.existsSync(firebaseConfigPath) ? firebaseConfigPath : null,
    backendType: String(profileConfig.backend_type || 'gcp'),
  }
}

export function resolveActiveProfile({
  cliArg = null,
  cwd = process.cwd(),
  env = process.env,
} = {}) {
  const name = resolveProfileName({ cliArg, cwd, env })
  return loadProfile(name, { cwd, env })
}

// Read the auth token for a profile. For the "default" profile, fall back
// to the legacy ~/.beacon/credentials.json when the profile dir does not
// exist yet — this covers the window between bclaude startup and the
// first CLI invocation that triggers silent migration on the Python side.
//
// Named profiles do NOT get the legacy fallback (the legacy file is
// conceptually "the old default", not a wildcard).
export function loadToken(profile, { env = process.env } = {}) {
  if (env.BEACON_AUTH_TOKEN) return env.BEACON_AUTH_TOKEN
  const c = safeReadJson(profile.credentialsPath)
  if (c.token || c.id_token) return c.token || c.id_token
  if (profile.name === DEFAULT_PROFILE) {
    const legacy = safeReadJson(legacyCredentialsPath(env))
    return legacy.token || legacy.id_token || ''
  }
  return ''
}
