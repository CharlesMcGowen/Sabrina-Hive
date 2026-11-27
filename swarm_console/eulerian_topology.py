"""Bipartite m-Eulerian tree topology, planar closure, SharingPolicy seams, and LOG_fin promotion gates.

Implements the Goulden-Serrano (arXiv:math/0508226) genus-0 branched cover bijection for
recursive self-improvement (RSI) swarms:
  - Inner white vertices (hubs, u): weight i >= 1, total degree m * i, exactly (i - 1) inner-deg-1 arbiters.
  - Inner black vertices of inner-degree 1 (arbiters, z): total degree m (1 hub + m - 1 white leaves).
  - Inner black vertices of inner-degree 2 (bridges, z): total degree m (2 hubs + m - 2 white leaves);
    the sole legal seam between two sub-spheres, carrying a SharingPolicy.
  - Planar map closure (Bousquet-Melou-Schaeffer): uniquely pairs every white leaf with a black leaf
    without crossings (internal LOG_da feedback loops), leaving exactly m unpaired black leaves on the
    outer face per connected sphere (finalized LOG_fin promotion ports).
  - Genus-0 Riemann-Hurwitz invariant: c(alpha) = (m - 1)n - l(alpha) + 2 black leaves per sphere,
    and B - W == m per sphere.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

NodeKind = Literal["hub", "arbiter", "bridge", "b_leaf", "w_leaf"]


class EulerianInvariantError(ValueError):
    """Raised when a topology mutation would violate g=0, local degrees, or LOG_fin invariants."""


@dataclass(frozen=True)
class SharingPolicy:
    """Explicit boundary contract enforced at every inner-degree-2 Join-Cut Mission Bridge."""

    allowed_kinds: frozenset[str] = frozenset({"summary", "verified_artifact", "handoff"})
    max_excerpt_chars: int = 3200
    allow_raw_log_da: bool = False

    def allows(self, artifact_kind: str, *, payload_chars: int = 0) -> bool:
        kind = str(artifact_kind or "").strip().lower()
        if kind == "log_da" and not self.allow_raw_log_da:
            return False
        if kind not in self.allowed_kinds:
            return False
        if payload_chars > self.max_excerpt_chars:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_kinds": sorted(self.allowed_kinds),
            "max_excerpt_chars": self.max_excerpt_chars,
            "allow_raw_log_da": self.allow_raw_log_da,
        }


@dataclass
class TopologyNode:
    """A vertex in the planar bipartite m-Eulerian forest."""

    id: str
    kind: NodeKind
    weight: int = 1
    neighbors: list[str] = field(default_factory=list)  # explicit cyclic planar order
    policy: SharingPolicy | None = None
    label: str = ""
    owner_worker_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "weight": self.weight,
            "neighbors": list(self.neighbors),
            "label": self.label,
        }
        if self.owner_worker_id is not None:
            data["owner_worker_id"] = self.owner_worker_id
        if self.policy is not None:
            data["policy"] = self.policy.to_dict()
        return data


@dataclass(frozen=True)
class SphereClosure:
    """Planar map closure result for a single connected genus-0 sphere."""

    sphere_index: int
    node_ids: tuple[str, ...]
    hub_ids: tuple[str, ...]
    alpha: tuple[int, ...]
    n_sum: int
    black_leaf_count: int
    white_leaf_count: int
    log_da_pairs: tuple[tuple[str, str], ...]  # (w_leaf_id, b_leaf_id)
    log_fin_ports: tuple[str, ...]             # exactly m unpaired outer-face b_leaf IDs

    def to_dict(self) -> dict[str, Any]:
        return {
            "sphere_index": self.sphere_index,
            "hub_ids": list(self.hub_ids),
            "alpha": list(self.alpha),
            "n_sum": self.n_sum,
            "l_alpha": len(self.alpha),
            "black_leaf_count": self.black_leaf_count,
            "white_leaf_count": self.white_leaf_count,
            "log_da_pairs": [list(p) for p in self.log_da_pairs],
            "log_fin_ports": list(self.log_fin_ports),
            "genus": 0,
        }


class EulerianSwarmTopology:
    """Maintains a forest of valid genus-0 m-Eulerian trees with Join-Cut and planar closure."""

    def __init__(self, m: int = 3) -> None:
        if m < 2:
            raise ValueError(f"Branching order m must be >= 2, got {m}")
        self.m: int = int(m)
        self.nodes: dict[str, TopologyNode] = {}
        self._seq: int = 0
        self._worker_to_node: dict[str, str] = {}

    def _new(
        self,
        kind: NodeKind,
        *,
        weight: int = 1,
        policy: SharingPolicy | None = None,
        label: str = "",
        owner_worker_id: str | None = None,
    ) -> TopologyNode:
        self._seq += 1
        node = TopologyNode(
            id=f"{kind}_{self._seq:03d}",
            kind=kind,
            weight=weight,
            policy=policy,
            label=label,
            owner_worker_id=owner_worker_id,
        )
        self.nodes[node.id] = node
        if owner_worker_id:
            self._worker_to_node[owner_worker_id] = node.id
        return node

    def _link(self, a_id: str, b_id: str) -> None:
        self.nodes[a_id].neighbors.append(b_id)
        self.nodes[b_id].neighbors.append(a_id)

    def _replace_neighbor(self, node_id: str, old_nb: str, new_nb: str) -> None:
        nbs = self.nodes[node_id].neighbors
        try:
            idx = nbs.index(old_nb)
        except ValueError as exc:
            raise EulerianInvariantError(f"{old_nb} is not a neighbor of {node_id}") from exc
        nbs[idx] = new_nb

    def bind_worker(self, worker_id: str, node_id: str) -> None:
        """Associate a runtime worker/subagent ID with a topology vertex."""
        if node_id not in self.nodes:
            raise KeyError(f"Unknown topology node: {node_id}")
        self.nodes[node_id].owner_worker_id = worker_id
        self._worker_to_node[worker_id] = node_id

    def node_for_worker(self, worker_id: str) -> str | None:
        return self._worker_to_node.get(worker_id)

    # ------------------------------------------------------------------
    # Core m-Eulerian Operators: Create, Expand, Join, Cut, Compact
    # ------------------------------------------------------------------

    def create_sphere(self, *, label: str = "", owner_worker_id: str | None = None) -> str:
        """Create a base i=1 Evaluation Hub with m black leaves (type alpha=(1))."""
        hub = self._new("hub", weight=1, label=label, owner_worker_id=owner_worker_id)
        for _ in range(self.m):
            self._link(hub.id, self._new("b_leaf").id)
        self.assert_invariants()
        return hub.id

    def expand_hub(self, hub_id: str, *, label: str = "", owner_worker_id: str | None = None) -> str:
        """Expand macro-cycle i -> i+1 by attaching 1 inner-deg-1 arbiter + (m-1) black leaves."""
        hub = self.nodes.get(hub_id)
        if hub is None or hub.kind != "hub":
            raise EulerianInvariantError(f"{hub_id!r} is not an inner white hub")
        hub.weight += 1
        arbiter = self._new("arbiter", label=label, owner_worker_id=owner_worker_id)
        self._link(hub.id, arbiter.id)
        for _ in range(self.m - 1):
            self._link(arbiter.id, self._new("w_leaf").id)
        for _ in range(self.m - 1):
            self._link(hub.id, self._new("b_leaf").id)
        self.assert_invariants()
        return arbiter.id

    def join_spheres(
        self,
        leaf_a: str,
        leaf_b: str,
        *,
        policy: SharingPolicy | None = None,
        label: str = "",
        owner_worker_id: str | None = None,
    ) -> str:
        """Eq. 12 Join: fuse two black leaves from distinct spheres into an inner-deg-2 bridge."""
        a = self.nodes.get(leaf_a)
        b = self.nodes.get(leaf_b)
        if a is None or b is None or a.kind != "b_leaf" or b.kind != "b_leaf":
            raise EulerianInvariantError("join_spheres requires two existing black leaves")
        if self._component_of(leaf_a) == self._component_of(leaf_b):
            raise EulerianInvariantError(
                f"Refused join({leaf_a}, {leaf_b}): both leaves belong to the same sphere (would create genus g >= 1)"
            )

        hub_a = a.neighbors[0]
        hub_b = b.neighbors[0]
        bridge = self._new(
            "bridge",
            policy=policy or SharingPolicy(),
            label=label,
            owner_worker_id=owner_worker_id,
        )
        self._replace_neighbor(hub_a, leaf_a, bridge.id)
        self._replace_neighbor(hub_b, leaf_b, bridge.id)

        # Preserve cyclic order around bridge: hub_a, first half of white leaves, hub_b, remaining white leaves
        bridge.neighbors.append(hub_a)
        white_count = self.m - 2
        half = (white_count + 1) // 2
        for _ in range(half):
            self._link(bridge.id, self._new("w_leaf").id)
        bridge.neighbors.append(hub_b)
        for _ in range(half, white_count):
            self._link(bridge.id, self._new("w_leaf").id)

        self._delete_node(leaf_a)
        self._delete_node(leaf_b)
        self.assert_invariants()
        return bridge.id

    def graft_new_hub(
        self,
        target_black_leaf: str,
        *,
        policy: SharingPolicy | None = None,
        hub_label: str = "",
        bridge_label: str = "",
        hub_worker_id: str | None = None,
    ) -> tuple[str, str]:
        """Convenience Join: spawn a fresh i=1 hub and graft it onto target_black_leaf."""
        if target_black_leaf not in self.nodes or self.nodes[target_black_leaf].kind != "b_leaf":
            raise EulerianInvariantError(f"{target_black_leaf!r} is not a black leaf")
        new_hub_id = self.create_sphere(label=hub_label, owner_worker_id=hub_worker_id)
        new_leaf_id = self.nodes[new_hub_id].neighbors[0]
        bridge_id = self.join_spheres(
            target_black_leaf,
            new_leaf_id,
            policy=policy,
            label=bridge_label,
        )
        return new_hub_id, bridge_id

    def cut_bridge(self, bridge_id: str) -> tuple[str, str]:
        """Eq. 12 Cut: sever an inner-deg-2 bridge into two isolated genus-0 spheres."""
        bridge = self.nodes.get(bridge_id)
        if bridge is None or bridge.kind != "bridge":
            raise EulerianInvariantError(f"{bridge_id!r} is not a Join-Cut bridge")
        hub_ids = [nb for nb in bridge.neighbors if self.nodes[nb].kind == "hub"]
        w_leaf_ids = [nb for nb in bridge.neighbors if self.nodes[nb].kind == "w_leaf"]
        if len(hub_ids) != 2:
            raise EulerianInvariantError(f"Bridge {bridge_id} has {len(hub_ids)} hubs, expected 2")

        restored: list[str] = []
        for h_id in hub_ids:
            new_leaf = self._new("b_leaf")
            self._replace_neighbor(h_id, bridge_id, new_leaf.id)
            new_leaf.neighbors.append(h_id)
            restored.append(new_leaf.id)

        for wl_id in w_leaf_ids:
            self._delete_node(wl_id)
        self._delete_node(bridge_id)
        self.assert_invariants()
        return restored[0], restored[1]

    def compact_branch(self, bridge_id: str, keep_hub_id: str) -> str:
        """Fractal compaction (v -> x): cut bridge, discard the other sphere, return restored black leaf on keep_hub."""
        bridge = self.nodes.get(bridge_id)
        if bridge is None or bridge.kind != "bridge":
            raise EulerianInvariantError(f"{bridge_id!r} is not a Join-Cut bridge")
        hub_ids = [nb for nb in bridge.neighbors if self.nodes[nb].kind == "hub"]
        if keep_hub_id not in hub_ids:
            raise EulerianInvariantError(f"Hub {keep_hub_id!r} is not incident to bridge {bridge_id!r}")
        drop_hub_id = hub_ids[1] if hub_ids[0] == keep_hub_id else hub_ids[0]

        leaf_a, leaf_b = self.cut_bridge(bridge_id)
        keep_leaf = leaf_a if self.nodes[leaf_a].neighbors[0] == keep_hub_id else leaf_b
        drop_comp = self._component_of(drop_hub_id)
        for nid in list(drop_comp):
            self._delete_node(nid)
        self.assert_invariants()
        return keep_leaf

    def _delete_node(self, node_id: str) -> None:
        node = self.nodes.pop(node_id, None)
        if node and node.owner_worker_id and self._worker_to_node.get(node.owner_worker_id) == node_id:
            del self._worker_to_node[node.owner_worker_id]

    # ------------------------------------------------------------------
    # Graph Queries, Seam Enforcement & Planar Map Closure
    # ------------------------------------------------------------------

    def _components(self) -> list[list[str]]:
        visited: set[str] = set()
        comps: list[list[str]] = []
        for nid in self.nodes:
            if nid in visited:
                continue
            comp: list[str] = []
            q: deque[str] = deque([nid])
            visited.add(nid)
            while q:
                cur = q.popleft()
                comp.append(cur)
                for nb in self.nodes[cur].neighbors:
                    if nb not in visited:
                        visited.add(nb)
                        q.append(nb)
            comps.append(comp)
        return comps

    def _component_of(self, node_id: str) -> set[str]:
        if node_id not in self.nodes:
            return set()
        visited: set[str] = {node_id}
        q: deque[str] = deque([node_id])
        while q:
            cur = q.popleft()
            for nb in self.nodes[cur].neighbors:
                if nb not in visited:
                    visited.add(nb)
                    q.append(nb)
        return visited

    def tree_path(self, src_id: str, dst_id: str) -> list[str] | None:
        """Return the unique simple path between src_id and dst_id, or None if in different spheres."""
        if src_id not in self.nodes or dst_id not in self.nodes:
            return None
        if src_id == dst_id:
            return [src_id]
        parent: dict[str, str | None] = {src_id: None}
        q: deque[str] = deque([src_id])
        while q:
            cur = q.popleft()
            if cur == dst_id:
                path: list[str] = []
                node: str | None = cur
                while node is not None:
                    path.append(node)
                    node = parent[node]
                path.reverse()
                return path
            for nb in self.nodes[cur].neighbors:
                if nb not in parent:
                    parent[nb] = cur
                    q.append(nb)
        return None

    def can_cross_seam(
        self,
        src: str,
        dst: str,
        artifact_kind: str,
        *,
        payload_chars: int = 0,
    ) -> tuple[bool, str]:
        """Check whether src (node or worker ID) may send artifact_kind to dst across all traversed bridges."""
        src_node = src if src in self.nodes else self._worker_to_node.get(src)
        dst_node = dst if dst in self.nodes else self._worker_to_node.get(dst)
        if src_node is None or dst_node is None:
            return False, "unregistered_endpoint"
        path = self.tree_path(src_node, dst_node)
        if path is None:
            return False, "isolated_spheres"
        for nid in path:
            node = self.nodes[nid]
            if node.kind == "bridge" and node.policy is not None:
                if not node.policy.allows(artifact_kind, payload_chars=payload_chars):
                    return False, f"blocked_by_bridge:{nid}"
        return True, "allowed"

    def close_planar_map(self) -> list[SphereClosure]:
        """Run the Bousquet-Melou-Schaeffer planar contour closure on every connected sphere."""
        closures: list[SphereClosure] = []
        for idx, comp in enumerate(self._components()):
            hubs = [self.nodes[nid] for nid in comp if self.nodes[nid].kind == "hub"]
            if not hubs:
                continue
            start_u = hubs[0].id
            start_nb = self.nodes[start_u].neighbors[0]

            # Counterclockwise Euler contour walk using explicit cyclic neighbor lists
            contour_leaves: list[str] = []
            cur, nxt = start_u, start_nb
            max_steps = len(comp) * 4 + 4
            steps = 0
            while steps < max_steps:
                if self.nodes[cur].kind in ("b_leaf", "w_leaf"):
                    contour_leaves.append(cur)
                nbs = self.nodes[nxt].neighbors
                pos = nbs.index(cur)
                prev_nb = nbs[(pos - 1) % len(nbs)]
                cur, nxt = nxt, prev_nb
                steps += 1
                if cur == start_u and nxt == start_nb:
                    break

            # Stack-based cyclic matching (white leaf -> next unmatched black leaf)
            paired: set[str] = set()
            in_stack: set[str] = set()
            stack: list[str] = []
            pairs: list[tuple[str, str]] = []
            for _pass in range(2):
                for leaf_id in contour_leaves:
                    if leaf_id in paired:
                        continue
                    kind = self.nodes[leaf_id].kind
                    if kind == "w_leaf":
                        if leaf_id not in in_stack:
                            stack.append(leaf_id)
                            in_stack.add(leaf_id)
                    elif kind == "b_leaf" and stack:
                        w_id = stack.pop()
                        in_stack.discard(w_id)
                        paired.add(w_id)
                        paired.add(leaf_id)
                        pairs.append((w_id, leaf_id))

            log_fin = tuple(
                lid for lid in contour_leaves if lid not in paired and self.nodes[lid].kind == "b_leaf"
            )
            alpha = tuple(sorted((h.weight for h in hubs), reverse=True))
            b_count = sum(1 for nid in comp if self.nodes[nid].kind == "b_leaf")
            w_count = sum(1 for nid in comp if self.nodes[nid].kind == "w_leaf")

            closures.append(
                SphereClosure(
                    sphere_index=idx,
                    node_ids=tuple(comp),
                    hub_ids=tuple(h.id for h in hubs),
                    alpha=alpha,
                    n_sum=sum(alpha),
                    black_leaf_count=b_count,
                    white_leaf_count=w_count,
                    log_da_pairs=tuple(pairs),
                    log_fin_ports=log_fin,
                )
            )
        return closures

    def log_fin_ports(self) -> set[str]:
        """Return all currently authorized outer-face LOG_fin promotion port IDs across all spheres."""
        ports: set[str] = set()
        for cl in self.close_planar_map():
            ports.update(cl.log_fin_ports)
        return ports

    def assert_invariants(self) -> None:
        """Verify all genus-0 m-Eulerian topological and Riemann-Hurwitz invariants."""
        for comp in self._components():
            # 1. Tree acyclicity (g = 0): |E| == |V| - 1
            deg_sum = sum(len(self.nodes[nid].neighbors) for nid in comp)
            if deg_sum % 2 != 0 or (deg_sum // 2) != len(comp) - 1:
                raise EulerianInvariantError(
                    f"Genus-0 violation: |V|={len(comp)}, |E|={deg_sum // 2} (expected |V|-1)"
                )

            hubs: list[TopologyNode] = []
            b_leaves = 0
            w_leaves = 0
            for nid in comp:
                node = self.nodes[nid]
                deg = len(node.neighbors)
                if node.kind == "hub":
                    hubs.append(node)
                    if deg != self.m * node.weight:
                        raise EulerianInvariantError(
                            f"Hub {nid} has degree {deg}, expected m*i = {self.m * node.weight}"
                        )
                    arbiter_nbs = sum(1 for nb in node.neighbors if self.nodes[nb].kind == "arbiter")
                    if arbiter_nbs != node.weight - 1:
                        raise EulerianInvariantError(
                            f"Hub {nid} (i={node.weight}) has {arbiter_nbs} arbiters, expected {node.weight - 1}"
                        )
                    for nb in node.neighbors:
                        if self.nodes[nb].kind not in ("arbiter", "bridge", "b_leaf"):
                            raise EulerianInvariantError(f"Bipartite violation: hub {nid} -> {nb}")
                elif node.kind == "arbiter":
                    if deg != self.m:
                        raise EulerianInvariantError(f"Arbiter {nid} has degree {deg}, expected m={self.m}")
                    h_nbs = sum(1 for nb in node.neighbors if self.nodes[nb].kind == "hub")
                    wl_nbs = sum(1 for nb in node.neighbors if self.nodes[nb].kind == "w_leaf")
                    if h_nbs != 1 or wl_nbs != self.m - 1:
                        raise EulerianInvariantError(
                            f"Arbiter {nid} must have 1 hub and {self.m - 1} white leaves"
                        )
                elif node.kind == "bridge":
                    if deg != self.m:
                        raise EulerianInvariantError(f"Bridge {nid} has degree {deg}, expected m={self.m}")
                    h_nbs = sum(1 for nb in node.neighbors if self.nodes[nb].kind == "hub")
                    wl_nbs = sum(1 for nb in node.neighbors if self.nodes[nb].kind == "w_leaf")
                    if h_nbs != 2 or wl_nbs != self.m - 2:
                        raise EulerianInvariantError(
                            f"Bridge {nid} must have 2 hubs and {self.m - 2} white leaves"
                        )
                elif node.kind == "b_leaf":
                    b_leaves += 1
                    if deg != 1 or self.nodes[node.neighbors[0]].kind != "hub":
                        raise EulerianInvariantError(f"Black leaf {nid} must attach to exactly 1 hub")
                elif node.kind == "w_leaf":
                    w_leaves += 1
                    if deg != 1 or self.nodes[node.neighbors[0]].kind not in ("arbiter", "bridge"):
                        raise EulerianInvariantError(f"White leaf {nid} must attach to 1 arbiter or bridge")

            n_sum = sum(h.weight for h in hubs)
            expected_b = (self.m - 1) * n_sum - len(hubs) + 2
            if b_leaves != expected_b:
                raise EulerianInvariantError(
                    f"Riemann-Hurwitz mismatch: B={b_leaves}, expected (m-1)n - l(alpha) + 2 = {expected_b}"
                )
            if (b_leaves - w_leaves) != self.m:
                raise EulerianInvariantError(
                    f"Outer-face port mismatch: B - W = {b_leaves - w_leaves}, expected m = {self.m}"
                )

        # Verify exact m LOG_fin ports per sphere under planar closure
        for cl in self.close_planar_map():
            if len(cl.log_fin_ports) != self.m:
                raise EulerianInvariantError(
                    f"Sphere {cl.sphere_index} closed with {len(cl.log_fin_ports)} LOG_fin ports, expected {self.m}"
                )

    def snapshot(self) -> dict[str, Any]:
        closures = self.close_planar_map()
        return {
            "m": self.m,
            "node_count": len(self.nodes),
            "sphere_count": len(closures),
            "spheres": [cl.to_dict() for cl in closures],
            "bridges": [
                n.to_dict() for n in self.nodes.values() if n.kind == "bridge"
            ],
            "worker_bindings": dict(self._worker_to_node),
        }


class LogFinPromotionGate:
    """Gates speculative LOG_da artifacts so only verified outer-face LOG_fin ports can promote."""

    def __init__(
        self,
        topology: EulerianSwarmTopology,
        checkers: dict[str, Callable[[dict[str, Any]], tuple[bool, str]]] | None = None,
    ) -> None:
        self.topology = topology
        self.checkers: dict[str, Callable[[dict[str, Any]], tuple[bool, str]]] = dict(checkers or {})
        self.speculative: dict[str, dict[str, Any]] = {}
        self.finalized: dict[str, dict[str, Any]] = {}
        self.rejections: dict[str, list[dict[str, Any]]] = {}

    @staticmethod
    def _artifact_id(kind: str, payload: dict[str, Any], slot: str) -> str:
        canonical = json.dumps(
            {"kind": kind, "payload": payload, "slot": slot},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def append_log_da(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        author: str,
        slot: str,
    ) -> str:
        """Record a speculative LOG_da rollout bound to a topology slot (w_leaf, arbiter, or b_leaf)."""
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict")
        if not slot or slot not in self.topology.nodes:
            raise ValueError(f"Unknown topology slot: {slot!r}")
        aid = self._artifact_id(kind, payload, slot)
        if aid not in self.speculative:
            self.speculative[aid] = {
                "artifact_id": aid,
                "kind": kind,
                "payload": payload,
                "author": author,
                "slot": slot,
                "ts": time.time(),
            }
        return aid

    def promote_via_log_fin(self, artifact_id: str, *, port_id: str) -> dict[str, Any]:
        """Promote a speculative LOG_da artifact exclusively through an authorized outer-face LOG_fin port."""
        rec = self.speculative.get(artifact_id)
        if rec is None:
            return {"ok": False, "error": f"Unknown speculative artifact: {artifact_id}"}

        authorized_ports = self.topology.log_fin_ports()
        if port_id not in authorized_ports:
            err = f"Port {port_id!r} is not an active outer-face LOG_fin port"
            self.rejections.setdefault(artifact_id, []).append({"port_id": port_id, "error": err, "ts": time.time()})
            return {"ok": False, "error": err}

        # Verify source slot and promotion port reside in the same connected sphere (no cross-cut leak)
        if self.topology.tree_path(rec["slot"], port_id) is None:
            err = f"Slot {rec['slot']!r} is isolated from LOG_fin port {port_id!r}"
            self.rejections.setdefault(artifact_id, []).append({"port_id": port_id, "error": err, "ts": time.time()})
            return {"ok": False, "error": err}

        checker = self.checkers.get(rec["kind"])
        if checker is None:
            err = f"No checker registered for kind {rec['kind']!r}"
            self.rejections.setdefault(artifact_id, []).append({"port_id": port_id, "error": err, "ts": time.time()})
            return {"ok": False, "error": err}

        try:
            ok, detail = checker(rec["payload"])
        except Exception as exc:
            err = f"Checker raised: {exc}"
            self.rejections.setdefault(artifact_id, []).append({"port_id": port_id, "error": err, "ts": time.time()})
            return {"ok": False, "error": err}

        if not ok:
            self.rejections.setdefault(artifact_id, []).append({"port_id": port_id, "error": detail, "ts": time.time()})
            return {"ok": False, "error": detail}

        fin = {
            **rec,
            "port_id": port_id,
            "detail": detail,
            "finalized_at": time.time(),
        }
        self.finalized[artifact_id] = fin
        return {"ok": True, "record": fin}
