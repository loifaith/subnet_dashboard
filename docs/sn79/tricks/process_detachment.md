# Detaching long-running processes

Two subsystems launch work that must outlive the request — and, for one of them, the
whole dashboard process. Both got there the hard way.

## RegBot: remote detachment over SSH

`trigger_run()` — [regbot.py:519-555](../../../79Miners/monitor/regbot.py#L519-L555)

```sh
set -m;
(chmod +x SCRIPT && cd REGBOT_DIR && exec setsid sh -c 'echo $$ > PIDFILE; exec SCRIPT') \
  </dev/null >LOGFILE 2>&1 &
disown -a; echo LAUNCHED
```

Four things are load-bearing.

### 1. One subshell, backgrounded atomically

The earlier version was `cmd1 && cmd2 && cmd3 &` followed by separate `disown` /
`echo` statements. In a **non-interactive** shell there is no job control unless
`set -m` is set, so plain `disown` is a **no-op** — bash still implicitly waited for
the backgrounded job before letting the script end. The launch command never
returned early, so **every trigger hit its 20s timeout** even though the underlying
process (kept alive by `setsid`) was running fine.

`set -m` fixes `disown` too, kept as defence in depth.

### 2. The process reports its own pid

```sh
setsid sh -c 'echo $$ > PIDFILE; exec SCRIPT'
```

**Not** `$!` from the outer shell. `setsid CMD` forks internally whenever its caller
is a process-group leader — which `set -m` makes the backgrounded subshell. In that
case setsid's own process exits right after forking off the real, longer-lived
child, so a `$!` captured from outside **goes stale within seconds** while the actual
work continues.

`$$` written from inside the process that then `exec`s into the real script is
correct regardless of whether setsid forked, because `exec` preserves the pid.

### 3. `setsid` makes the pid a process-group leader

Which is what lets `kill_run()` use a **negative pid**:

```sh
kill -TERM -"$pid"
```

That reaches the `run79.sh` shell **and** its `creg79.py` child together. A plain
`kill "$pid"` would only hit the shell and orphan `creg79.py`, which would carry on
spending TAO.

### 4. `</dev/null` and full redirection

Without them the SSH channel stays open waiting on the child's stdio and the command
does not return.

## Unstake: local detachment via process group

`_run_unstake_env()` — [unstake.py:166-174](../../../79Miners/monitor/unstake.py#L166-L174)

```python
proc = await asyncio.create_subprocess_exec(
    *cmd, start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1"}, ...)
```

`start_new_session=True` puts the child in its **own process group**. Without it,
`pm2 restart dashboard` — to deploy an unrelated code change — sends its termination
signal to the whole process group, which reaches this child and kills an in-flight
unstake mid-extrinsic.

Observed in practice: a restart SIGINT'd a run partway through its 5th hotkey,
surfacing as a scary `KeyboardInterrupt` traceback even though the on-chain work had
already gone through.

Same reasoning as RegBot's `setsid`, different mechanism because the child is local.

`PYTHONUNBUFFERED=1` is in the same call for a different reason: Python
block-buffers stdout when it isn't a TTY, so without it the log file stays empty
until process exit and there is nothing to tail.

## The general rule

> If a process must survive the thing that started it, put it in **its own session
> or process group**, and capture its pid **from inside itself**.

Everything else — pid files, kill escalation, log tailing — depends on getting those
two right.
