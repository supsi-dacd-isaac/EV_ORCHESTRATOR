import torch
import torch.nn as nn

class Actor(nn.Module):
    def __init__(self, parameters_path: str):
        super().__init__()
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        parameters_dict = torch.load(parameters_path, map_location=self.device)

        # Infer architecture from state_dict
        layer_shapes = [
            v.shape for k, v in parameters_dict.items() if "weight" in k
        ]

        layers = []
        for i in range(len(layer_shapes) - 1):
            in_dim = layer_shapes[i][1]
            out_dim = layer_shapes[i][0]
            layers.append(nn.Linear(in_dim, out_dim))
            layers.append(nn.ReLU())

        self.network = nn.Sequential(*layers)
        self.actor = nn.Linear(layer_shapes[-2][0], layer_shapes[-1][0])

        self.load_state_dict(parameters_dict)
        self.to(self.device)
        self.eval()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x.to(self.device).float()
        hidden = self.network(x)
        return self.actor(hidden)
