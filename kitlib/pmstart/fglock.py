"""pm_start check: the live ForegroundLockTimeout (docs/windows-token-leak.md). Read only."""
import ctypes

SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000
FIX = ('& ([scriptblock]::Create((Get-Content -Raw -LiteralPath '
       '"$env:LOCALAPPDATA\\foreground-lock-fix\\fix.ps1"))) -Trigger manual')


def read_live(user32=None):
    """The live value (GET only; the GET has no foreground restriction). Raises OSError on failure."""
    if user32 is None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    v = ctypes.c_uint32(1)
    if not user32.SystemParametersInfoW(SPI_GETFOREGROUNDLOCKTIMEOUT, 0, ctypes.byref(v), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return v.value


def check(cfg, read=None):
    """One warning line when the live value is not 0; [] when it is 0. Always on: no config key.
    `cfg` is unused, kept for the checks pattern."""
    value = (read or read_live)()
    if value == 0:
        return []
    return ["foreground lock timeout is %d, not 0: process launches slow down with uptime "
            "(docs/windows-token-leak.md, Keep it after every logon). Ask the owner (via the hub) to run, "
            "in their own PowerShell window: %s ; logon log: %%LOCALAPPDATA%%\\foreground-lock-fix\\fix.log"
            % (value, FIX)]
