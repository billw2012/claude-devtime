"""Text summary of a project's time (the old profile_sessions.py output), from the same dataset as the dashboard."""

import collections


def fmt(seconds):
    h, rem = divmod(int(seconds), 3600)
    return f"{h}h{rem // 60:02d}" if h else f"{rem // 60}m{rem % 60:02d}"


def report(data, top=15, since=None):
    waiting = set(data["waiting"]) | {"overhead"}
    kinds, labels, iv = data["kinds"], data["labels"], data["iv"]
    transcripts = data["transcripts"]
    total = collections.Counter()
    main_active = main_wait = 0.0
    per_t = collections.defaultdict(collections.Counter)
    slow = []
    for n in range(len(iv["t"])):
        if since and iv["e"][n] < since:
            continue
        kind = kinds[iv["k"][n]]
        t = transcripts[iv["t"][n]]
        if iv["f"][n] & data["flags"]["noshare"]:
            continue
        share = iv["w"][n]
        if since and iv["s"][n] < since:
            share *= (iv["e"][n] - since) / max(iv["e"][n] - iv["s"][n], 1e-9)
        per_t[t["idx"]][kind] += share
        if kind not in waiting:
            total[kind] += share
        if t["role"] == "main":
            if kind in waiting:
                main_wait += share
            else:
                main_active += share
        if kind not in ("model", "compaction") and kind not in waiting:
            slow.append((iv["e"][n] - iv["s"][n], kind, f"{t['description'] or t['title']}: {labels[iv['l'][n]]}"))
    agent = sum(total.values())
    print(f"Agent time (every session + subagent, busy): {fmt(agent)}")
    subs = [t for t in transcripts if t["role"] != "main" and per_t[t["idx"]]]
    print(f"Main sessions: active {fmt(main_active)}, idle/waiting {fmt(main_wait)}; {len(subs)} subagents")
    cost = sum(t["cost"] for t in transcripts)
    print(f"Estimated cost: ${cost:,.2f}\n")
    print("Where agent time goes:")
    for kind, value in total.most_common():
        print(f"  {kind:28s} {fmt(value):>7s}  {100 * value / max(agent, 1):5.1f} %")
    from . import overview
    mon = overview.monitoring(data, overview.Index(data), since or 0, float("inf"), cost)
    if mon["total"]["requests"]:
        print("\nMonitoring cost (model turns started by Monitor events):")
        t = mon["total"]
        print(f"  {'total':28s} {t['wakes']:5d} wakes  ${t['cost']:8.2f}  ${(t['avgPerWake'] or 0):.3f}/wake"
              f"  {100 * (t['share'] or 0):.1f} % of cost")
        for r in mon["byTag"]:
            print(f"  {r['tag']:28s} {r['wakes']:5d} wakes  ${r['cost']:8.2f}  ${(r['avgPerWake'] or 0):.3f}/wake")
    mine = overview.own_usage(data, overview.Index(data), since or 0, float("inf"), cost)
    if mine["requests"]:
        split = ", ".join(f"{k} ${v:,.2f}" for k, v in mine["byCategory"].items() if v)
        print(f"\ndevtime's own usage: ${mine['cost']:,.2f} ({100 * (mine['share'] or 0):.1f} % of cost; {split})")
    print(f"\nSlowest subagents (busy; top {top}):")
    ranked = sorted(subs, key=lambda t: -sum(v for k, v in per_t[t["idx"]].items() if k not in waiting))
    for t in ranked[:top]:
        secs = per_t[t["idx"]]
        busy = sum(v for k, v in secs.items() if k not in waiting)
        top5 = ", ".join(f"{k} {fmt(v)}" for k, v in secs.most_common(6) if k not in waiting)
        print(f"  {fmt(busy):>7s}  {t['description'][:60]:60s}  {top5}")
    print(f"\nSlowest single tool calls (top {top}):")
    for duration, kind, label in sorted(slow, reverse=True)[:top]:
        print(f"  {fmt(duration):>7s}  {kind:24s} {label[:100]}")
