"""Runs inside a disposable no-network container. Never receives held-out labels."""
import base64
import io
import json
import sys
import torch
from torch import nn
from torch.nn import functional as F
from transformers import MambaConfig, MambaModel

torch.set_num_threads(2)
request = json.load(sys.stdin)
scope = {'torch': torch, 'nn': nn, 'F': F}
exec(compile(request['source'], '<candidate>', 'exec'), scope)


class Model(nn.Module):
    def __init__(self):
        super().__init__()
        self.embed = nn.Embedding(10, 24)
        # Real small Mamba backbone, trained from scratch in each matched arm.
        self.mamba = MambaModel(MambaConfig(vocab_size=10, hidden_size=24, state_size=8,
                                num_hidden_layers=1, expand=2, conv_kernel=4, use_cache=False))
        self.head = nn.Linear(24, 8)
        self.layer = scope['CandidateLayer'](24)
        count = sum(p.numel() for p in self.parameters())
        if not 0 < count <= 1000000:
            raise ValueError('Parameter count outside allowed range')

    def forward(self, x):
        x = self.embed(x)
        x = self.mamba(inputs_embeds=x, use_cache=False).last_hidden_state
        y = self.layer(x)
        if not torch.is_tensor(y) or y.shape != x.shape or not torch.isfinite(y).all():
            raise ValueError('Expected finite [batch, sequence, width]')
        return self.head(y[:, -1, :])


torch.manual_seed(7301)
m = Model()
if request['mode'] == 'train':
    opt = torch.optim.Adam(m.parameters(), lr=0.003)
    for examples in request['batches']:
        x = torch.tensor([row['x'] for row in examples], dtype=torch.long)
        y = torch.tensor([row['y'] for row in examples], dtype=torch.long)
        opt.zero_grad(set_to_none=True)
        loss = F.cross_entropy(m(x), y)
        if not torch.isfinite(loss):
            raise ValueError('Non-finite loss')
        loss.backward()
        nn.utils.clip_grad_norm_(m.parameters(), 1.0)
        opt.step()
    buffer = io.BytesIO()
    torch.save(m.state_dict(), buffer)
    output = {'checkpoint': base64.b64encode(buffer.getvalue()).decode(),
              'parameters': sum(p.numel() for p in m.parameters())}
elif request['mode'] == 'predict':
    if request['checkpoint'] is not None:
        weights = base64.b64decode(request['checkpoint'], validate=True)
        m.load_state_dict(torch.load(io.BytesIO(weights), map_location='cpu', weights_only=True))
    m.eval()
    with torch.no_grad():
        x = torch.tensor(request['inputs'], dtype=torch.long)
        pred = m(x).argmax(dim=-1).tolist()
    output = {'predictions': pred}
else:
    raise ValueError('Unsupported sandbox operation')
print('LAYER_RESPONSE=' + json.dumps(output, allow_nan=False))
