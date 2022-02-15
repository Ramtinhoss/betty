import abc
import typing
from dataclasses import dataclass

import torch
import functorch

import betty.optim as optim
import betty.hypergradient as hypergradient
import betty.utils as utils


@dataclass
class HypergradientConfig:
    type: str = 'maml'
    step: int = 2
    first_order: bool = False
    retain_graph: bool = False
    allow_unused: bool = False
    leaf: bool = False


class Module:
    def __init__(self,
                 config,
                 device=None):
        self._config = config
        self.device = device

        # computation graph depedency
        # ! dependency can be defined both in ``Module'' class and ``Engine'' class
        self._parents = []
        self._children = []
        self.ready = None
        self.count = 0

        # data loader
        self.data_loader = None

        # module
        self.module = None
        self.fmodule = None
        self.params = None
        self.buffers = None

        # optimizer
        self.optimizer = None
        self.state = []
        self.param_groups = []
        self.param_mapping = []
        self.flattened_param_mapping = None
        self.update_fn = None

        # misc
        self._first_order = False
        self._retain_graph = config.retain_graph
        self._allow_unused = config.allow_unused
        self._inner_loop_start = True

    def initialize(self):
        """[summary]
        Initialize basic things
        """
        # initialize update ready to False
        if self.config.leaf:
            assert len(self._children) == 0
        self.ready = [False for _ in range(len(self._children))]

        # initialize whether to track higher-order gradient for parameter update
        first_order = []
        for problem in self._parents:
            hgconfig = problem.config
            first_order.append(hgconfig.first_order)
        self._first_order = all(first_order)

        self._inner_loop_start = True

        # set up data loader
        self.data_loader = iter(self.configure_data_loader())

        # set up module for the current level
        self.module = self.configure_module()

        # set up optimizer
        self.optimizer = self.configure_optimizer()

        # patch model and optimizer to follow functional programming paradigm
        self.initialize_optimizer_state()
        self.patch_models()
        self.patch_optimizer()
        self.zero_grad()

    def __call__(self, *args, **kwargs):
        return self.forward(*args, **kwargs)

    @abc.abstractmethod
    def forward(self, *args, **kwargs):
        """[summary]
        Users define how forward call is defined for the current problem.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def loss_fn(self, batch, *args, **kwargs):
        """[summary]
        Users define how loss is calculated for the current problem.
        """
        # (batch, batch_idx)
        raise NotImplementedError

    def step(self, *args, **kwargs):
        """[summary]
        Perform gradient calculation and update parameters accordingly
        """
        if self.check_ready():
            if self._inner_loop_start:
                self.on_inner_loop_start()
                self._inner_loop_start = False
            self.count += 1

            # load data
            try:
                batch = next(self.data_loader)
            except StopIteration:
                self.data_loader = iter(self.configure_data_loader())
                batch = next(self.data_loader)

            # calculate loss
            loss = self.loss_fn(batch, *args, **kwargs)

            # calculate gradient
            self.backward(loss, self.params,
                          create_graph=not self._first_order,
                          retain_graph=self._retain_graph,
                          allow_unused=self._allow_unused)

            # calculate parameter update
            new_params = self.optimizer_step()
            self.params = new_params

            # zero-out grad
            self.zero_grad()

            # call parent step function
            for problem in self._parents:
                if self.count % problem.config.step == 0:
                    idx = problem.children.index(self)
                    problem.ready[idx] = True
                    problem.step()

                    self._inner_loop_start = True

                # TODO: reinitialize params at the beginning of inner loop

            self.ready = [False for _ in range(len(self._children))]

    def backward(self, loss, params, create_graph=True, retain_graph=False, allow_unused=False):
        """[summary]
        Calculate and return gradient for given loss and parameters
        Args:
            loss ([type]): [description]
            params ([type]): [description]
            first_order (bool, optional): [description]. Defaults to False.

        Returns:
            [type]: [description]
        """
        if self.optimizer is None:
            return None

        if self._config.leaf:
            grad = torch.autograd.grad(loss, params,
                                       create_graph=create_graph,
                                       retain_graph=retain_graph,
                                       allow_unused=allow_unused)
        else:
            assert len(self._children) > 0
            grad_fn = hypergradient.get_grad_fn(self.config.type)
            grad = grad_fn(loss, params,
                           create_graph=create_graph,
                           retain_graph=retain_graph,
                           allow_unused=allow_unused)

        # set gradient for each parameter
        for p, g in zip(params, grad):
            if hasattr(p, 'gradient') and p.gradient is not None:
                p.gradient = p.gradient + g
            else:
                p.gradient = g

    def optimizer_step(self, *args, **kwargs):
        """[summary]
        Update weights as in native PyTorch's optim.step()
        """
        if self.optimizer is None:
            new_params = self.custom_optimizer_step(*args, **kwargs)
        else:
            new_params = self.update_fn(
                self.params,
                self.param_mapping,
                self.param_groups,
                self.state
            )

        self.param_callback(new_params)

        return new_params

    def custom_optimizer_step(self):
        """[summary]
        Users define how optimizer step is performed. This is mainly used for developing
        meta- (or learnable) optimizer
        """
        return self.params

    def zero_grad(self):
        """[summary]
        Set gradients for trainable parameters for the current problem to 0.
        """
        for param in list(self.params):
            if hasattr(param, 'gradient'):
                del param.gradient
            if hasattr(param, 'grad'):
                del param.grad

    @abc.abstractmethod
    def configure_data_loader(self):
        """[summary]
        Return user-defined data loader
        """
        raise NotImplementedError

    @abc.abstractmethod
    def configure_module(self):
        """[summary]
        Return user-defined module
        """
        raise NotImplementedError

    @abc.abstractmethod
    def configure_optimizer(self):
        """[summary]
        Return user-defined optimizer
        """
        raise NotImplementedError

    def grad_callback(self, grads):
        """[summary]
        Users define custom gradient callback functions such as gradient clipping
        """
        return

    def param_callback(self, params):
        """[summary]
        Users define custom parameter callback functions such as parameter clipping
        """
        return

    def on_inner_loop_start(self):
        self._inner_loop_start = False

    def initialize_optimizer_state(self):
        """[summary]
        Initialize optimizer state
        """
        for param_group in self.optimizer.param_groups:
            for param in param_group['params']:
                param.grad = torch.zeros_like(param.data)
        self.optimizer.step()

    def patch_models(self):
        """[summary]
        Patch models to support functional forward that takes params as an input
        """
        fmodule, params, buffers = functorch.make_functional_with_buffers(self.module)
        self.fmodule = fmodule
        self.params = params
        self.buffers = buffers

    def patch_optimizer(self):
        """[summary]
        Patch optimizer to avoid in-place operations so that gradient flows through param update.
        Raises:
            NotImplementedError: [description]
        """
        if self.optimizer is None:
            return None

        self.state = [None for _ in range(len(list(self.module.parameters())))]
        for param_group in self.optimizer.param_groups:
            # copy param group dictionaory from optimizer.param_groups to self.param_groups
            my_param_group = {}
            for key, value in param_group.items():
                if key != 'params':
                    my_param_group[key] = value
            self.param_groups.append(my_param_group)

            # construct param mapping from optimizer.param_groups
            param_mapping = []
            for param in param_group['params']:
                param_idx = list(self.module.parameters()).index(param)
                param_mapping.append(param_idx)
                self.state[param_idx] = self.optimizer.state[param]
            self.param_mapping.append(param_mapping)
        self.flattened_param_mapping = utils.flatten_list(self.param_mapping)

        self.update_fn = optim.get_update_fn(self.optimizer)

    def check_ready(self):
        """[summary]
        Check if parameter updates in all children are ready
        """
        if self._config.leaf:
            return True
        ready = all(self.ready)
        return ready

    def add_child(self, problem):
        """[summary]
        Add a new problem to the children node list.
        """
        assert problem not in self._children
        assert problem not in self._parents
        self._children.append(problem)

    def add_parent(self, problem):
        """[summary]
        Add a new problem to the parent node list.
        """
        assert problem not in self._children
        assert problem not in self._parents
        self._parents.append(problem)

    def parameters(self):
        """[summary]
        Return parameters for the current problem.
        """
        return self.params

    def trainable_parameters(self):
        """[summary]
        Return trainable parameters for the current problem.
        """
        mapping_set = set(self.flattened_param_mapping)
        trainable_params = list(p for idx, p in enumerate(self.params) if idx in mapping_set)
        return trainable_params

    @property
    def config(self):
        """[summary]
        Return the hypergradient configuration for the current problem.
        """
        return self._config

    @property
    def children(self):
        """[summary]
        Return children problems for the current problem.
        """
        return self._children

    @property
    def parents(self):
        """[summary]
        Return parent problemss for the current problem.
        """
        return self._parents