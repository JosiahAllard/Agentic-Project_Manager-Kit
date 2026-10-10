# Windows token leak: slow process launches

Optional, Windows only. A kit that launches thousands of git and Python processes per test suite
feels this leak first.

## Symptom

Every process launch gets slower the longer the machine stays up, and only a full Restart brings
it back. Measured on one Windows 11 machine over 5 hours: about 281,000 leaked tokens, and kernel
CPU per process launch up from 1.3 ms to 6.2 ms.

## Cause

While the foreground lock is armed, win32kfull's foreground-launch check
(`CForegroundLaunch::_CheckAllowForeground`) takes a reference to the parent process's token and
never releases it. The leaked tokens fill a kernel table that every process creation walks, so
each launch costs more than the last. An independent write-up of the same leak:
github.com/bentoner/windows-token-leak.

Windows arms the lock at each logon by setting the live `ForegroundLockTimeout` system parameter
to 0x7FFFFFFF. The live value is what counts, not the registry value: the registry was seen
reading 2147483647 while the live value was 0.

## Check

Prints the live value; 0 means the fix is in place.

```powershell
$t = Add-Type -Name FltCheck -Namespace W -PassThru -MemberDefinition '[DllImport("user32.dll", EntryPoint="SystemParametersInfoW")] public static extern bool Get(uint a, uint b, ref uint c, uint d);'; $v = [uint32]1; $null = $t::Get(0x2000, 0, [ref]$v, 0); $v
```

## Fix

After each logon, call `SystemParametersInfo(SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001, 0, (PVOID)0, 0)`
and confirm with `SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000`. With the fix live, 300 launches made
144,412 token allocations and left 15 outstanding: near zero. The cost: any
application may take focus at any time (the write-up above).

Tokens already leaked stay until a full Restart. Shut down with Fast Startup keeps them.

Not covered: a second, smaller leak from the console default-terminal handoff and MSYS2 exec.

## Keep it after every logon

A per-user scheduled task, no administrator rights: at logon, after 20 s, a hidden PowerShell sets
the value and retries every 5 s for up to 10 minutes until the check reads 0, logging each run
to `%LOCALAPPDATA%\foreground-lock-fix.log`. Not yet seen at a real logon: the first-logon check
is pending. Register it from PowerShell:

```powershell
$s = @'
$t = Add-Type -Name S -Namespace W -PassThru -MemberDefinition '[DllImport("user32.dll", EntryPoint="SystemParametersInfoW")] public static extern bool Set(uint a, uint b, IntPtr c, uint d); [DllImport("user32.dll", EntryPoint="SystemParametersInfoW")] public static extern bool Get(uint a, uint b, ref uint c, uint d);'
$log = "$env:LOCALAPPDATA\foreground-lock-fix.log"
for ($i = 1; $i -le 120; $i++) {
  $null = $t::Set(0x2001, 0, [IntPtr]::Zero, 0)
  $v = [uint32]1; $null = $t::Get(0x2000, 0, [ref]$v, 0)
  if ($v -eq 0) { "$(Get-Date -Format s) set to 0 on try $i" | Add-Content $log; exit 0 }
  Start-Sleep -Seconds 5
}
"$(Get-Date -Format s) FAILED after 120 tries, value $v" | Add-Content $log; exit 1
'@
$enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($s))
$action = New-ScheduledTaskAction -Execute powershell.exe -Argument "-NoProfile -WindowStyle Hidden -EncodedCommand $enc"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = 'PT20S'
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
Register-ScheduledTask -TaskName 'Foreground lock timeout fix' -Action $action -Trigger $trigger -Settings $settings -Description 'Sets ForegroundLockTimeout to 0 after logon; works around the Windows token leak that slows process launches.'
```

After the next logon, the log's last line should read `set to 0 on try N`, and the check above
should print 0.

## Remove

```powershell
Unregister-ScheduledTask -TaskName 'Foreground lock timeout fix' -Confirm:$false
```

At the next logon Windows arms the lock again, and the leak resumes.
