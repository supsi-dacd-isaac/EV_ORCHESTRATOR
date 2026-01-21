import numpy as np
import torch

from .actor import Actor

class Policy:
    def __init__(self, model_path: str, deterministic: bool = True):
        self.actor = Actor(model_path)
        self.deterministic = deterministic

    def compute_action(self, obs: np.ndarray) -> int:
        with torch.no_grad():
            if obs.ndim == 1:
                obs = obs[None, :]  # add batch dim

            obs_tensor = torch.from_numpy(obs)

            logits = self.actor(obs_tensor)

            if self.deterministic:
                action = torch.argmax(logits, dim=-1)
            else:
                probs = torch.distributions.Categorical(logits=logits)
                action = probs.sample()

            return int(action.item())