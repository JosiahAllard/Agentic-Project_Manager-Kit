"""The machine schedule window's logic: reserve_ui.pyw (kit root) is its tkinter view.

model: one kitlib.lock.api snapshot (and api.history) -> what the window draws and says.
actions: each window action as one kitlib.lock.api call, and the parsers behind its dialogs.
Nothing here reads or writes the lock store except through kitlib.lock.api.
"""
