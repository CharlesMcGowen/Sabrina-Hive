"""Isolated candidate projection experiments inside an actual Hugging Face Mamba block."""
import base64
import copy
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
        self.mamba = MambaModel(MambaConfig(vocab_size=10, hidden_size=24, state_size=8,
                                num_hidden_layers=1, expand=2, conv_kernel=4, use_cache=False))
        mixer = self.mamba.layers[0].mixer
        original = mixer.x_proj
        if original.in_features != 48 or original.out_features != 18:
            raise ValueError('Unexpected Mamba projection dimensions')
        mixer.x_proj = scope['CandidateLayer'](48)
        # Mamba transposes its convolution output, so the projection input is
        # generally noncontiguous. Keep the authored module and state-dict keys
        # intact while giving it a value-equivalent, contiguous tensor.
        mixer.x_proj.register_forward_pre_hook(lambda module, args: (args[0].contiguous(),))
        self.head = nn.Linear(24, 8)
        count = sum(p.numel() for p in self.parameters())
        if not 0 < count <= 1000000:
            raise ValueError('Parameter count outside allowed range')

    def forward(self, x):
        y = self.mamba(inputs_embeds=self.embed(x), use_cache=False).last_hidden_state
        if not torch.is_tensor(y) or y.shape != (*x.shape, 24) or not torch.isfinite(y).all():
            raise ValueError('Expected finite Mamba output')
        return self.head(y[:, -1, :])

    def validate(self):
        self.eval()
        with torch.no_grad():
            projection = self.mamba.layers[0].mixer.x_proj
            sample = torch.randn(2, 4, 48)
            coeff = projection(sample)
            if not torch.is_tensor(coeff) or coeff.shape != (2, 4, 18) or not torch.isfinite(coeff).all():
                raise ValueError('Expected finite projection [batch, sequence, 18]')
            chunks = torch.cat([projection(sample[:, i:i+1, :]) for i in range(4)], dim=1)
            if not torch.allclose(coeff, chunks, atol=1e-5, rtol=1e-5):
                raise ValueError('Projection must be token-local for causal streaming')
            altered = projection(sample + 0.5)
            for start, end, label in ((0, 2, 'delta'), (2, 10, 'B'), (10, 18, 'C')):
                if (coeff[..., start:end] - altered[..., start:end]).abs().max().item() < 1e-7:
                    raise ValueError('Mamba '+label+' must depend on the input')
            embeddings = self.embed(torch.tensor([[1,2,3,4],[4,3,2,1]], dtype=torch.long))
            full = self.mamba(inputs_embeds=embeddings, use_cache=False).last_hidden_state
            prefix = self.mamba(inputs_embeds=embeddings[:,:-1,:], use_cache=False).last_hidden_state
            if not torch.allclose(full[:,:-1,:], prefix, atol=1e-5, rtol=1e-5):
                raise ValueError('Mamba block is not causal')
            first = self.mamba(inputs_embeds=embeddings[:,:-1,:], use_cache=True)
            if getattr(first, 'cache_params', None) is None:
                raise ValueError('Mamba did not return a rolling state cache')
            zero_state = copy.deepcopy(first.cache_params)
            if hasattr(zero_state, 'ssm_states'):
                states = list(zero_state.ssm_states)
            elif hasattr(zero_state, 'layers'):
                states = []
                for layer in zero_state.layers:
                    recurrent = layer.recurrent_states
                    values = recurrent.values() if isinstance(recurrent, dict) else recurrent
                    if torch.is_tensor(values):
                        values = [values]
                    states.extend(value for value in values if value is not None)
            else:
                raise ValueError('Unsupported Mamba cache representation')
            if not states:
                raise ValueError('Mamba cache has no recurrent state')
            for state in states:
                state.zero_()
            last = self.mamba(inputs_embeds=embeddings[:,-1:,:], use_cache=True, cache_params=first.cache_params)
            control = self.mamba(inputs_embeds=embeddings[:,-1:,:], use_cache=True, cache_params=zero_state)
            if not torch.allclose(full[:,-1:,:], last.last_hidden_state, atol=1e-5, rtol=1e-4):
                raise ValueError('Cached rolling state differs from full-sequence output')
            if (last.last_hidden_state-control.last_hidden_state).abs().max().item() < 1e-10:
                raise ValueError('Mamba output does not use the preceding rolling state')
        return {'mamba_block':True, 'input_dependent_delta_b_c':True, 'causal':True,
                'token_local_projection':True, 'streaming_cache_matches':True, 'state_affects_output':True}

torch.manual_seed(7301)
m = Model()
if request['mode'] == 'validate':
    output = m.validate()
elif request['mode'] == 'train':
    opt = torch.optim.Adam(m.parameters(), lr=0.003)
    for examples in request['batches']:
        x = torch.tensor([row['x'] for row in examples], dtype=torch.long)
        y = torch.tensor([row['y'] for row in examples], dtype=torch.long)
        opt.zero_grad(set_to_none=True)
        loss = F.cross_entropy(m(x), y)
        if not torch.isfinite(loss): raise ValueError('Non-finite loss')
        loss.backward()
        nn.utils.clip_grad_norm_(m.parameters(), 1.0)
        opt.step()
    buffer = io.BytesIO()
    torch.save(m.state_dict(), buffer)
    output = {'checkpoint':base64.b64encode(buffer.getvalue()).decode(),
              'parameters':sum(p.numel() for p in m.parameters())}
elif request['mode'] == 'predict':
    if request['checkpoint'] is not None:
        weights = base64.b64decode(request['checkpoint'], validate=True)
        m.load_state_dict(torch.load(io.BytesIO(weights), map_location='cpu', weights_only=True))
    m.eval()
    with torch.no_grad():
        x = torch.tensor(request['inputs'], dtype=torch.long)
        pred = m(x).argmax(dim=-1).tolist()
    output = {'predictions':pred}
else:
    raise ValueError('Unsupported sandbox operation')
print('LAYER_RESPONSE=' + json.dumps(output, allow_nan=False))
