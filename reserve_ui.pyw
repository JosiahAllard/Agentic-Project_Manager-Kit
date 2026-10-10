"""reserve_ui.pyw: the machine schedule window, on kitlib.lock.api.

A thin entry point (ARCHITECTURE.md, Layers): the window is kitlib/lockui/window.py, its logic
kitlib/lockui/model.py and actions.py. Double-click it (pythonw: no console). It reads and writes
the machine's lock store through kitlib.lock.api, which decides who acts from this process's tree
(kitlib.lock.api.who): owner writes go through only when the owner runs it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kitlib.lockui import window                                      # noqa: E402

if __name__ == "__main__":
    window.main()
