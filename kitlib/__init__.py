"""kitlib: all kit logic, no command lines.

Importing it installs kitlib.proc.console's CreateProcess hook (Windows): from then on no start
in this process opens a console window, a bare subprocess or multiprocessing start included. The
import stays cheap: no ctypes until the first start that needs the hook."""
import os as _os

if _os.name == 'nt':
    from .proc import console as _console
    _console.install()
