"""Machine setup's scheduled jobs on Linux (docs/linux-support-plan.md slice 10): the agent temp
sweep, each repository's worktree sweep and the automatic tasks' tick, each a pm-kit job of
kitlib.platform.linux.schedule where Windows registers a scheduled task. Each step plans a job
when the Windows step would plan its task (missing; for the tick, also one that differs); the job
itself is built beside the Windows line (agenttemp.linux_job, worktreesweep.linux_job,
extras.auto_job). The host (extras.Host, or a test's fake) does the reading and writing:
task_exists(name), task_list(), job_backend(job), job_describe(job, kind), job_install(job),
systemd(), linger().

Linger (loginctl's keep-the-manager-running-without-a-sign-in) is never turned on here: with
user systemd and linger off, linger_note says so and gives the command for the owner to run.
Nothing here prints or exits.
"""
from kitlib.platform.linux import schedule


def plan_job(plan, ctx, job, verb, area='extras'):
    """Plan writing `job` (`verb`: register or update): a note instead when the machine has no
    backend for it. True when planned."""
    host = ctx.host
    try:
        kind = host.job_backend(job)
        detail = host.job_describe(job, kind)
    except (OSError, RuntimeError) as e:          # schedule.ScheduleError is a RuntimeError
        plan.note(area, 'pm-kit job %s not planned: %s' % (job.name, e))
        return False
    plan.add(area, '%s the pm-kit job %s (%s, as you, %s): %s'
             % (verb, job.name, schedule.when_text(job.when), kind, ' '.join(job.argv)),
             lambda: host.job_install(job), undo=schedule.undo_text(job.name, kind),
             detail=detail[1:])
    return True


def linger_note(plan, ctx, area='extras'):
    """A note when the machine has user systemd and linger is off: its jobs then run only while
    the user is signed in. The owner turns it on (schedule.LINGER_COMMAND); setup never does."""
    host = ctx.host
    try:
        if not host.systemd() or host.linger() is not False:
            return
    except (OSError, RuntimeError):
        return
    plan.note(area, 'linger is off for this user: user systemd runs the pm-kit jobs only while you '
                    'are signed in. To run them from boot, the owner runs: %s' % schedule.LINGER_COMMAND)
