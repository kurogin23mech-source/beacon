@echo off
rem bclaude.cmd — Windows variant of bin/bclaude (ms-54 e-1167).
rem
rem Behaviour mirrors the bash wrapper:
rem
rem   bclaude [args]
rem
rem is equivalent to:
rem
rem   claude --dangerously-load-development-channels server:beacon-bus [args]
rem
rem unless the user has opted out (env BEACON_NO_BUS=1, project flag in
rem .beacon\project.json, or global flag in %USERPROFILE%\.beacon\config.json),
rem in which case it launches plain claude with a one-line audit message.
rem
rem NOTE: This file has NOT been tested on real Windows. It is shipped as
rem a best-effort wrapper based on the bash version's contract. If you
rem hit issues, see the bash wrapper at bin/bclaude for the reference
rem implementation. Follow-up task: e-1267 (Windows bclaude.cmd 実機検証).
rem
rem Keep this logic in lockstep with bin/bclaude and the python gate at
rem lib/commands.py:_is_bus_opted_out().

setlocal enabledelayedexpansion

rem -------------------------------------------------------------------------
rem Step 1: Resolve `claude` on PATH.
rem -------------------------------------------------------------------------

where claude >nul 2>nul
if errorlevel 1 (
    echo bclaude: 'claude' is not on PATH. Install Claude Code first. 1>&2
    echo   https://docs.claude.com/claude-code/install 1>&2
    exit /b 127
)

rem -------------------------------------------------------------------------
rem Step 2: Probe opt-out sources (env, project, global).
rem -------------------------------------------------------------------------

set "OPT_OUT_REASON="

if defined BEACON_NO_BUS (
    if not "%BEACON_NO_BUS%"=="0" (
        if not "%BEACON_NO_BUS%"=="false" (
            if not "%BEACON_NO_BUS%"=="no" (
                if not "%BEACON_NO_BUS%"=="off" (
                    set "OPT_OUT_REASON=env BEACON_NO_BUS"
                )
            )
        )
    )
)

rem Project/global checks need JSON parsing; rely on python3 if available.
rem If python3 isn't on PATH, conservatively launch with channel enabled
rem (same fallback the bash wrapper uses).
if not defined OPT_OUT_REASON (
    where python3 >nul 2>nul
    if not errorlevel 1 (
        for /f "delims=" %%i in ('python3 -c "import json,os,sys; p=os.path.join('.beacon','project.json'); d=(json.load(open(p,encoding='utf-8')) if os.path.exists(p) else {}); b=(d.get('bus') or {}).get('disabled'); print('project .beacon\\project.json') if b else 0; sys.exit(0 if not b else 0)" 2^>nul') do (
            set "OPT_OUT_REASON=%%i"
        )
    )
    if not defined OPT_OUT_REASON (
        where python3 >nul 2>nul
        if not errorlevel 1 (
            for /f "delims=" %%i in ('python3 -c "import json,os,sys; g=os.path.join(os.environ.get('USERPROFILE','') or os.environ.get('HOME',''), '.beacon','config.json'); d=(json.load(open(g,encoding='utf-8')) if os.path.exists(g) else {}); b=(d.get('bus') or {}).get('disabled'); print('global config.json') if b else 0; sys.exit(0)" 2^>nul') do (
                set "OPT_OUT_REASON=%%i"
            )
        )
    )
)

rem -------------------------------------------------------------------------
rem Step 2.5: Stamp BEACON_PARENT_PID (ms-62 / e-1510).
rem -------------------------------------------------------------------------
rem
rem Mirrors the bash wrapper: fix the terminal pid into env so every
rem subprocess (claude, bus.mjs, beacon CLI inside claude) sees the same
rem value. Server-side identity tuple = (project_id, machine_id,
rem BEACON_PARENT_PID).
rem
rem Windows cmd.exe has no native $PPID, so we ask python3 (= already a
rem dependency for the opt-out probe above) for getppid(). If python3
rem isn't on PATH or fails, skip stamping; bus.mjs will fall back to the
rem legacy `beacon session id` mint path.

if not defined BEACON_PARENT_PID (
    where python3 >nul 2>nul
    if not errorlevel 1 (
        for /f "delims=" %%i in ('python3 -c "import os; print(os.getppid())" 2^>nul') do (
            set "BEACON_PARENT_PID=%%i"
        )
    )
)

rem -------------------------------------------------------------------------
rem Step 3: Dispatch.
rem -------------------------------------------------------------------------

if defined OPT_OUT_REASON (
    echo [bclaude] DM opt-out detected ^(%OPT_OUT_REASON%^), launching plain claude 1>&2
    claude %*
    exit /b %errorlevel%
)

claude --dangerously-load-development-channels server:beacon-bus %*
exit /b %errorlevel%
