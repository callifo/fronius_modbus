"""How long one test may take, and who is over.

The suite has no business containing a test that runs for minutes. When one
does, it is nearly always an accident rather than a decision - a wait that was
supposed to be shortened and no longer is: a patch that landed on a name
nothing reads any more, a backoff constant read from a module the test did not
patch. The assertions still hold, so neither the mutation run nor a structural
guard can see it. What is visible from outside is the clock.
"""

# Generous on purpose. This is not a performance budget - it exists to catch a
# wait nobody meant to have, and a threshold that argues with ordinary slowness
# would get raised until it means nothing. The slowest deliberate test in this
# suite is an end-to-end run of a few seconds.
SLOW_TEST_SECONDS = 30.0


def over_budget(durations, budget=SLOW_TEST_SECONDS):
    """The tests whose total runtime passed `budget`, slowest first.

    Total, not per phase: a test that spends its time in a fixture is just as
    slow as one that spends it in the body.
    """
    too_slow = [
        (node_id, seconds) for node_id, seconds in durations.items() if seconds > budget
    ]
    return sorted(too_slow, key=lambda entry: -entry[1])
