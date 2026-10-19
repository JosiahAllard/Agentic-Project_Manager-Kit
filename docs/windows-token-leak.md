# Windows token leak: slow process launches

Optional, Windows only. A kit that launches thousands of git and Python processes per test suite
feels this leak first. The PM session-start checklist warns while the live value is not 0
(docs/pm-start.md, `kitlib/pmstart/fglock.py`).

## Symptom

Every process launch gets slower the longer the machine stays up, and only a full Restart brings
it back. Measured on one Windows 11 machine over 5 hours: about 281,000 leaked tokens, and kernel
CPU per process launch up from 1.3 ms to 6.2 ms.

## Cause

While the foreground lock is armed, win32kfull's foreground-launch check
(`CForegroundLaunch::_CheckAllowForeground`) takes a reference to the parent process's token and
never releases it. The leaked tokens fill a kernel table that every process creation walks, so
each launch costs more than the last.

After each logon the live `ForegroundLockTimeout` system parameter reads 0x7FFFFFFF. The live value is what counts, not the HKCU registry value.

## Check

Prints the live value; 0 means the fix is in place.

```powershell
$t = Add-Type -Name FltCheck -Namespace W -PassThru -MemberDefinition '[DllImport("user32.dll", EntryPoint="SystemParametersInfoW")] public static extern bool Get(uint a, uint b, ref uint c, uint d);'; $v = [uint32]1; $null = $t::Get(0x2000, 0, [ref]$v, 0); $v
```

## Fix

After each logon, call `SystemParametersInfo(SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001, 0, (PVOID)0, 0)`
and confirm with `SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000`. The cost: any application may take focus
at any time.

The set is refused unless the calling thread may change the foreground window (SystemParametersInfo documentation), so a set from a background process, such as a scheduled task, is likely refused. The set works from the owner's own foreground PowerShell window.

Tokens already leaked stay until a full Restart. Shut down with Fast Startup keeps them.

Not covered: a second, smaller leak from the console default-terminal handoff and MSYS2 exec.

## Keep it after every logon

### Why the first task failed

A hidden PowerShell started from a scheduled task died before its host initialised (LastTaskResult 0xC000013A, no log line): its console was handed to Windows Terminal while the logon was still initialising, and that window closed (inference). `-WindowStyle Hidden` acts only after PowerShell has started, which is too late.

### Design

Both carriers run the script under `conhost.exe --headless`: no window, no default-terminal
handoff, nothing for anyone to close. `--headless` is not documented by Microsoft; it was observed
working on Windows 11 build 26300.

- One script file, `%LOCALAPPDATA%\foreground-lock-fix\fix.ps1`, so it can be read and run by hand.
  It writes only the live value (fWinIni 0) and never the registry. It uses `return`, never
  `exit`, so it is safe to run in your own window.
- Carrier (a): a per-user scheduled task with a logon trigger (20 s delay) and a session-unlock
  trigger (StateChange 8), which re-arms the fix if something sets the value again. No
  administrator rights.
- Carrier (b): an HKCU Run entry. Its parent is `explorer.exe`, which at logon may be the
  foreground process (inference). Whichever carrier wins, the other logs `already 0`.
- If a set is refused, the script logs the Win32 error and the foreground process, then tries once
  more after a synthetic zero-distance mouse move, which makes the caller the last-input process
  (inference; `-NoNudge` turns it off).
- The log is the result: `conhost --headless` returns 0 whatever its child returns, so
  LastTaskResult reads 0 on every run.

Run these in your own non-elevated Windows PowerShell 5.1 window.

### Step 1: write the script

```powershell
$dir = Join-Path $env:LOCALAPPDATA 'foreground-lock-fix'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$ps1 = Join-Path $dir 'fix.ps1'
$script = @'
param([string]$Trigger = 'manual', [int]$DeadlineMinutes = 30, [switch]$Probe, [switch]$NoNudge)
# Sets the live ForegroundLockTimeout to 0 (docs/windows-token-leak.md). Every run logs; the log is the result.
$log = Join-Path $env:LOCALAPPDATA 'foreground-lock-fix\fix.log'
function Write-Log([string]$m) {
  $line = "$(Get-Date -Format s) pid=$PID trigger=$Trigger $m"
  try { Add-Content -LiteralPath $log -Value $line -Encoding UTF8 -ErrorAction Stop } catch { }
  Write-Host $line
}
$reg = try { (Get-ItemProperty -LiteralPath 'HKCU:\Control Panel\Desktop' -Name ForegroundLockTimeout -ErrorAction Stop).ForegroundLockTimeout } catch { 'unreadable' }
Write-Log "start registry=$reg"
try {
  if (-not ('W.FglFix' -as [type])) {
    Add-Type -Name FglFix -Namespace W -ErrorAction Stop -MemberDefinition (
      '[DllImport("user32.dll", EntryPoint="SystemParametersInfoW", SetLastError=true)] public static extern bool Set(uint a, uint b, IntPtr c, uint d);' +
      '[DllImport("user32.dll", EntryPoint="SystemParametersInfoW", SetLastError=true)] public static extern bool Get(uint a, uint b, ref uint c, uint d);' +
      '[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();' +
      '[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);' +
      '[DllImport("user32.dll")] public static extern void mouse_event(uint f, int x, int y, uint d, UIntPtr e);')
  }
  $t = 'W.FglFix' -as [type]
} catch { Write-Log "FAILED Add-Type: $($_.Exception.Message)"; return }
function Get-Live { $v = [uint32]1; if ($t::Get(0x2000, 0, [ref]$v, 0)) { $v } else { "get-error-$([Runtime.InteropServices.Marshal]::GetLastWin32Error())" } }
function Get-Fg { $p = [uint32]0; $null = $t::GetWindowThreadProcessId($t::GetForegroundWindow(), [ref]$p); "$((Get-Process -Id $p -ErrorAction SilentlyContinue).ProcessName)($p)" }
function Set-Zero { $ok = $t::Set(0x2001, 0, [IntPtr]::Zero, 0); $e = [Runtime.InteropServices.Marshal]::GetLastWin32Error(); @($ok, $e) }
Write-Log "live=$(Get-Live) foreground=$(Get-Fg)"
if ($Probe) { $DeadlineMinutes = 0 }
$deadline = (Get-Date).AddMinutes($DeadlineMinutes)
$try = 0; $refused = 0
while ($true) {
  $try++
  $v = Get-Live
  if ($v -is [uint32] -and $v -eq 0 -and -not $Probe) { Write-Log "already 0 on try $try"; return }
  $r = Set-Zero; $how = 'plain'
  if (-not $r[0] -and -not $NoNudge) { $t::mouse_event(0x0001, 0, 0, 0, [UIntPtr]::Zero); $r = Set-Zero; $how = 'after input nudge' }
  $after = Get-Live
  if ($r[0] -and $after -is [uint32] -and $after -eq 0) { Write-Log "set to 0 on try $try ($how), was $v"; return }
  $refused++
  if ($refused -eq 1) { Write-Log "refused on try ${try}: Set returned $($r[0]), error $($r[1]), live=$after, foreground=$(Get-Fg)" }
  if ((Get-Date) -ge $deadline) { Write-Log "FAILED after $try tries, live=$after, last error $($r[1])"; return }
  Start-Sleep -Seconds 5
}
'@
Set-Content -LiteralPath $ps1 -Value $script -Encoding UTF8
```

### Step 2: set it now, from your own window

This is the path known to work. It needs no execution-policy change, because the script is read
as a string.

```powershell
& ([scriptblock]::Create((Get-Content -Raw -LiteralPath "$env:LOCALAPPDATA\foreground-lock-fix\fix.ps1"))) -Trigger manual
```

Expect a `start` line, a `live=... foreground=...` line, then `set to 0 on try 1 (plain), was ...`
(or `already 0`). This is the command the session-start warning prints. Add `-Probe -NoNudge` to
re-set the value to 0 even when it already is 0, which tests the method without changing anything.

### Step 3 (optional experiment): persist 0 in the registry

Print the old value first, so Remove can restore it.

```powershell
(Get-ItemProperty -LiteralPath 'HKCU:\Control Panel\Desktop' -Name ForegroundLockTimeout).ForegroundLockTimeout
Set-ItemProperty -LiteralPath 'HKCU:\Control Panel\Desktop' -Name ForegroundLockTimeout -Value 0 -Type DWord
```

At the next logon, the `start registry=0` line and its `live=` value show whether the logon value
comes from the registry: `live=0` means it does. The script itself never writes the registry.

### Step 4: replace the scheduled task

```powershell
Unregister-ScheduledTask -TaskName 'Foreground lock timeout fix' -Confirm:$false -ErrorAction SilentlyContinue
$ps1 = Join-Path $env:LOCALAPPDATA 'foreground-lock-fix\fix.ps1'
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\conhost.exe" -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$ps1`" -Trigger task"
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logon.Delay = 'PT20S'
$unlock = Get-CimClass -Namespace ROOT\Microsoft\Windows\TaskScheduler -ClassName MSFT_TaskSessionStateChangeTrigger | New-CimInstance -ClientOnly -Property @{StateChange = [uint32]8; UserId = $env:USERNAME; Enabled = $true}
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 45)
Register-ScheduledTask -TaskName 'Foreground lock timeout fix' -Action $action -Trigger @($logon, $unlock) -Settings $settings -Description 'Sets ForegroundLockTimeout to 0 at logon and unlock; works around the Windows token leak that slows process launches. Result: %LOCALAPPDATA%\foreground-lock-fix\fix.log'
(Get-ScheduledTask -TaskName 'Foreground lock timeout fix').Triggers | Format-List CimClass, Enabled, Delay, StateChange, UserId
```

The last line should show one MSFT_TaskLogonTrigger with Delay PT20S and one
MSFT_TaskSessionStateChangeTrigger with StateChange 8 (the unlock trigger command is untested, and
this line is its check).

### Step 5: add the Run-key carrier

```powershell
$ps1 = Join-Path $env:LOCALAPPDATA 'foreground-lock-fix\fix.ps1'
Set-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'ForegroundLockFix' -Value "$env:SystemRoot\System32\conhost.exe --headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$ps1`" -Trigger run"
```

### Step 6: probe the task path

A set of 0 over 0 changes nothing, so this is safe while the value is already 0. It decides
whether a task may set the value at all.

```powershell
$ps1 = Join-Path $env:LOCALAPPDATA 'foreground-lock-fix\fix.ps1'
$log = Join-Path $env:LOCALAPPDATA 'foreground-lock-fix\fix.log'
$a = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\conhost.exe" -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$ps1`" -Trigger probe-plain -Probe -NoNudge"
Register-ScheduledTask -TaskName 'Foreground lock fix probe' -Action $a -Force | Out-Null
Start-ScheduledTask -TaskName 'Foreground lock fix probe'
Start-Sleep -Seconds 15
$a = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\conhost.exe" -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$ps1`" -Trigger probe-nudge -Probe"
Register-ScheduledTask -TaskName 'Foreground lock fix probe' -Action $a -Force | Out-Null
Start-ScheduledTask -TaskName 'Foreground lock fix probe'
Start-Sleep -Seconds 15
Unregister-ScheduledTask -TaskName 'Foreground lock fix probe' -Confirm:$false
Get-Content -LiteralPath $log -Tail 12
```

- No `start` line for `probe-plain`: the headless carrier does not run. Stop.
- `probe-plain` `set to 0 on try 1 (plain)`: a task may set the value; the old failure was the
  window handoff alone.
- `probe-plain` `refused ... error N`: the foreground restriction applies to a task. Then
  `probe-nudge` `(after input nudge)` means the task carrier is viable; `FAILED` means the task
  cannot set the value, and the Run key and the step-3 registry value are the remaining carriers.

### Step 7: after the next real logon

```powershell
Get-Content -LiteralPath (Join-Path $env:LOCALAPPDATA 'foreground-lock-fix\fix.log') -Tail 20
```

Expect `start` lines for `trigger=task` and `trigger=run`, each followed by `set to 0` or
`already 0`. Then run the Check above; it should print 0. Not yet seen at a real logon.

### The log, line by line

Each line reads `<time> pid=<n> trigger=<manual|task|run|probe-...> <text>` in
`%LOCALAPPDATA%\foreground-lock-fix\fix.log`.

- `start registry=<v>`: the script began; `<v>` is the HKCU registry value (`unreadable` if missing).
- `live=<v> foreground=<process>(<pid>)`: the live value, and the process owning the foreground window.
- `already 0 on try N`: nothing to do (the other carrier, or the registry, got there first).
- `set to 0 on try N (plain)` or `(after input nudge), was <v>`: the set worked, with or without the mouse nudge.
- `refused on try N: Set returned False, error E, live=..., foreground=...`: the set was refused
  (error 87 is the foreground restriction); it retries every 5 s until the 30 minute deadline.
- `FAILED after N tries, live=..., last error E`: gave up. `FAILED Add-Type: ...`: the API
  declaration did not compile.

## Remove

```powershell
Unregister-ScheduledTask -TaskName 'Foreground lock timeout fix' -Confirm:$false
Remove-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'ForegroundLockFix'
Remove-Item -LiteralPath (Join-Path $env:LOCALAPPDATA 'foreground-lock-fix') -Recurse
```

If step 3 was run, also restore the value it printed:
`Set-ItemProperty -LiteralPath 'HKCU:\Control Panel\Desktop' -Name ForegroundLockTimeout -Value <the value step 3 printed> -Type DWord`.

At the next logon Windows arms the lock again, and the leak resumes.
