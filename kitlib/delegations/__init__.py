"""The owner's delegation list (docs/delegations.md): the operations the owner lets the PM run.

load() reads and checks `~/.pm-kit/owner-delegations.json`; it runs and grants nothing.
"""
from .load import (FILE_NAME, Delegations, DelegationError, DelegationMissing,  # noqa: F401
                   delegations_file, load)
from .schema import Arg, Operation, check_arg                                  # noqa: F401
