"""
redteam/graph/dependency_graph.py

Real-time deadlock and starvation detector for multi-core NoC/cache protocols.

Improvements over the original:
- Iterative DFS (no recursion limit — handles thousands of nodes)
- Incremental cycle check (O(V+E) only when a new edge could create a cycle)
- Thread-safe: all mutations hold a lock (safe for asyncio + threaded telemetry)
- Starvation detection: nodes that can never make progress
- Full transaction history with timestamps
- ASCII + structured visualization of detected cycles
"""

import asyncio
import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, Iterator, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────

class RequestType(str, Enum):
    READ_REQ    = "READ_REQ"
    WRITE_REQ   = "WRITE_REQ"
    INVALIDATE  = "INVALIDATE"
    SNOOP_REQ   = "SNOOP_REQ"
    SNOOP_ACK   = "SNOOP_ACK"
    UPGRADE     = "UPGRADE"
    EVICT       = "EVICT"
    WRITEBACK   = "WRITEBACK"


@dataclass
class Transaction:
    src:        str           # e.g. "Core0", "L1Cache_0", "LLC", "DRAM"
    dst:        str
    address:    int
    req_type:   RequestType
    timestamp:  int           # Simulation cycle
    txn_id:     int = 0       # Unique transaction ID

    def to_dict(self) -> Dict[str, Any]:
        return {
            "txn_id":   self.txn_id,
            "src":      self.src,
            "dst":      self.dst,
            "address":  hex(self.address),
            "req_type": self.req_type.value,
            "timestamp": self.timestamp,
        }


@dataclass
class DeadlockEvent:
    cycle_path:   List[str]       # Ordered list of nodes forming the cycle
    timestamp:    int             # When detected
    transactions: List[Transaction]   # Transactions that created this cycle
    severity:     str = "CRITICAL"

    def visualize(self) -> str:
        """ASCII art: Core0 → L1Cache_0 → Core1 → L1Cache_1 → Core0"""
        return " → ".join(self.cycle_path)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cycle":        self.cycle_path,
            "diagram":      self.visualize(),
            "timestamp":    self.timestamp,
            "severity":     self.severity,
            "cycle_length": len(self.cycle_path) - 1,
        }


@dataclass
class StarvationEvent:
    node:            str
    wait_chain:      List[str]   # Path of nodes blocking this node
    wait_depth:      int
    timestamp:       int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node":       self.node,
            "wait_chain": self.wait_chain,
            "wait_depth": self.wait_depth,
            "timestamp":  self.timestamp,
        }


# ─────────────────────────────────────────────
# Incremental Cycle Detector
# ─────────────────────────────────────────────

class _IncrementalCycleDetector:
    """
    Efficient incremental cycle detection using iterative DFS.

    Key property: after adding edge (u, v), a cycle exists iff v can reach u.
    We only need to check reachability from v to u — not a full graph scan.
    This reduces average cost from O(V+E) per insertion to O(reachable nodes).
    """

    def check_creates_cycle(
        self,
        graph: Dict[str, Set[str]],
        new_src: str,
        new_dst: str,
    ) -> Optional[List[str]]:
        """
        Check if adding edge new_src → new_dst creates a cycle.
        Returns the cycle path if one is found, else None.

        Algorithm:
        - If new_dst can reach new_src, then adding new_src→new_dst creates a cycle.
        - The cycle is: new_src → new_dst → ... → new_src
        """
        path = self._iterative_path(graph, start=new_dst, target=new_src)
        if path is not None:
            # Prepend new_src to make cycle: new_src → path[0=new_dst] → ... → new_src
            return [new_src] + path
        return None

    def find_all_cycles(
        self, graph: Dict[str, Set[str]]
    ) -> List[List[str]]:
        """
        Full graph cycle scan using iterative DFS with coloring.
        Use sparingly (only on reset/full analysis) — not on every edge add.

        Returns list of unique cycles (deduplicated by node set).
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        color:   Dict[str, int]  = defaultdict(int)
        cycles:  List[List[str]] = []
        seen_sets: Set[FrozenSet] = set()

        for start in list(graph.keys()):
            if color[start] != WHITE:
                continue

            # Iterative DFS using explicit stack
            # Stack element: (node, iterator_over_neighbors, current_path)
            stack: List[Tuple[str, Iterator, List[str]]] = [
                (start, iter(graph.get(start, [])), [start])
            ]
            color[start] = GRAY

            while stack:
                node, neighbors, path = stack[-1]
                try:
                    nb = next(neighbors)
                    if color[nb] == GRAY:
                        # Back edge found: nb is in current path → cycle
                        try:
                            idx = path.index(nb)
                        except ValueError:
                            idx = 0
                        cycle = path[idx:] + [nb]
                        key   = frozenset(cycle)
                        if key not in seen_sets:
                            seen_sets.add(key)
                            cycles.append(cycle)
                    elif color[nb] == WHITE:
                        color[nb] = GRAY
                        stack.append((nb, iter(graph.get(nb, [])), path + [nb]))
                except StopIteration:
                    color[node] = BLACK
                    stack.pop()

        return cycles

    def _iterative_path(
        self,
        graph:  Dict[str, Set[str]],
        start:  str,
        target: str,
    ) -> Optional[List[str]]:
        """
        BFS to find path from start to target.
        Returns the path [start, ..., target] if reachable, else None.
        """
        if start == target:
            return [start]

        visited: Set[str]              = {start}
        queue:   deque                 = deque([(start, [start])])

        while queue:
            node, path = queue.popleft()
            for nb in graph.get(node, []):
                if nb == target:
                    return path + [nb]
                if nb not in visited:
                    visited.add(nb)
                    queue.append((nb, path + [nb]))
        return None


# ─────────────────────────────────────────────
# Dependency Graph
# ─────────────────────────────────────────────

class DependencyGraph:
    """
    Real-time NoC/cache dependency graph with deadlock and starvation detection.

    Each call to add_transaction() adds a directed edge "src is waiting for dst"
    and immediately checks whether a cycle was created.

    Thread-safe: all mutations and queries hold self._lock.

    Usage:
        graph = DependencyGraph(max_wait_depth=4)
        graph.add_transaction(Transaction("Core0", "Cache0", 0x1000, RequestType.READ_REQ, 100))
        graph.add_transaction(Transaction("Cache0", "Core0", 0x1000, RequestType.SNOOP_ACK, 101))
        # ↑ This creates Core0→Cache0→Core0 — deadlock detected immediately.

        stats = graph.get_stats()
        for dl in graph.deadlocks:
            print(dl.visualize())
    """

    # Bound history to avoid unbounded memory growth
    MAX_TRANSACTION_HISTORY = 100_000
    MAX_DEADLOCK_HISTORY    = 1_000
    MAX_STARVATION_HISTORY  = 1_000

    def __init__(
        self,
        max_wait_depth:     int = 4,
        starvation_enabled: bool = True,
    ):
        self._max_wait_depth     = max_wait_depth
        self._starvation_enabled = starvation_enabled

        # Core graph structure — adj list: node → set of nodes it waits for
        self._graph:   Dict[str, Set[str]] = defaultdict(set)
        self._in_edges: Dict[str, Set[str]] = defaultdict(set)    # reverse edges

        # Transaction log
        self._transactions: List[Transaction] = []
        self._txn_counter: int = 0

        # Detected events
        self._deadlocks:   List[DeadlockEvent]   = []
        self._starvations: List[StarvationEvent] = []

        # Algorithm
        self._detector = _IncrementalCycleDetector()
        self._lock     = threading.Lock()

        logger.info(
            f"DependencyGraph initialised — "
            f"max_wait_depth={max_wait_depth}, "
            f"starvation_check={'enabled' if starvation_enabled else 'disabled'}"
        )

    # ── Public API ────────────────────────────────────────────────────────

    def add_transaction(self, txn: Transaction) -> Optional[DeadlockEvent]:
        """
        Add a "src waits for dst" dependency edge.
        Immediately checks for deadlock after adding.

        Returns:
            DeadlockEvent if this transaction created a deadlock, else None.
        """
        with self._lock:
            self._txn_counter += 1
            txn.txn_id = self._txn_counter

            # Append to history (bounded)
            self._transactions.append(txn)
            if len(self._transactions) > self.MAX_TRANSACTION_HISTORY:
                self._transactions = self._transactions[-self.MAX_TRANSACTION_HISTORY:]

            # Skip self-edges
            if txn.src == txn.dst:
                return None

            # Check if adding this edge creates a cycle BEFORE modifying graph
            cycle = self._detector.check_creates_cycle(
                self._graph, txn.src, txn.dst
            )

            # Add edge to graph
            self._graph[txn.src].add(txn.dst)
            self._in_edges[txn.dst].add(txn.src)

            if cycle:
                event = DeadlockEvent(
                    cycle_path   = cycle,
                    timestamp    = txn.timestamp,
                    transactions = self._get_cycle_transactions(cycle),
                )
                self._deadlocks.append(event)
                if len(self._deadlocks) > self.MAX_DEADLOCK_HISTORY:
                    self._deadlocks = self._deadlocks[-self.MAX_DEADLOCK_HISTORY:]

                logger.critical(
                    f"🚨 DEADLOCK: {event.visualize()} "
                    f"(cycle_len={len(cycle)-1}, t={txn.timestamp})"
                )
                return event

            # Starvation check (only when no deadlock)
            if self._starvation_enabled:
                self._check_starvation(txn.src, txn.timestamp)

            return None

    def remove_transaction(self, src: str, dst: str) -> None:
        """
        Remove a dependency edge (transaction completed or aborted).
        Call this when a request is acknowledged to keep the graph current.
        """
        with self._lock:
            self._graph[src].discard(dst)
            self._in_edges[dst].discard(src)
            # Clean up empty entries
            if not self._graph[src]:
                del self._graph[src]
            if not self._in_edges[dst]:
                del self._in_edges[dst]

    def clear(self) -> None:
        """Reset all state (use between test scenarios)."""
        with self._lock:
            self._graph.clear()
            self._in_edges.clear()
            self._transactions.clear()
            self._deadlocks.clear()
            self._starvations.clear()
            self._txn_counter = 0
        logger.info("DependencyGraph cleared.")

    # ── Query API ─────────────────────────────────────────────────────────

    @property
    def deadlocks(self) -> List[DeadlockEvent]:
        with self._lock:
            return list(self._deadlocks)

    @property
    def starvations(self) -> List[StarvationEvent]:
        with self._lock:
            return list(self._starvations)

    @property
    def transaction_count(self) -> int:
        return self._txn_counter

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            total_nodes = len(self._graph) + len(
                set(self._in_edges.keys()) - set(self._graph.keys())
            )
            total_edges = sum(len(v) for v in self._graph.values())
            max_out = max((len(v) for v in self._graph.values()), default=0)
            max_in  = max((len(v) for v in self._in_edges.values()), default=0)

        return {
            "total_nodes":         total_nodes,
            "total_edges":         total_edges,
            "deadlocks_detected":  len(self._deadlocks),
            "starvations_detected": len(self._starvations),
            "transactions_logged": self._txn_counter,
            "max_out_degree":      max_out,
            "max_in_degree":       max_in,
        }

    def get_waiting_nodes(self) -> List[str]:
        """Return nodes that are currently blocked (have outgoing edges)."""
        with self._lock:
            return list(self._graph.keys())

    def get_node_wait_chain(self, node: str, max_depth: int = 10) -> List[str]:
        """Return the chain of nodes that node is transitively waiting for."""
        chain: List[str] = []
        current = node
        visited: Set[str] = set()

        with self._lock:
            while current in self._graph and len(chain) < max_depth:
                if current in visited:
                    chain.append(f"{current} (CYCLE!)")
                    break
                visited.add(current)
                neighbors = list(self._graph[current])
                if not neighbors:
                    break
                next_node = neighbors[0]   # Follow first dependency
                chain.append(next_node)
                current = next_node

        return chain

    # ── Async monitoring ──────────────────────────────────────────────────

    async def monitor(
        self,
        interval_sec: float = 1.0,
        on_deadlock=None,
        on_starvation=None,
    ) -> None:
        """
        Async loop that periodically logs graph status and fires callbacks.

        Args:
            interval_sec:   How often to check.
            on_deadlock:    Async callback(DeadlockEvent) when new deadlock found.
            on_starvation:  Async callback(StarvationEvent) when starvation found.
        """
        last_dl_count = 0
        last_st_count = 0

        while True:
            await asyncio.sleep(interval_sec)

            dl_list = self.deadlocks
            st_list = self.starvations

            # Fire callbacks for new events only
            for dl in dl_list[last_dl_count:]:
                logger.warning(f"[Monitor] New deadlock: {dl.visualize()}")
                if on_deadlock:
                    try:
                        await on_deadlock(dl)
                    except Exception as e:
                        logger.error(f"on_deadlock callback error: {e}")

            for st in st_list[last_st_count:]:
                logger.warning(f"[Monitor] Starvation: {st.node} (depth={st.wait_depth})")
                if on_starvation:
                    try:
                        await on_starvation(st)
                    except Exception as e:
                        logger.error(f"on_starvation callback error: {e}")

            last_dl_count = len(dl_list)
            last_st_count = len(st_list)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _check_starvation(self, node: str, timestamp: int) -> None:
        """
        Detect if a node is starving: its wait chain exceeds max_wait_depth.
        Must be called inside self._lock.
        """
        depth = 0
        chain: List[str] = []
        current = node
        visited: Set[str] = set()

        while current in self._graph:
            if current in visited or depth >= self._max_wait_depth:
                if depth >= self._max_wait_depth:
                    event = StarvationEvent(
                        node        = node,
                        wait_chain  = chain,
                        wait_depth  = depth,
                        timestamp   = timestamp,
                    )
                    self._starvations.append(event)
                    if len(self._starvations) > self.MAX_STARVATION_HISTORY:
                        self._starvations = self._starvations[-self.MAX_STARVATION_HISTORY:]
                    logger.warning(
                        f"[DependencyGraph] Starvation: "
                        f"{node} → {'→'.join(chain)} (depth={depth})"
                    )
                break
            visited.add(current)
            neighbors = list(self._graph[current])
            if not neighbors:
                break
            next_node = neighbors[0]
            chain.append(next_node)
            current = next_node
            depth += 1

    def _get_cycle_transactions(self, cycle: List[str]) -> List[Transaction]:
        """Retrieve recent transactions whose src/dst appear in the cycle."""
        cycle_nodes = set(cycle)
        relevant: List[Transaction] = []
        for txn in reversed(self._transactions[-200:]):
            if txn.src in cycle_nodes and txn.dst in cycle_nodes:
                relevant.append(txn)
            if len(relevant) >= len(cycle) * 2:
                break
        return list(reversed(relevant))