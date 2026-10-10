"""Old path kept until settings point at claude/hooks/; delete then."""
import os
import runpy

runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "claude", "hooks", "coordinator_session_start.py"),
               run_name="__main__")
