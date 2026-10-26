"""Device interconnection map (topology) from /ip/neighbor (MNDP/LLDP/CDP) + CAPsMAN.

MNDP passes through bridges, so a device also "sees" neighbours several hops away.
Indirect edges are therefore filtered out: edge A-C is redundant when A sees B on the
same port and B sees C on a different port than A (B lies between them).

RouterOS reports the interface as "ether5,bridge" (local) or "bridge/ether1" (remote);
only the physical port is used. A neighbour seen only on a virtual interface (VLAN,
bridge) is somewhere in the same L2 domain, not necessarily on the other end of a cable,
so such sightings never create a cable link. They are used only to attach a device that
would otherwise stay disconnected (kind "l2").

A device that announces itself to nobody (e.g. behind an unmanaged switch) is placed from
the bridge MAC tables instead (kind "fdb"): the port where another device learned its MAC.
When other wired clients share that port, there is a switch in between; it is drawn as an
unmanaged node and those clients are moved under it.
"""
import re

# DHCP host names that look like a switch (TP-Link TL-SG…, Netgear GS…, Ubiquiti USW…)
_SWITCH_NAME = re.compile(r"switch|^tl-s[gf]|^sg\d|^gs\d|^usw|^es-\d", re.I)


def _norm(s) -> str:
    return str(s or "").strip().lower().replace("_", "-")


def _is_wireless(iface: str | None) -> bool:
    i = _norm(iface)
    return i.startswith(("wlan", "wifi", "cap", "wl-"))


def _virtual(token: str) -> bool:
    t = _norm(token)
    return not t or t.startswith(("bridge", "br-")) or "vlan" in t


def _phys_port(iface) -> str | None:
    """Physical port from "ether5,bridge" / "bridge/ether1"; None for a virtual interface."""
    for tok in str(iface or "").replace("/", ",").split(","):
        if not _virtual(tok):
            return tok.strip()
    return None


def build(raw: list[dict], client_counts: dict[str, int] | None = None,
          clients: list[dict] | None = None) -> dict:
    client_counts = dict(client_counts or {})
    clients = clients or []
    by_ident, by_mac, by_host = {}, {}, {}
    nodes = []
    for d in raw:
        name = d["name"]
        by_ident[_norm(name)] = name
        if d.get("identity"):
            by_ident[_norm(d["identity"])] = name
        by_host[d.get("host")] = name
        for m in d.get("own_macs") or []:
            by_mac[str(m).upper()] = name
        r, rb = d.get("resource") or {}, d.get("routerboard") or {}
        nodes.append({"id": name, "label": d.get("identity") or name, "role": d.get("role"),
                      "host": d.get("host"), "reachable": bool(d.get("reachable")),
                      "model": rb.get("model") or r.get("board"), "managed": True,
                      "clients": client_counts.get(name, 0)})

    root = next((d["name"] for d in raw if d.get("role") == "router"), None) \
        or next((d["name"] for d in raw if d.get("role") == "switch"), None) \
        or (raw[0]["name"] if raw else None)

    def resolve(n: dict) -> str | None:
        return (by_mac.get(n.get("mac") or "") or by_ident.get(_norm(n.get("identity")))
                or by_host.get(n.get("address")))

    # observations: (A, B) -> {"port": port on A, "remote": port on B as reported by A}
    # physical sightings in `seen`, sightings only on a virtual interface in `l2_seen`
    seen: dict[tuple, dict] = {}
    l2_seen: dict[tuple, dict] = {}
    external: dict[str, dict] = {}
    for d in raw:
        a = d["name"]
        for n in d.get("neighbors") or []:
            b = resolve(n)
            if b == a:
                continue
            if b is None:
                key = n.get("mac") or _norm(n.get("identity"))
                if not key:
                    continue
                b = "ext:" + key
                external.setdefault(b, {"id": b, "label": n.get("identity") or n.get("mac"),
                                        "role": "external", "host": n.get("address"),
                                        "reachable": True, "managed": False, "clients": 0,
                                        "model": n.get("board") or n.get("platform")})
            port = _phys_port(n.get("interface"))
            obs = {"port": port, "remote": _phys_port(n.get("remote_interface"))}
            if port:
                seen.setdefault((a, b), obs)
            else:
                l2_seen.setdefault((a, b), {**obs, "port": n.get("interface")})

    def port(a, b):
        return (seen.get((a, b)) or {}).get("port")

    managed = [d["name"] for d in raw]

    def redundant(a, c) -> bool:
        pa = port(a, c)
        for b in managed:
            if b in (a, c) or port(a, b) != pa or (b, c) not in seen:
                continue
            pb_c, pb_a = port(b, c), port(b, a)
            if pb_a is not None and pb_c != pb_a:
                return True
        return False

    edges: dict[tuple, dict] = {}
    for (a, b), obs in seen.items():
        if redundant(a, b) or ((b, a) in seen and redundant(b, a)):
            continue
        k = tuple(sorted((a, b)))
        e = edges.setdefault(k, {"a": k[0], "b": k[1], "a_port": None, "b_port": None,
                                 "kind": "wired", "mutual": False})
        mine, theirs = obs["port"], obs["remote"]
        if a == k[0]:
            e["a_port"] = e["a_port"] or mine
            e["b_port"] = e["b_port"] or theirs
        else:
            e["b_port"] = e["b_port"] or mine
            e["a_port"] = e["a_port"] or theirs
        e["mutual"] = e["mutual"] or (b, a) in seen
        if _is_wireless(mine) or _is_wireless(theirs):
            e["kind"] = "wireless"

    # MAC tables: devices without a physical link go to the port where their MAC is learned
    raw_by = {d["name"]: d for d in raw}
    linked = {n for k in edges for n in k}
    toward_managed = {(e["a"], e["a_port"]) for e in edges.values()} | \
        {(e["b"], e["b_port"]) for e in edges.values()}
    client_parent: dict[str, str] = {}
    hidden: set[str] = set()
    for name in managed:
        if name in linked:
            continue
        macs = {str(m).upper() for m in raw_by[name].get("own_macs") or []}
        cands = {(a["name"], h.get("interface")) for a in raw if a["name"] != name
                 for h in a.get("bridge_hosts") or []
                 if str(h.get("mac") or "").upper() in macs and h.get("interface")
                 and (a["name"], h.get("interface")) not in toward_managed}
        if not cands:
            continue
        a, port = min(cands, key=lambda c: (c[0] != root, c[0] not in linked, c))
        # the device's own uplink port: where it learned a MAC of the observer
        a_macs = {str(m).upper() for m in raw_by[a].get("own_macs") or []}
        d_hosts = raw_by[name].get("bridge_hosts") or []
        uplink = next((h.get("interface") for h in d_hosts
                       if str(h.get("mac") or "").upper() in a_macs), None)
        behind = {str(h.get("mac") or "").upper() for h in d_hosts
                  if uplink and h.get("interface") != uplink}
        others = [c for c in clients if c.get("device") == a and c.get("interface") == port
                  and c.get("conn") == "eth" and c.get("mac") not in behind]
        kind = "wireless" if _is_wireless(port) else "fdb"
        if not others:
            k = tuple(sorted((a, name)))
            edges[k] = {"a": k[0], "b": k[1], "kind": kind, "mutual": False,
                        "a_port": port if k[0] == a else uplink,
                        "b_port": uplink if k[0] == a else port}
        else:
            sw = next((c for c in others
                       if _SWITCH_NAME.search(c.get("hostname") or c.get("comment") or "")), None)
            sid = f"ext:sw:{a}:{port}"
            nodes.append({"id": sid, "label": sw and (sw.get("hostname") or sw.get("comment")),
                          "role": "external", "host": sw and sw.get("ip"), "reachable": True,
                          "managed": False, "model": sw and sw.get("vendor"),
                          "clients": len(others) - (1 if sw else 0), "inferred": True})
            if sw:
                hidden.add(sw["mac"])
            for c in others:
                if c is not sw:
                    client_parent[c["mac"]] = sid
            client_counts[a] = client_counts.get(a, 0) - len(others)
            edges[(a, sid)] = {"a": a, "b": sid, "kind": "fdb", "mutual": False,
                               "a_port": port, "b_port": None}
            edges[(sid, name)] = {"a": sid, "b": name, "kind": "fdb", "mutual": False,
                                  "a_port": None, "b_port": uplink}
        linked.update((a, name))
    for n in nodes:
        if n.get("managed"):
            n["clients"] = client_counts.get(n["id"], 0)

    # CAPsMAN: AP <-> controller when no physical edge is seen
    linked = {n for k in edges for n in k}
    for d in raw:
        for ident in d.get("remote_caps") or []:
            ap = by_ident.get(_norm(ident))
            if not ap or ap == d["name"]:
                continue
            k = tuple(sorted((d["name"], ap)))
            if k in edges:
                edges[k]["capsman"] = True
            elif ap not in linked:
                edges[k] = {"a": k[0], "b": k[1], "a_port": None, "b_port": None,
                            "kind": "capsman", "mutual": False, "capsman": True}

    # devices seen only over a VLAN/bridge: attach to the observer closest to the root
    linked = {n for k in edges for n in k}
    for name in managed:
        if name in linked:
            continue
        observers = sorted({a for (a, b) in l2_seen if b == name} | {b for (a, b) in l2_seen if a == name},
                           key=lambda o: (o != root, o not in linked, o))
        if not observers:
            continue
        o = observers[0]
        obs = l2_seen.get((o, name)) or {}
        rev = l2_seen.get((name, o)) or {}
        k = tuple(sorted((o, name)))
        mine, theirs = obs.get("port") or rev.get("remote"), obs.get("remote") or rev.get("port")
        edges[k] = {"a": k[0], "b": k[1], "kind": "l2", "mutual": bool(obs and rev),
                    "a_port": mine if k[0] == o else theirs, "b_port": theirs if k[0] == o else mine}
        linked.update(k)

    used_ext = {n for k in edges for n in k if n in external}
    nodes += [external[x] for x in sorted(used_ext)]

    return {"nodes": nodes, "edges": list(edges.values()), "root": root,
            "client_parent": client_parent, "hidden_clients": sorted(hidden),
            "has_neighbors": any(d.get("neighbors") for d in raw)}
