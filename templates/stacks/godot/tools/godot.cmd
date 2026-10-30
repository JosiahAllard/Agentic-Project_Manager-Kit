@echo off
rem The one place the Godot path is resolved. Every script and doc goes through here.
rem
rem   tools\godot.cmd <args>        run the console (headless-capable) exe with <args>
rem   call tools\godot.cmd --env    set GODOT_CONSOLE and GODOT_GUI for the calling batch file
rem
rem Set GODOT_DIR (user environment variable) to the Godot .NET folder holding exactly one
rem Godot_*_console.exe, or set GODOT_CONSOLE to that exe itself. Nothing is hard-coded here.
rem The path of this shim has no spaces, so it can be invoked unquoted from any shell.
rem
rem Notes for this stack:
rem  - This shim deliberately does NOT take the machine lock: the owner's editor and play runs
rem    go through it, and so do the several engines of one multi-process test, which belong
rem    under one lock, not several. The gate (tools/gate.py) books the lock for its own runs; any
rem    other test run takes it through the kit's lock tool (in the kit's folder, not in this repo):
rem      python "<kit folder>/tools/machine_lock.py" run --repo <repo> --slug <you> --kind test -- tools\godot.cmd <args>
rem  - Check for an engine left behind by a killed test run before the next one: a leftover engine
rem    keeps its ports and files ("Couldn't create an ENet host"); an open editor, whose command
rem    line names no test scene, is not one.
rem  - Kill a leftover engine's whole process tree, never just this .cmd's handle: the console
rem    exe starts the real engine as a child, and that child keeps its ports and files.
rem  - A headless import (--headless --editor --quit) exits 0 even when a resource fails to
rem    load, so gate on tests that load resources, not on the import's output.
rem  - An import writes .uid sidecars for scripts that lack one: commit those that belong to
rem    your scripts and delete the rest before merging main in, or git refuses the merge.
if defined GODOT_CONSOLE goto have_console
if not defined GODOT_DIR goto no_dir
set "_godot_count=0"
for %%F in ("%GODOT_DIR%\Godot_*_console.exe") do (
    set "GODOT_CONSOLE=%%~fF"
    set /a _godot_count+=1 >nul
)
if "%_godot_count%"=="0" goto not_found
if not "%_godot_count%"=="1" goto ambiguous

:have_console
set "GODOT_GUI=%GODOT_CONSOLE:_console.exe=.exe%"
if "%~1"=="--env" exit /b 0
if not exist "%GODOT_CONSOLE%" goto not_found
"%GODOT_CONSOLE%" %*
exit /b %ERRORLEVEL%

:no_dir
echo tools\godot.cmd: GODOT_DIR is not set. Point it at the Godot .NET folder, or set GODOT_CONSOLE to the console exe. 1>&2
exit /b 1

:not_found
echo tools\godot.cmd: no Godot console exe found. GODOT_DIR="%GODOT_DIR%" GODOT_CONSOLE="%GODOT_CONSOLE%" 1>&2
set "GODOT_CONSOLE="
exit /b 1

:ambiguous
echo tools\godot.cmd: %_godot_count% Godot console exes in "%GODOT_DIR%"; set GODOT_CONSOLE to the one to use. 1>&2
set "GODOT_CONSOLE="
exit /b 1
