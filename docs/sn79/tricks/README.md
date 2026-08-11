# Tricks

Non-obvious techniques in this codebase, and the failure each one is a response to.
Most were learned from a production incident; the source comments usually record the
date and the symptom.

| Doc | The problem it solves |
|---|---|
| [process_detachment.md](process_detachment.md) | work that must outlive the request — and the dashboard |
| [crash_safe_state.md](crash_safe_state.md) | a `pm2 restart` silently dropping in-flight work |
| [stale_data_handling.md](stale_data_handling.md) | a flaky upstream rendering real zeros |
| [input_validation.md](input_validation.md) | user input reaching a root shell and a wallet |
| [known_issues.md](known_issues.md) | what is currently broken |

Completion detection — why a live pid doesn't mean "still working" — lives with the
subsystem it belongs to: [../regbot/active_runs.md](../regbot/active_runs.md).

## The short version

1. **`setsid` / `start_new_session`, and capture the pid from inside the process.**
   `$!` from the parent goes stale when setsid forks.
2. **Negative pid to kill a group.** `kill -TERM -$pid` reaches the child too.
3. **`tmp` + `os.replace`** for every state file, local or over SSH.
4. **Persist identity before the side effect**, so a crash mid-launch is recoverable.
5. **"Unreachable" ≠ "didn't happen".** Raise instead of guessing when money is
   involved.
6. **A batch that launched nothing is FAILED, not DONE.**
7. **Absent ≠ zero.** Carry forward the last good value and flag it.
8. **Window your metric queries** (10 min, step 60) instead of sampling an instant.
9. **Allowlist input with a regex**; never escape your way into a root shell.
10. **Trust the log over the pid** for "is this done?".
11. **Unique suffixes on ids**, because second-resolution timestamps collide.
12. **`try` inside the worker loop**, never around it.
