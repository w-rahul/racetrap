from collections import Counter

from deadheat.explore import Event, Outcome, holds, judge, normalize, unstable

IGNORED = {("bookings", "id")}


def outcome(a, b, *users, events=(), failed=()):
    rows = [(i + 1, u) for i, u in enumerate(users)]
    return Outcome({"A": a, "B": b}, {"bookings": (["id", "user_name"], rows)}, list(events), list(failed))


# One seat. Run one at a time, exactly one of the two gets it.
SERIALS = [outcome(200, 409, "alice"), outcome(409, 200, "bob")]


def test_a_serial_outcome_is_not_a_race():
    assert judge(outcome(409, 200, "bob"), SERIALS, IGNORED) == []


def test_double_booking_is_a_race():
    assert judge(outcome(200, 200, "alice", "bob"), SERIALS, IGNORED) == [
        "responses match no serial order",
        "final data matches no serial order",
    ]


def test_right_responses_with_wrong_data_is_a_race():
    assert judge(outcome(200, 409, "alice", "alice"), SERIALS, IGNORED) == ["final data matches no serial order"]


def test_responses_from_one_order_and_data_from_the_other_is_a_race():
    assert judge(outcome(200, 409, "bob"), SERIALS, IGNORED) == [
        "responses and data each match a different serial order"
    ]


def test_invariants_and_deadlocks_are_named():
    deadlock = Event(0.0, "B", 2, "error", "40P01 deadlock detected")
    reasons = judge(outcome(409, 200, "bob", events=[deadlock], failed=["SELECT false"]), SERIALS, IGNORED)
    assert reasons == ["invariant failed: SELECT false", "a deadlock aborted one request"]


def test_holds_skip_the_first_statement_and_count_repeats():
    trace = ["BEGIN", "SELECT n", "UPDATE t", "SELECT n", "COMMIT"]
    assert holds(trace) == [("SELECT n", 1), ("UPDATE t", 1), ("SELECT n", 2), ("COMMIT", 1)]


def test_normalize_drops_ignored_columns_and_keeps_duplicates():
    snapshot = {"bookings": (["id", "user_name"], [(1, "alice"), (2, "alice")])}
    assert normalize(snapshot, IGNORED) == {"bookings": Counter({("alice",): 2})}


def test_unstable_finds_columns_that_change_between_identical_runs():
    first = {"t": (["id", "updated_at", "n"], [(1, "10:00:01", 5)])}
    again = {"t": (["id", "updated_at", "n"], [(1, "10:00:02", 5)])}
    assert unstable(first, again) == {("t", "updated_at")}
