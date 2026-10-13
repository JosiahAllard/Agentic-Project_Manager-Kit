"""The error the named-pipe and process-identity helpers raise (kitlib.proc.pipe).

`code` is a short fixed string such as 'pipe-owner-mismatch' or 'private-pipe-accept-timeout';
callers branch on it. It never carries a path, a pid or other caller data.
"""


class ProcError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__('process call refused: %s' % code)
