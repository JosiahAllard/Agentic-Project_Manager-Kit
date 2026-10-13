"""Fixed bridge launch policy shared by producers and process verifiers.

Isolated Python ignores PYTHONDONTWRITEBYTECODE. Explicit -B prevents bytecode
file additions when managed entry points and their helpers import code.
"""
PYTHON_FLAGS=('-I','-B')
