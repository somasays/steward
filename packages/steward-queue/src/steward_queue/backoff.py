"""Retry scheduling policy.

Separate from the SQL so the policy is a pure function: given how many
attempts a task has burned, how long before it becomes claimable again. Kept
deterministic (no implicit jitter source) so the retry-then-dead integration
test asserts an exact schedule rather than a range.
"""

from datetime import timedelta

DEFAULT_BASE_DELAY = timedelta(seconds=1)
DEFAULT_FACTOR = 2.0
DEFAULT_MAX_DELAY = timedelta(minutes=10)


def retry_delay(
    attempts: int,
    *,
    base: timedelta = DEFAULT_BASE_DELAY,
    factor: float = DEFAULT_FACTOR,
    cap: timedelta = DEFAULT_MAX_DELAY,
) -> timedelta:
    """Delay before a task that has burned `attempts` attempts is retried.

    Exponential in the number of attempts already made (`attempts=1` -> `base`),
    clamped to `cap`. `attempts <= 0` is treated as the first attempt rather
    than rejected: a caller that has not yet recorded an attempt still deserves
    the base delay, not an exception on the failure path.

    Grown by repeated multiplication that stops at the cap rather than by
    `base * factor ** attempts`: the closed form overflows long before it
    reaches a delay anyone would wait, and a failure path is the last place
    that should be able to raise.
    """
    if base <= timedelta(0) or factor <= 1.0:
        return min(max(base, timedelta(0)), cap)
    seconds, cap_seconds = base.total_seconds(), cap.total_seconds()
    for _ in range(max(attempts, 1) - 1):
        seconds *= factor
        if seconds >= cap_seconds:
            return cap
    return min(timedelta(seconds=seconds), cap)
