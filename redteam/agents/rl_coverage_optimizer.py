"""
redteam/agents/rl_coverage_optimizer.py

Deep Q-Network (DQN) agent that learns which test actions maximise
coverage gain fastest.  Designed to be the "brain" that replaces random
test selection with a data-driven policy after enough experience.

Architecture: DQN with experience replay + target network (stable training).
Falls back to epsilon-greedy random when PyTorch is not installed.

Dependencies:
    pip install torch   (CPU build: pip install torch --index-url https://download.pytorch.org/whl/cpu)
"""

import json
import logging
import os
import random
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Optional PyTorch
# ─────────────────────────────────────────────
try:
    import torch
    import torch.nn as nn
    import torch.optim as optim
    import torch.nn.functional as F
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False
    logger.warning(
        "PyTorch not installed — RL agent running in random-baseline mode. "
        "Install: pip install torch"
    )


# ─────────────────────────────────────────────
# Action catalogue
# ─────────────────────────────────────────────

TEST_ACTIONS: List[str] = [
    "directed_alu_ops",
    "branch_stress",
    "load_store_unaligned",
    "csr_read_write",
    "interrupt_injection",
    "pipeline_hazards",
    "cache_thrash",
    "illegal_opcode",
    "mret_ecall_stress",
    "atomic_amo_ops",
]

# Map action name → integer index
ACTION_INDEX: Dict[str, int] = {a: i for i, a in enumerate(TEST_ACTIONS)}
NUM_ACTIONS = len(TEST_ACTIONS)

# State feature names (must match what encode_state() produces)
STATE_FEATURES: List[str] = [
    "line_coverage",
    "toggle_coverage",
    "branch_coverage",
    "functional_coverage",
    "overall_coverage",
    "delta_overall",             # Change from last run
    "bugs_found_total",
    "runs_completed",
    "stagnant_runs",             # Consecutive runs with < 0.5% gain
    # Padding to reach STATE_DIM
    *[f"pad_{i}" for i in range(118)],
]
STATE_DIM = len(STATE_FEATURES)     # 128


# ─────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────

@dataclass
class Transition:
    """One (s, a, r, s', done) tuple stored in the replay buffer."""
    state:      List[float]
    action:     int
    reward:     float
    next_state: List[float]
    done:       bool


@dataclass
class CoverageState:
    """Structured input from the CoverageDirector."""
    line_coverage:       float = 0.0
    toggle_coverage:     float = 0.0
    branch_coverage:     float = 0.0
    functional_coverage: float = 0.0
    overall_coverage:    float = 0.0
    delta_overall:       float = 0.0
    bugs_found_total:    int   = 0
    runs_completed:      int   = 0
    stagnant_runs:       int   = 0


def encode_state(cs: CoverageState) -> List[float]:
    """Normalise a CoverageState into a fixed-length float vector."""
    raw = [
        cs.line_coverage       / 100.0,
        cs.toggle_coverage     / 100.0,
        cs.branch_coverage     / 100.0,
        cs.functional_coverage / 100.0,
        cs.overall_coverage    / 100.0,
        max(-1.0, min(1.0, cs.delta_overall / 10.0)),   # clamp delta to [-1, 1]
        min(1.0, cs.bugs_found_total / 100.0),
        min(1.0, cs.runs_completed   / 500.0),
        min(1.0, cs.stagnant_runs    / 20.0),
    ]
    # Pad to STATE_DIM
    raw += [0.0] * (STATE_DIM - len(raw))
    return raw


def compute_reward(
    coverage_before: float,
    coverage_after:  float,
    bug_found:       bool,
) -> float:
    """
    Reward function:
    - +10 per 1% coverage gain (scaled)
    - +50 bonus if a new bug was found
    - -1 penalty for stagnation (no gain)
    """
    delta = coverage_after - coverage_before
    reward = delta * 10.0
    if bug_found:
        reward += 50.0
    if delta <= 0.01:
        reward -= 1.0
    return reward


# ─────────────────────────────────────────────
# Neural Network
# ─────────────────────────────────────────────

if TORCH_AVAILABLE:
    class _DQNNetwork(nn.Module):
        """
        Dueling DQN architecture:
        - Shared feature extractor
        - Separate value and advantage streams
        - Combines to Q(s,a) = V(s) + A(s,a) - mean(A)

        This reduces variance compared to vanilla DQN,
        especially when many actions have similar Q-values.
        """

        def __init__(self, state_dim: int, action_dim: int, hidden: int = 256):
            super().__init__()
            self.feature = nn.Sequential(
                nn.Linear(state_dim, hidden),
                nn.LayerNorm(hidden),
                nn.ReLU(),
                nn.Linear(hidden, hidden),
                nn.LayerNorm(hidden),
                nn.ReLU(),
            )
            # Value stream
            self.value_stream = nn.Sequential(
                nn.Linear(hidden, 128),
                nn.ReLU(),
                nn.Linear(128, 1),
            )
            # Advantage stream
            self.advantage_stream = nn.Sequential(
                nn.Linear(hidden, 128),
                nn.ReLU(),
                nn.Linear(128, action_dim),
            )

        def forward(self, x: "torch.Tensor") -> "torch.Tensor":
            features   = self.feature(x)
            value      = self.value_stream(features)
            advantage  = self.advantage_stream(features)
            # Dueling combination
            q_values = value + (advantage - advantage.mean(dim=1, keepdim=True))
            return q_values


# ─────────────────────────────────────────────
# Replay Buffer
# ─────────────────────────────────────────────

class ReplayBuffer:
    """Fixed-size circular replay buffer with uniform random sampling."""

    def __init__(self, capacity: int = 10_000):
        self._buf: deque = deque(maxlen=capacity)

    def push(self, t: Transition) -> None:
        self._buf.append(t)

    def sample(self, batch_size: int) -> List[Transition]:
        return random.sample(self._buf, min(batch_size, len(self._buf)))

    def __len__(self) -> int:
        return len(self._buf)


# ─────────────────────────────────────────────
# RL Coverage Optimizer
# ─────────────────────────────────────────────

class CoverageRLAgent:
    """
    DQN agent that selects which test action to run next to maximise
    coverage gain.  Trains online from experience after each simulation run.

    Usage:
        agent = CoverageRLAgent()
        action_name = agent.select_action(coverage_state)
        # ... run simulation with that action ...
        agent.store_transition(state_before, action_name, reward, state_after, done=False)
        agent.train_step()            # call after each simulation
        agent.save("checkpoint.pt")   # persist
    """

    # ── Hyperparameters ───────────────────────────────────────────────────
    GAMMA        = 0.99     # Discount factor
    LR           = 3e-4     # Adam learning rate
    BATCH_SIZE   = 64
    TARGET_SYNC  = 50       # Steps between target-network updates
    EPS_START    = 1.0      # Initial exploration rate
    EPS_END      = 0.05     # Minimum exploration rate
    EPS_DECAY    = 0.995    # Per-step multiplicative decay

    def __init__(
        self,
        state_dim:     int = STATE_DIM,
        action_dim:    int = NUM_ACTIONS,
        buffer_cap:    int = 10_000,
        checkpoint_dir: str = "checkpoints",
    ):
        self.state_dim      = state_dim
        self.action_dim     = action_dim
        self.epsilon        = self.EPS_START
        self.step_count     = 0
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

        self.replay_buffer  = ReplayBuffer(capacity=buffer_cap)

        if TORCH_AVAILABLE:
            self.device      = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            self.policy_net  = _DQNNetwork(state_dim, action_dim).to(self.device)
            self.target_net  = _DQNNetwork(state_dim, action_dim).to(self.device)
            self.target_net.load_state_dict(self.policy_net.state_dict())
            self.target_net.eval()
            self.optimizer   = optim.Adam(self.policy_net.parameters(), lr=self.LR)
            logger.info(
                f"CoverageRLAgent initialised on {self.device} "
                f"(state_dim={state_dim}, action_dim={action_dim})"
            )
        else:
            self.policy_net = None
            logger.warning("CoverageRLAgent running WITHOUT PyTorch — random baseline only.")

    # ── Public API ────────────────────────────────────────────────────────

    def select_action(self, state: CoverageState) -> str:
        """
        Choose the next test action.
        Epsilon-greedy: explore randomly or exploit the current policy.

        Returns:
            Action name string (from TEST_ACTIONS).
        """
        self.step_count += 1
        self.epsilon = max(self.EPS_END, self.epsilon * self.EPS_DECAY)

        # Always random if no PyTorch
        if not TORCH_AVAILABLE or self.policy_net is None:
            return random.choice(TEST_ACTIONS)

        # Epsilon-greedy
        if random.random() < self.epsilon:
            action_idx = random.randrange(self.action_dim)
        else:
            state_vec = encode_state(state)
            with torch.no_grad():
                t = torch.tensor([state_vec], dtype=torch.float32, device=self.device)
                q_vals     = self.policy_net(t)
                action_idx = int(q_vals.argmax(dim=1).item())

        return TEST_ACTIONS[action_idx]

    def store_transition(
        self,
        state_before:    CoverageState,
        action:          str,
        reward:          float,
        state_after:     CoverageState,
        done:            bool = False,
    ) -> None:
        """Push one transition into the replay buffer."""
        action_idx = ACTION_INDEX.get(action, 0)
        self.replay_buffer.push(Transition(
            state      = encode_state(state_before),
            action     = action_idx,
            reward     = reward,
            next_state = encode_state(state_after),
            done       = done,
        ))

    def train_step(self) -> Optional[float]:
        """
        Sample a mini-batch from the replay buffer and update the policy network.

        Returns:
            Loss value (float) if a training step occurred, None otherwise.
        """
        if not TORCH_AVAILABLE or len(self.replay_buffer) < self.BATCH_SIZE:
            return None

        batch      = self.replay_buffer.sample(self.BATCH_SIZE)
        states     = torch.tensor([t.state      for t in batch], dtype=torch.float32, device=self.device)
        actions    = torch.tensor([t.action     for t in batch], dtype=torch.long,    device=self.device)
        rewards    = torch.tensor([t.reward     for t in batch], dtype=torch.float32, device=self.device)
        next_states= torch.tensor([t.next_state for t in batch], dtype=torch.float32, device=self.device)
        dones      = torch.tensor([t.done       for t in batch], dtype=torch.float32, device=self.device)

        # Q(s, a) for taken actions
        q_current = self.policy_net(states).gather(1, actions.unsqueeze(1)).squeeze(1)

        # Double DQN: use policy net to SELECT action, target net to EVALUATE it
        with torch.no_grad():
            next_actions = self.policy_net(next_states).argmax(dim=1)
            q_next       = self.target_net(next_states).gather(1, next_actions.unsqueeze(1)).squeeze(1)
            q_target     = rewards + self.GAMMA * q_next * (1.0 - dones)

        loss = F.smooth_l1_loss(q_current, q_target)

        self.optimizer.zero_grad()
        loss.backward()
        # Gradient clipping prevents exploding gradients
        nn.utils.clip_grad_norm_(self.policy_net.parameters(), max_norm=10.0)
        self.optimizer.step()

        # Periodically sync target network
        if self.step_count % self.TARGET_SYNC == 0:
            self.target_net.load_state_dict(self.policy_net.state_dict())
            logger.debug(f"Target network synced at step {self.step_count}")

        return float(loss.item())

    def save(self, filename: Optional[str] = None) -> str:
        """Save model weights and training state to disk."""
        if not TORCH_AVAILABLE:
            logger.warning("Cannot save — PyTorch not available.")
            return ""
        path = self.checkpoint_dir / (filename or f"dqn_step_{self.step_count}.pt")
        torch.save({
            "step_count":       self.step_count,
            "epsilon":          self.epsilon,
            "policy_state":     self.policy_net.state_dict(),
            "target_state":     self.target_net.state_dict(),
            "optimizer_state":  self.optimizer.state_dict(),
        }, str(path))
        logger.info(f"RL checkpoint saved: {path}")
        return str(path)

    def load(self, path: str) -> bool:
        """Load model weights and training state from disk."""
        if not TORCH_AVAILABLE:
            logger.warning("Cannot load — PyTorch not available.")
            return False
        p = Path(path)
        if not p.exists():
            logger.error(f"Checkpoint not found: {path}")
            return False
        checkpoint = torch.load(str(p), map_location=self.device)
        self.policy_net.load_state_dict(checkpoint["policy_state"])
        self.target_net.load_state_dict(checkpoint["target_state"])
        self.optimizer.load_state_dict(checkpoint["optimizer_state"])
        self.step_count = checkpoint.get("step_count", 0)
        self.epsilon    = checkpoint.get("epsilon", self.EPS_END)
        logger.info(f"RL checkpoint loaded from {path} (step={self.step_count})")
        return True

    def stats(self) -> Dict[str, Any]:
        return {
            "step_count":       self.step_count,
            "epsilon":          round(self.epsilon, 4),
            "buffer_size":      len(self.replay_buffer),
            "torch_available":  TORCH_AVAILABLE,
            "device":           str(getattr(self, "device", "cpu")),
        }