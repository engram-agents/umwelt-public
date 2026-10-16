#!/usr/bin/env python3
"""umwelt#495 slice 1b — write-surface resolve_actor conversion.

Single-pass, line-number-keyed, content-verified. Replaces the direct
`agent = _canonical_agent(agent_raw)` fold on the write/mutate routes with
the resolve_actor + 401 block (byte-identical to the #631 read-route
pattern). Aborts loudly if any target line's content does not match
expectation — no silent partial conversion.

Second pass (direct Edit, not via this script): `wake_block` + `wake_unblock`
were added after the initial 27, once re-reading issue #621 surfaced that
Mira had named them PRIORITY surfaces ("forging them destroys or eavesdrops
rather than merely impersonates") and the initial sweep had missed them.
Both use the RETIRE pattern — caller `agent`/target resolved (as
`_actor_resolution`), subject `blocked` left on the declared fold (same
caller-vs-subject trap as retire_identity's `identity`). The issue's own
acceptance baseline ("do not let this issue be closed by a partial
conversion") made catching this before commit load-bearing.
"""
import sys

PATH = "src/umwelt/server.py"

STD = [
    '        # umwelt#495 slice 1b (write-surface conversion): resolve_actor',
    '        # replaces the direct _canonical_agent(agent_raw) fold. No token',
    "        # -> BYTE-IDENTICAL to before (resolve_actor's declared-branch",
    '        # applies the same _canonical_agent fold). A valid bearer token',
    '        # now wins over `agent_raw` entirely (anti-forgery); a presented',
    "        # bad/unknown/expired token returns 401 — fail-closed per Mira's",
    '        # ruling (presented-but-unresolvable must 401 on every converted',
    '        # route, including during the backfill window). See /umwelt/look',
    '        # for the identical read-route comment this mirrors.',
    '        resolution = resolve_actor(g.conn, request, declared=agent_raw)',
    '        if resolution.agent_id is None:',
    '            return jsonify({"error": "authentication failed"}), 401',
    '        agent = resolution.agent_id',
]

# canon=False routes: agent is required-but-unused (presence only). Same
# resolve_actor block, but the comment explains agent is held for a future
# ACL check rather than used now.
UNUSED = [
    '        # umwelt#495 slice 1b: `agent` is not yet used to authorize this',
    '        # write (v0 trust model — no ACL check is made against it here),',
    '        # but the CALLER identity is resolved through resolve_actor so a',
    '        # presented bad/unknown/expired token fails closed (401) rather',
    '        # than mutating shared state as an unverified caller. `agent` is',
    '        # held unused for the future ACL check. No token -> BYTE-IDENTICAL',
    "        # (resolve_actor's declared-branch applies the same fold).",
    '        resolution = resolve_actor(g.conn, request, declared=agent_raw)',
    '        if resolution.agent_id is None:',
    '            return jsonify({"error": "authentication failed"}), 401',
    '        agent = resolution.agent_id',
]

# use(): agent is OPTIONAL (telemetry). Introduces agent_raw; absent agent
# -> declared="" -> resolve_actor returns "" (non-None) -> passes, matching
# the old _canonical_agent("") no-op. A presented bad token still 401s.
USE = [
    '        # umwelt#495 slice 1b: resolve_actor replaces the inline',
    "        # _canonical_agent fold. `agent` is OPTIONAL here (telemetry only",
    '        # — see _record_use below), so absent agent -> declared="" ->',
    '        # resolve_actor returns "" (non-None) -> passes, byte-identical to',
    '        # the old _canonical_agent("") no-op. A PRESENTED bad token still',
    '        # returns 401 (fail-closed: a bad credential on thing-execution',
    '        # must not silently run as an unverified caller).',
    '        agent_raw = str(body.get("agent", "")).strip()',
    '        resolution = resolve_actor(g.conn, request, declared=agent_raw)',
    '        if resolution.agent_id is None:',
    '            return jsonify({"error": "authentication failed"}), 401',
    '        agent = resolution.agent_id',
]

# retire_identity(): caller param is `actor` (the maintainer), target is
# `identity`. Convert the CALLER; leave the target on the declared fold
# (same caller-vs-subject trap crossings_for_agent's `agent` param guards).
RETIRE = [
    '        # umwelt#495 slice 1b: resolve the CALLER (`actor`, the maintainer',
    '        # performing the retirement) through resolve_actor — fail-closed',
    '        # on a presented bad token before the maintainer-authorization',
    '        # check below. `identity` (the TARGET being retired) stays on the',
    '        # declared fold — it is not a caller identity, and resolving it',
    '        # would silently rewrite "retire X" into "retire me" (same',
    "        # caller-vs-subject trap crossings_for_agent's `agent` param guards).",
    '        _actor_resolution = resolve_actor(g.conn, request, declared=actor_raw)',
    '        if _actor_resolution.agent_id is None:',
    '            return jsonify({"error": "authentication failed"}), 401',
    '        actor = _actor_resolution.agent_id',
]

# --- single-line replacements: (lineno, expected_substring, block) ---
SINGLE = [
    # canon=True write routes — replace `agent = _canonical_agent(agent_raw)` line
    (5786, "agent = _canonical_agent(agent_raw)", STD),   # create_place
    (6302, "agent = _canonical_agent(agent_raw)", STD),   # customize_avatar
    (6422, "agent = _canonical_agent(agent_raw)", STD),   # move_to
    (7115, "agent = _canonical_agent(agent_raw)", STD),   # create_thing
    (7146, "agent = _canonical_agent(agent_raw)", STD),   # update_thing
    (7434, "agent = _canonical_agent(agent_raw)", STD),   # update_place
    (7521, "agent = _canonical_agent(agent_raw)", STD),   # checklist
    (7875, "agent = _canonical_agent(agent_raw)", STD),   # act
    (8306, "agent = _canonical_agent(agent_raw)", STD),   # trash_thing
    (8367, "agent = _canonical_agent(agent_raw)", STD),   # restore_thing
    (8967, "agent = _canonical_agent(agent_raw)", STD),   # take
    (9050, "agent = _canonical_agent(agent_raw)", STD),   # drop
    (9159, "agent = _canonical_agent(agent_raw)", STD),   # put
    (9252, "agent = _canonical_agent(agent_raw)", STD),   # take_out
    (9393, "agent = _canonical_agent(agent_raw)", STD),   # send_home
    (9779, "agent = _canonical_agent(agent_raw)", STD),   # send_crossing (sender)
    (10045, "agent = _canonical_agent(agent_raw)", STD),  # heartbeat
    (10403, "agent = _canonical_agent(agent_raw)", STD),  # projects (create)
    (10514, "agent = _canonical_agent(agent_raw)", STD),  # projects/<id>/tasks
    # special single-line shapes
    (6798, "agent = _canonical_agent(str(body.get(\"agent\"", USE),    # use
    (8520, "actor = _canonical_agent(actor_raw)", RETIRE),              # retire_identity
]

# --- range replacements: (start, end_inclusive, [expected_substr_per_line], block) ---
# canon=False "required but not yet resolved" comment -> UNUSED block
RANGE = [
    (5868, 5870, ["# `agent` is required but not yet resolved", "same caveat create_place's", "ownership ACL check is made"], UNUSED),  # set_parent
    (5918, 5920, ["# `agent` is required but not yet resolved", "same caveat create_place's", "ownership ACL check is made"], UNUSED),  # add_link
    (5961, 5963, ["# `agent` is required but not yet resolved", "same caveat create_place's", "ownership ACL check is made"], UNUSED),  # remove_link
    (10663, 10664, ["# `agent` is required but not yet resolved", "same caveat create_place's"], UNUSED),  # tasks/<id>/deps
]

# --- insertions after a guard line: (lineno, expected_substring, block) ---
# canon=False routes with no "required-but-not-resolved" comment: insert
# UNUSED block immediately AFTER the presence guard (guard line kept).
INSERT_AFTER = [
    (10447, '"agent is required"', UNUSED),   # projects/<id>/update
    (10192, '"agent, kind, and ref are required"', UNUSED),  # record
]


def main():
    lines = open(PATH).read().splitlines(keepends=False)
    # build a map: 1-indexed lineno -> list of replacement lines
    repl = {}  # lineno -> [lines]; for ranges, every line in [start,end] is consumed
    consumed = set()  # line numbers consumed by a range/insert

    # single-line
    for lineno, substr, block in SINGLE:
        idx = lineno - 1
        actual = lines[idx]
        if substr not in actual:
            print(f"FAIL single @ {lineno}: expected substring {substr!r}\n  got: {actual!r}")
            sys.exit(1)
        if lineno in consumed:
            print(f"FAIL: lineno {lineno} targeted twice")
            sys.exit(1)
        repl[lineno] = block
        consumed.add(lineno)

    # ranges (replace start..end inclusive with block)
    for start, end, subs, block in RANGE:
        for i, sub in enumerate(subs):
            ln = start + i
            idx = ln - 1
            actual = lines[idx]
            if sub not in actual:
                print(f"FAIL range @ {ln}: expected {sub!r}\n  got: {actual!r}")
                sys.exit(1)
            if ln in consumed:
                print(f"FAIL: lineno {ln} targeted twice")
                sys.exit(1)
            consumed.add(ln)
        # mark start as the emit point; the rest are consumed (skip)
        repl[start] = block
        for ln in range(start + 1, end + 1):
            repl[ln] = []  # drop (consumed by the range)

    # insertions after guard: keep guard line, append block
    for lineno, substr, block in INSERT_AFTER:
        idx = lineno - 1
        actual = lines[idx]
        if substr not in actual:
            print(f"FAIL insert_after @ {lineno}: expected {substr!r}\n  got: {actual!r}")
            sys.exit(1)
        if lineno in consumed:
            print(f"FAIL: lineno {lineno} targeted twice")
            sys.exit(1)
        consumed.add(lineno)
        repl[lineno] = [actual, *block]  # original guard line + block

    # emit
    out = []
    n = len(lines)
    i = 0
    while i < n:
        ln = i + 1
        if ln in repl:
            out.extend(repl[ln])
        else:
            out.append(lines[i])
        i += 1

    open(PATH, "w").write("\n".join(out) + "\n")
    single_ct = sum(1 for s, _, _ in SINGLE)
    range_ct = len(RANGE)
    ins_ct = len(INSERT_AFTER)
    print(f"OK — converted {single_ct} single-line + {range_ct} range + {ins_ct} insert-after = {single_ct+range_ct+ins_ct} routes")
    print(f"file lines: {n} -> {len(out)}")


if __name__ == "__main__":
    main()