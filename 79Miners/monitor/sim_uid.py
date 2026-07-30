import json, urllib.parse, urllib.request, time

API = "https://taos.simulate.trading/api/datasources/proxy/uid/c7665619-8b04-4227-9882-a44e8dad57a6/api/v1"
WALLET = "5EWwdZB7qCCMaAso5Mzcks4UUcPxKYvpAj32t5Mg1v6HSxoF"   # SN79 miner wallet
NETUID = "79"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
}

_cache = {"sim": None, "at": 0.0}
SIM_TTL = 300  # 5 min, same as the dashboard

def get_sim_id(force=False):
    now = time.time()
    if not force and _cache["sim"] and now - _cache["at"] < SIM_TTL:
        return _cache["sim"]
    # cheap probe: one metric, NO sim_id filter → one series per live sim
    selector = (f'last_over_time(miner_gauges{{wallet=~"{WALLET}", '
                f'netuid="{NETUID}", miner_gauge_name=~"score"}}[1h])')
    body = urllib.parse.urlencode({"query": selector}).encode()
    req = urllib.request.Request(API + "/query", data=body, headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    sims = {s["metric"]["sim_id"] for s in data["data"]["result"] if s["metric"].get("sim_id")}
    if not sims:
        raise RuntimeError("could not discover a live sim_id from taos")
    _cache["sim"], _cache["at"] = max(sims), now   # YYYYMMDD_HHMM → string-max = newest
    return _cache["sim"]


if __name__ == "__main__":
    print(get_sim_id())   # e.g. 20260721_0503