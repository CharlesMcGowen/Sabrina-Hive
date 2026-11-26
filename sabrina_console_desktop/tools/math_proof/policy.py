"""Only exported tensors control action ranking. No language model is called."""
from pathlib import Path
import hashlib

import torch
from safetensors.torch import load_file, save_file
from torch import nn

from .common import digest, load, save
from .goals import ACTIONS, FEATURES

SHAPES = {"up": [16, 18], "up_bias": [16], "down": [6, 16], "down_bias": [6], "decay": [6]}


class ProofPolicy(nn.Module):
    def __init__(self, weights):
        super().__init__()
        if set(weights) != set(SHAPES):
            raise ValueError("Unexpected policy tensors")
        for name, shape in SHAPES.items():
            tensor = weights[name]
            if list(tensor.shape) != shape or not torch.isfinite(tensor).all() or tensor.abs().max() > 100:
                raise ValueError(f"Invalid policy tensor {name}")
            self.register_buffer(name, tensor.to(dtype=torch.float32))
        if not ((0 <= self.decay).all() and (self.decay <= 1).all()):
            raise ValueError("History decay must lie in [0,1]")

    def forward(self, goal_features, history_state=None):
        if goal_features.shape[-1] != len(FEATURES):
            raise ValueError("Expected twelve goal features")
        if history_state is None:
            history_state = goal_features.new_zeros((*goal_features.shape[:-1], len(ACTIONS)))
        combined = torch.cat((goal_features, history_state), dim=-1)
        logits = torch.relu(combined @ self.up.T + self.up_bias) @ self.down.T + self.down_bias
        return logits, history_state * self.decay

    @classmethod
    def load(cls, path):
        path = Path(path)
        manifest = load(path.parent / "manifest.json")
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["checkpoint_sha256"]:
            raise ValueError("Checkpoint hash differs from its manifest")
        return cls(load_file(str(path))).eval()

    def rank(self, feature_values, state):
        with torch.inference_mode():
            logits, next_state = self(torch.tensor(feature_values, dtype=torch.float32), state)
        return sorted(range(6), key=lambda i: (-float(logits[i]), i)), next_state


def export(weights, output, provenance):
    if set(weights) != set(SHAPES):
        raise ValueError("Generator must return the five specified tensors")
    tensors = {k: torch.tensor(v, dtype=torch.float32) for k, v in weights.items()}
    policy = ProofPolicy(tensors)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    save_file(dict(policy.named_buffers()), str(output / ".policy.pending"))
    (output / ".policy.pending").replace(output / "policy.safetensors")
    save(output / "manifest.json", dict(schema="printed-proof-policy-v1", features=FEATURES,
         actions=ACTIONS, shapes=SHAPES, generator_output_sha256=digest(weights), provenance=provenance,
         checkpoint_sha256=hashlib.sha256((output / "policy.safetensors").read_bytes()).hexdigest(),
         training_steps=0, parameters=sum(t.numel() for t in tensors.values())))
    return policy
