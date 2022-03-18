import sys
sys.path.insert(0, "./../..")

import numpy as np
from sklearn.model_selection import train_test_split

import torch
import torch.nn.functional as F

from betty.engine import Engine
from betty.config_template import Config
from betty.problems import ImplicitProblem

device = 'cuda' if torch.cuda.is_available() else 'cpu'

# hyperparameters
DATA_NUM = 1000
DATA_DIM = 20

# data preparation
w_gt = np.random.randn(DATA_DIM)
x = np.random.randn(DATA_NUM, DATA_DIM)
y = x @ w_gt + 0.1 * np.random.randn(DATA_NUM)
y = (y > 0).astype(float)

x_train, x_val, y_train, y_val = train_test_split(x, y, test_size=0.5)
x_train, y_train = torch.from_numpy(x_train).to(device).float(), torch.from_numpy(y_train).to(device).float()
x_val, y_val = torch.from_numpy(x_val).to(device).float(), torch.from_numpy(y_val).to(device).float()

def make_data_loader(xs, ys):
    datasets = [(xs, ys)]

    return datasets


class ChildNet(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()

        self.w = torch.nn.Parameter(torch.zeros(DATA_DIM))

    def forward(self, inputs):
        outs = inputs @ self.w
        return outs, self.w

class ParentNet(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()

        self.w = torch.nn.Parameter(torch.ones(DATA_DIM))

    def forward(self):
        return self.w

class Parent(ImplicitProblem):
    def forward(self, *args, **kwargs):
        return self.module()

    def training_step(self, batch, *args, **kwargs):
        inputs, targets = batch
        outs = self.inner(inputs)[0]
        loss = F.binary_cross_entropy_with_logits(outs, targets)

        if self.count % 10 == 0:
            print('count:', self.count, '|| val loss:', loss.item())
        return loss

    def configure_train_data_loader(self):
        return make_data_loader(x_val, y_val)

    def configure_module(self):
        return ParentNet().to(device)

    def configure_optimizer(self):
        return torch.optim.SGD(self.module.parameters(), lr=1, momentum=0.9)

    def param_callback(self, params):
        for p in params:
            p.data.clamp_(min=1e-8)

class Child(ImplicitProblem):
    def forward(self, inputs):
        return self.module(inputs)

    def training_step(self, batch, *args, **kwargs):
        inputs, targets = batch
        outs, params = self.module(inputs)
        loss = F.binary_cross_entropy_with_logits(outs, targets) +\
            0.5 * (params.unsqueeze(0) @ torch.diag(self.outer()) @ params.unsqueeze(1)).sum()
        return loss

    def configure_train_data_loader(self):
        return make_data_loader(x_train, y_train)

    def configure_module(self):
        return ChildNet().to(device)

    def configure_optimizer(self):
        return torch.optim.SGD(self.module.parameters(), lr=0.1)

    def on_inner_loop_start(self):
        self.module.w.data.zero_()

parent_config = Config(type='darts',
                       step=100,
                       first_order=True)
child_config = Config(type='torch')
parent = Parent(name='outer', config=parent_config, device=device)
child = Child(name='inner', config=child_config, device=device)

problems = [parent, child]
dependencies = {parent: [child]}

engine = Engine(config=None, problems=problems, dependencies=dependencies)
engine.run()