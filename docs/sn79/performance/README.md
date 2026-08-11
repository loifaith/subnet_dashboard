# Performance

Every doc here exists because of a measured production problem, not a
micro-optimisation. The measurements are preserved in the source comments.

| Doc | Problem | Result |
|---|---|---|
| [chain_subprocess.md](chain_subprocess.md) | substrate's pure-Python metadata decode holds the GIL; an unrelated request waited **13.7 s** | every chain query runs in a child process; one connection per run |
| [ssh_multiplexing.md](ssh_multiplexing.md) | 125–160 ms RTT × ~10 handshake round-trips per operation | one persistent master per box, kept warm; **3.0 s → 0.5 s** per params push |
| [cache.md](cache.md) | expensive data on every page load | five caching layers, each matched to its data's shape |
| [concurrent_processing.md](concurrent_processing.md) | one event loop, many slow remote things | subprocess vs. thread vs. task, and where serial is deliberate |

## The three rules

1. **GIL-bound work goes in a subprocess. I/O-bound work goes in a thread.**
   Getting this backwards is what caused the 13.7 s stalls, and `asyncio.to_thread`
   looked like the fix at the time. It wasn't.

2. **Count round-trips, not bytes.** A 4 KB params file is free; the SSH handshake
   isn't. This is why the push abandoned rsync, why masters are multiplexed and kept
   warm, why bulk restart groups by box, and why the balance sweep uses one
   connection for every address.

3. **Move expensive work off the request path onto a schedule.** The daily status
   file is the primary read path, not a fallback. A page load costs a file read.

## Quick reference: what costs what

| Operation | Typical | Timeout |
|---|---|---|
| Metagraph load (cold) | 10–60 s | 90 s |
| Reg cost chain query | ~5 s | 120 s |
| Balance sweep, all coldkeys | tens of s | 300 s |
| Daily status refresh | minutes | 600 s |
| Params push (warm master) | ~0.5 s | 60 s |
| Params push (cold) | ~2.5–3 s | 60 s |
| `pm2 restart` (warm) | ~0.5 s | 60 s |
| SSH active-run probe | ~0.2–1 s | 12 s |
| Unstake run | minutes | 1800 s |
