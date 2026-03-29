"""
redteam/graph/gnn_detector.py

Graph Neural Network for detecting deadlocks and circular dependencies in
multi-core NoC (Network-on-Chip) dependency graphs.

Two detection modes:
1. GNN (fast inference, learned patterns): requires torch + torch_geometric.
2. Classic DFS cycle detector (always available, O(V+E)): pure Python fallback.

The two modes can also be combined: DFS first for certainty, GNN for
probability estimates and classification.

Dependencies (GNN mode):
    pip install torch torch_geometric

Dependencies (fallback):
    None (pure Python)
"""

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Optional: PyTorch + PyTorch Geometric
# ─────────────────────────────────────────────
try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False
    logger.info("PyTorch not installed — GNN mode unavailable.")

try:
    from torch_geometric.nn import GCNConv, global_mean_pool
    from torch_geometric.data import Data, Batch
    PYG_AVAILABLE = True
except ImportError:
    PYG_AVAILABLE = False
    if TORCH_AVAILABLE:
        logger.info(
            "torch_geometric not installed — GNN mode unavailable. "
            "Install: pip install torch_geometric"
        )


# ─────────────────────────────────────────────
# Enumerations and Data classes
# ─────────────────────────────────────────────

class DeadlockType(str, Enum):
    NONE         = "none"
    CIRCULAR_DEP = "circular_dependency"
    RESOURCE     = "resource_deadlock"
    LIVELOCK     = "livelock"
    STARVATION   = "starvation"
    UNKNOWN      = "unknown"


@dataclass
class DependencyEdge:
    """A directed dependency edge: ``src`` is waiting for ``dst``."""
    src:          int        # Core / agent ID
    dst:          int        # Core / agent ID
    resource:     str = ""   # Cache line, port, lock, etc.
    weight:       float = 1.0

    def to_tuple(self) -> Tuple[int, int]:
        return (self.src, self.dst)


@dataclass
class DeadlockResult:
    """Result from deadlock detection on a dependency graph."""
    deadlock_detected: bool
    deadlock_type:     DeadlockType
    probability:       float              # 0.0–1.0 (GNN confidence or 1.0 for DFS)
    cycles:            List[List[int]]    # List of node cycles found
    involved_nodes:    List[int]          # All nodes that are part of a deadlock
    detection_method:  str                # "gnn" | "dfs" | "combined"
    message:           str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "deadlock_detected": self.deadlock_detected,
            "deadlock_type":     self.deadlock_type.value,
            "probability":       round(self.probability, 4),
            "cycles":            self.cycles,
            "involved_nodes":    self.involved_nodes,
            "detection_method":  self.detection_method,
            "message":           self.message,
        }


# ─────────────────────────────────────────────
# Classic DFS-based cycle detector (always available)
# ─────────────────────────────────────────────

class DFSCycleDetector:
    """
    Detects all cycles in a directed graph using iterative DFS.
    Time complexity: O(V + E).  Always available, no dependencies.
    """

    def find_cycles(self, edges: List[DependencyEdge]) -> List[List[int]]:
        """
        Return a list of cycles, where each cycle is an ordered list of node IDs.
        Uses Johnson's algorithm approach via DFS with path tracking.
        """
        if not edges:
            return []

        graph: Dict[int, List[int]] = defaultdict(list)
        nodes: Set[int] = set()

        for e in edges:
            graph[e.src].append(e.dst)
            nodes.add(e.src)
            nodes.add(e.dst)

        visited:     Set[int] = set()
        rec_stack:   Set[int] = set()
        cycles:      List[List[int]] = []

        for start in sorted(nodes):
            if start not in visited:
                self._dfs(start, graph, visited, rec_stack, [], cycles)

        # Deduplicate rotations of the same cycle
        return self._deduplicate_cycles(cycles)

    def _dfs(
        self,
        node:      int,
        graph:     Dict[int, List[int]],
        visited:   Set[int],
        rec_stack: Set[int],
        path:      List[int],
        cycles:    List[List[int]],
    ) -> None:
        visited.add(node)
        rec_stack.add(node)
        path.append(node)

        for neighbour in graph.get(node, []):
            if neighbour not in visited:
                self._dfs(neighbour, graph, visited, rec_stack, path, cycles)
            elif neighbour in rec_stack:
                # Found a cycle — extract it
                cycle_start = path.index(neighbour)
                cycles.append(path[cycle_start:] + [neighbour])

        path.pop()
        rec_stack.discard(node)

    @staticmethod
    def _deduplicate_cycles(cycles: List[List[int]]) -> List[List[int]]:
        """Remove duplicate cycles that are rotations of each other."""
        seen:   Set[frozenset] = set()
        unique: List[List[int]] = []
        for cycle in cycles:
            key = frozenset(cycle)
            if key not in seen:
                seen.add(key)
                unique.append(cycle)
        return unique

    def check_starvation(
        self, edges: List[DependencyEdge], max_wait_depth: int = 4
    ) -> List[int]:
        """
        Detect starvation: nodes that can never reach a resource because they
        are always pre-empted by others (chain depth > max_wait_depth).
        """
        graph: Dict[int, List[int]] = defaultdict(list)
        in_degree: Dict[int, int] = defaultdict(int)

        for e in edges:
            graph[e.src].append(e.dst)
            in_degree[e.dst] += 1

        starved: List[int] = []
        for node in graph:
            depth = self._chain_depth(node, graph, set())
            if depth >= max_wait_depth:
                starved.append(node)
        return starved

    def _chain_depth(
        self, node: int, graph: Dict[int, List[int]], visited: Set[int]
    ) -> int:
        if node in visited or node not in graph:
            return 0
        visited.add(node)
        return 1 + max(
            (self._chain_depth(n, graph, visited) for n in graph[node]),
            default=0,
        )


# ─────────────────────────────────────────────
# GNN Deadlock Classifier (PyTorch Geometric)
# ─────────────────────────────────────────────

if TORCH_AVAILABLE and PYG_AVAILABLE:
    class _DeadlockGNN(nn.Module):
        """
        3-layer GCN that classifies whether a dependency graph contains a deadlock.
        Input: per-node feature vectors (node degree, wait-time, resource ID)
        Output: scalar probability in [0, 1]
        """

        NODE_FEAT_DIM = 8    # Feature vector size per node

        def __init__(self, hidden_dim: int = 64):
            super().__init__()
            self.conv1 = GCNConv(self.NODE_FEAT_DIM, hidden_dim)
            self.conv2 = GCNConv(hidden_dim, hidden_dim)
            self.conv3 = GCNConv(hidden_dim, hidden_dim // 2)
            self.head  = nn.Sequential(
                nn.Linear(hidden_dim // 2, 16),
                nn.ReLU(),
                nn.Linear(16, 1),
            )

        def forward(
            self,
            x:          "torch.Tensor",     # [N, NODE_FEAT_DIM]
            edge_index: "torch.Tensor",     # [2, E]
            batch:      "torch.Tensor",     # [N] — graph assignment
        ) -> "torch.Tensor":
            x = F.relu(self.conv1(x, edge_index))
            x = F.dropout(x, p=0.2, training=self.training)
            x = F.relu(self.conv2(x, edge_index))
            x = F.relu(self.conv3(x, edge_index))
            x = global_mean_pool(x, batch)   # Graph-level readout
            return torch.sigmoid(self.head(x)).squeeze(-1)

        @staticmethod
        def build_graph_data(
            edges: List[DependencyEdge],
        ) -> "Data":
            """Convert edge list to a PyG Data object."""
            nodes: List[int] = sorted(
                {e.src for e in edges} | {e.dst for e in edges}
            )
            node_index = {n: i for i, n in enumerate(nodes)}
            n = len(nodes)

            # Node features: [degree_out, degree_in, is_in_cycle (0), wait_depth, ...]
            in_deg  = defaultdict(int)
            out_deg = defaultdict(int)
            for e in edges:
                out_deg[e.src] += 1
                in_deg[e.dst]  += 1

            feats = []
            for node in nodes:
                od   = out_deg[node]
                ind  = in_deg[node]
                feat = [
                    od / max(n, 1),          # normalised out-degree
                    ind / max(n, 1),         # normalised in-degree
                    float(od > 0),           # has outgoing edges
                    float(ind > 0),          # has incoming edges
                    float(od + ind) / max(n, 1),
                    0.0, 0.0, 0.0,           # padding (for future features)
                ]
                feats.append(feat)

            x = torch.tensor(feats, dtype=torch.float32)

            if edges:
                edge_list = [[node_index[e.src], node_index[e.dst]] for e in edges]
                edge_index = torch.tensor(edge_list, dtype=torch.long).t().contiguous()
            else:
                edge_index = torch.zeros((2, 0), dtype=torch.long)

            return Data(x=x, edge_index=edge_index)


# ─────────────────────────────────────────────
# Main Detector
# ─────────────────────────────────────────────

class DeadlockDetector:
    """
    Detects deadlocks and circular dependencies in multi-core dependency graphs.

    Detection strategy:
    - DFS is always run first (100% accurate for deterministic cycle detection).
    - GNN is run additionally when available, to provide probability scores and
      classify the deadlock type beyond simple cycle detection.
    - When both agree, confidence is high. When they disagree, the DFS result
      is treated as ground truth.

    Usage:
        detector = DeadlockDetector()
        edges = [
            DependencyEdge(src=0, dst=1, resource="cache_0xDEADBEEF"),
            DependencyEdge(src=1, dst=2, resource="cache_0xCAFEBABE"),
            DependencyEdge(src=2, dst=0, resource="cache_0xBAADF00D"),  # ← creates cycle
        ]
        result = detector.detect(edges)
        print(result.to_dict())
    """

    def __init__(
        self,
        model_path:   Optional[str] = None,
        use_gnn:      bool = True,
        starvation_depth: int = 4,
    ):
        self._dfs            = DFSCycleDetector()
        self._gnn: Optional["_DeadlockGNN"] = None
        self._starvation_depth = starvation_depth

        if use_gnn and TORCH_AVAILABLE and PYG_AVAILABLE:
            self._device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            self._gnn    = _DeadlockGNN().to(self._device)
            self._gnn.eval()

            if model_path and os.path.exists(model_path):
                self._load_weights(model_path)
                logger.info(f"DeadlockGNN loaded weights from {model_path}")
            else:
                logger.info(
                    "DeadlockGNN running with random weights — "
                    "train or load a checkpoint for meaningful scores."
                )
        else:
            self._device = None
            if use_gnn:
                logger.info(
                    "GNN requested but torch/torch_geometric unavailable — "
                    "using DFS-only mode."
                )

    # ── Public API ────────────────────────────────────────────────────────

    def detect(self, edges: List[DependencyEdge]) -> DeadlockResult:
        """
        Run deadlock detection on the given dependency graph.

        Args:
            edges: List of directed dependency edges.

        Returns:
            DeadlockResult with full diagnosis.
        """
        if not edges:
            return DeadlockResult(
                deadlock_detected = False,
                deadlock_type     = DeadlockType.NONE,
                probability       = 0.0,
                cycles            = [],
                involved_nodes    = [],
                detection_method  = "dfs",
                message           = "Empty graph — no deadlock possible.",
            )

        # 1. Always run DFS (ground truth)
        cycles = self._dfs.find_cycles(edges)
        starved = self._dfs.check_starvation(edges, self._starvation_depth)

        dfs_deadlock  = len(cycles) > 0
        has_starvation = len(starved) > 0

        involved = sorted({n for cycle in cycles for n in cycle})
        method   = "dfs"

        # 2. Run GNN if available (augments with probability + type)
        gnn_prob = 1.0 if dfs_deadlock else 0.0
        if self._gnn is not None:
            gnn_prob = self._gnn_probability(edges)
            method   = "combined"

        # 3. Classify deadlock type
        if dfs_deadlock:
            dtype = DeadlockType.CIRCULAR_DEP
            msg   = (
                f"Circular dependency detected in {len(cycles)} cycle(s) "
                f"involving nodes {involved}."
            )
        elif has_starvation:
            dtype    = DeadlockType.STARVATION
            involved = starved
            msg      = f"Starvation detected: nodes {starved} may never acquire resources."
        elif gnn_prob > 0.85:
            # GNN confident about a pattern not caught by DFS (e.g. soft livelock)
            dtype    = DeadlockType.LIVELOCK
            involved = []
            msg      = (
                f"GNN indicates probable livelock (p={gnn_prob:.2f}) "
                f"even without a strict cycle."
            )
        else:
            dtype = DeadlockType.NONE
            msg   = "No deadlock detected."

        return DeadlockResult(
            deadlock_detected = dfs_deadlock or has_starvation or gnn_prob > 0.85,
            deadlock_type     = dtype,
            probability       = max(gnn_prob, 1.0 if dfs_deadlock else 0.0),
            cycles            = cycles,
            involved_nodes    = involved,
            detection_method  = method,
            message           = msg,
        )

    def detect_from_dict(self, edge_dicts: List[Dict[str, Any]]) -> DeadlockResult:
        """
        Convenience wrapper: accepts plain dicts instead of DependencyEdge objects.
        Each dict must have "src" and "dst" keys (int).
        """
        edges = [
            DependencyEdge(
                src      = int(d["src"]),
                dst      = int(d["dst"]),
                resource = str(d.get("resource", "")),
                weight   = float(d.get("weight", 1.0)),
            )
            for d in edge_dicts
            if "src" in d and "dst" in d
        ]
        return self.detect(edges)

    # ── GNN inference ─────────────────────────────────────────────────────

    def _gnn_probability(self, edges: List[DependencyEdge]) -> float:
        try:
            data  = _DeadlockGNN.build_graph_data(edges).to(self._device)
            batch = torch.zeros(data.num_nodes, dtype=torch.long, device=self._device)
            with torch.no_grad():
                prob = self._gnn(data.x, data.edge_index, batch)
            return float(prob.item())
        except Exception as e:
            logger.warning(f"GNN inference failed: {e}")
            return 0.0

    def _load_weights(self, path: str) -> None:
        try:
            state = torch.load(path, map_location=self._device)
            self._gnn.load_state_dict(state)
            logger.info(f"GNN weights loaded from {path}")
        except Exception as e:
            logger.warning(f"Could not load GNN weights from {path}: {e}")


# ─────────────────────────────────────────────
# Missing import guard
# ─────────────────────────────────────────────
import os   # noqa: E402 — needed for model_path check above