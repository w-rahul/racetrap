from collections import Counter

from racetrap.explore import Event, Outcome, holds, judge, normalize, shape, unstable

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


def test_an_invariant_broken_in_every_serial_order_is_not_a_race():
    serials = [outcome(200, 409, "alice", failed=["Q"]), outcome(409, 200, "bob", failed=["Q"])]
    assert judge(outcome(409, 200, "bob", failed=["Q"]), serials, IGNORED) == []


def signup(accounts, memberships):
    return {
        "users": (["id", "name"], [(1, "alice"), (2, "bob")]),
        "accounts": (["id", "name"], accounts),
        "memberships": (["user_id", "account_id"], memberships),
    }


def test_generated_ids_compare_by_the_rows_they_point_to():
    ignored = {("users", "id"), ("accounts", "id")}
    fks = {("memberships", "user_id"): ("users", "id"), ("memberships", "account_id"): ("accounts", "id")}
    serial = normalize(signup([(1, "alice"), (2, "bob")], [(1, 1), (2, 2)]), ignored, fks)
    # bob's account got id 1 this time, and every link still points to the right owner
    swapped = signup([(1, "bob"), (2, "alice")], [(1, 2), (2, 1)])
    crossed = signup([(1, "bob"), (2, "alice")], [(1, 1), (2, 2)])
    assert normalize(swapped, ignored, fks) == serial
    assert normalize(crossed, ignored, fks) != serial


def test_shape_blanks_literals_but_keeps_placeholders():
    assert shape("UPDATE t SET n = 5, s = 'it''s' WHERE id = 12") == "UPDATE t SET n = ?, s = ? WHERE id = ?"
    assert shape("SELECT * FROM t1 WHERE id = $1") == "SELECT * FROM t1 WHERE id = $1"
