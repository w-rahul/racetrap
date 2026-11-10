import http.client
from collections import Counter

import pytest

from racetrap.explore import (
    Event,
    Outcome,
    Pair,
    RunError,
    Schedule,
    body_key,
    captured,
    differing,
    fill,
    footprint,
    holds,
    independent,
    judge,
    normalize,
    shape,
    unstable,
)
from racetrap.report import fix
from racetrap.scenario import Request

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


def test_holds_leave_out_orders_that_are_serial_anyway():
    reset = "SELECT pg_advisory_unlock_all();\nCLOSE ALL;\nUNLISTEN *;\nRESET ALL;"
    trace = ["BEGIN;", "SELECT n", "UPDATE t", "SELECT n", "COMMIT;", reset]
    # before the first SELECT A has only opened a transaction, and after COMMIT it is done
    assert holds(trace) == [("UPDATE t", 1), ("SELECT n", 2), ("COMMIT;", 1)]


def test_holds_keep_every_gap_between_statements_that_commit_on_their_own():
    assert holds(["SELECT n", "UPDATE t", "INSERT x"]) == [("UPDATE t", 1), ("INSERT x", 1)]


NAMES = {"shows", "bookings", "users", "audit"}
PARENTS, CHILDREN = {"bookings": {"shows"}}, {"shows": {"bookings"}}


def test_footprint_reads_tables_from_sql_and_follows_foreign_keys():
    trace = ["BEGIN;", "SELECT seats_left FROM shows WHERE id = $1", "INSERT INTO bookings (show_id) VALUES ($1)", "COMMIT;"]
    assert footprint(trace, NAMES, set(), PARENTS, CHILDREN, set()) == ({"shows", "bookings"}, {"bookings"})
    # an insert reads the parent row, and a delete can cascade into the child table
    assert footprint(["INSERT INTO bookings VALUES ($1)"], NAMES, set(), PARENTS, CHILDREN, set())[0] == {"shows", "bookings"}
    assert footprint(["DELETE FROM shows WHERE id = $1"], NAMES, set(), PARENTS, CHILDREN, set())[1] == {"shows", "bookings"}
    # rows that changed count as written even when no statement names the table
    assert footprint(["SELECT ?"], NAMES, set(), {}, {}, {"audit"}) == ({"audit"}, {"audit"})


def test_footprint_gives_up_when_sql_cannot_tell():
    assert footprint(["SELECT book_seat($1)"], NAMES, {"book_seat"}, {}, {}, set()) is None, "a function of your own"
    assert footprint(["SELECT * FROM somewhere_else"], NAMES, set(), {}, {}, set()) is None, "a relation racetrap does not know"
    assert footprint(["UPDATE users SET name = $1"], NAMES, {"users"}, {}, {}, set()) is None, "a table with a trigger"


def test_only_pairs_that_cannot_affect_each_other_are_independent():
    reader, writer, other = ({"users"}, set()), ({"users"}, {"users"}), ({"audit"}, {"audit"})
    assert independent(reader, reader), "two reads cannot race"
    assert independent(writer, other)
    assert not independent(reader, writer)
    assert not independent(writer, writer)
    assert not independent(None, other), "an unknown footprint is never skipped"


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


def answered(a, b, *users, a_body, b_body):
    out = outcome(a, b, *users)
    out.bodies = {"A": a_body, "B": b_body}
    return out


# An API that answers 200 for everything and says what happened in the body.
WON, LOST = '{"booked": true, "id": 7}', '{"booked": false, "error": "sold out"}'
BODY_SERIALS = [answered(200, 200, "alice", a_body=WON, b_body=LOST), answered(200, 200, "bob", a_body=LOST, b_body=WON)]


def test_bodies_tell_a_race_when_every_status_is_200():
    both_won = answered(200, 200, "alice", "bob", a_body=WON, b_body=WON)
    assert judge(both_won, BODY_SERIALS, IGNORED) == ["response bodies match no serial order", "final data matches no serial order"]
    assert judge(answered(200, 200, "bob", a_body=LOST, b_body=WON), BODY_SERIALS, IGNORED) == []


def test_generated_and_unstable_fields_do_not_count():
    # the id differs, and so does a field that changed between two identical runs
    other_id = answered(200, 200, "alice", a_body='{"booked": true, "id": 9, "queue": 3}', b_body=LOST)
    serials = [answered(200, 200, "alice", a_body='{"booked": true, "id": 7, "queue": 1}', b_body=LOST), BODY_SERIALS[1]]
    assert judge(other_id, serials, IGNORED, loose={"A": {("queue",)}, "B": set()}) == []
    assert judge(other_id, serials, IGNORED) == ["response bodies match no serial order"]


def test_differing_finds_fields_that_change_on_their_own():
    first = '{"user": {"name": "alice", "seen": "10:00:01"}, "tags": ["a"]}'
    again = '{"user": {"name": "alice", "seen": "10:00:02"}, "tags": ["a", "b"]}'
    assert differing(first, again) == {("user", "seen"), ("tags", 1)}
    assert differing("plain text", "other text") == {()}
    assert differing(None, None) == set()


def test_body_key_drops_ids_tokens_and_timestamps():
    body = '{"id": 1, "user_id": 2, "createdAt": "x", "access_token": "t", "seat": "F12", "items": [{"orderId": 5, "qty": 2}]}'
    assert body_key(body, set()) == {(("seat",), "'F12'"), (("items", 0, "qty"), "2")}
    assert body_key(None, set()) is None


def test_captured_reads_json_fields_and_headers():
    headers = http.client.HTTPMessage()
    headers["Set-Cookie"] = "session=abc; Path=/; HttpOnly"
    headers["Set-Cookie"] = "csrf=xyz; Path=/"
    body = b'{"data": {"tokens": [{"value": "t-1"}]}}'
    assert captured("data.tokens.0.value", body, headers, "alice") == "t-1"
    assert captured("header:set-cookie", body, headers, "alice") == "session=abc; csrf=xyz"
    with pytest.raises(RunError, match="no data.missing"):
        captured("data.missing", body, headers, "alice")
    with pytest.raises(RunError, match="no x-token header"):
        captured("header:x-token", body, headers, "alice")


def test_fill_puts_captured_values_everywhere():
    values = {"alice.token": "t-1"}
    assert fill("/users/${alice.token}", values) == "/users/t-1"
    assert fill({"authorization": "Bearer ${alice.token}", "n": [1, "${alice.token}"]}, values) == {"authorization": "Bearer t-1", "n": [1, "t-1"]}


def test_a_unique_violation_gets_its_own_reason_and_fix():
    insert = "INSERT INTO users (email) VALUES (?)"
    a = Request("a", "POST", "/signup")
    serials = [Outcome({"A": 200, "B": 400}, {}, [], []), Outcome({"A": 400, "B": 200}, {}, [], [])]
    events = [Event(0.0, "A", 1, "error", "23505 duplicate key value violates unique constraint")]
    forced = Outcome({"A": 500, "B": 200}, {}, events, [])
    reasons = judge(forced, serials, set())
    assert reasons == ["responses match no serial order", "a unique violation was not handled"]
    advice = fix(Pair(a, a, ["SELECT 1", insert], serials, set()), Schedule((insert, 1), forced, reasons), "a", "a again")
    assert "ON CONFLICT DO NOTHING" in advice and "FOR UPDATE" not in advice
