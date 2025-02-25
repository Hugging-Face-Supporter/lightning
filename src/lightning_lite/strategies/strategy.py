# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import contextlib
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Generator, Iterable, List, Mapping, Optional, Tuple, TypeVar, Union

import torch
from torch import Tensor
from torch.nn import Module
from torch.optim import Optimizer
from torch.utils.data import DataLoader

from lightning_lite.accelerators import Accelerator
from lightning_lite.plugins.io.checkpoint_plugin import CheckpointIO
from lightning_lite.plugins.io.torch_plugin import TorchCheckpointIO
from lightning_lite.plugins.precision import Precision
from lightning_lite.strategies.launchers.base import _Launcher
from lightning_lite.utilities.apply_func import move_data_to_device
from lightning_lite.utilities.distributed import ReduceOp
from lightning_lite.utilities.optimizer import optimizer_to_device
from lightning_lite.utilities.types import _PATH, Steppable

TBroadcast = TypeVar("TBroadcast")
TReduce = TypeVar("TReduce")

log = logging.getLogger(__name__)


class Strategy(ABC):
    """Base class for all strategies that change the behaviour of the training, validation and test- loop."""

    def __init__(
        self,
        accelerator: Optional[Accelerator] = None,
        checkpoint_io: Optional[CheckpointIO] = None,
        precision_plugin: Optional[Precision] = None,
    ) -> None:
        self._accelerator: Optional[Accelerator] = accelerator
        self._checkpoint_io: Optional[CheckpointIO] = checkpoint_io
        self._precision_plugin: Optional[Precision] = precision_plugin
        self._launcher: Optional[_Launcher] = None

    @property
    @abstractmethod
    def root_device(self) -> torch.device:
        """Returns the root device."""

    @property
    @abstractmethod
    def is_global_zero(self) -> bool:
        """Whether the current process is the rank zero process not only on the local node, but for all nodes."""

    @property
    def launcher(self) -> Optional[_Launcher]:
        return self._launcher

    @property
    def accelerator(self) -> Optional[Accelerator]:
        return self._accelerator

    @accelerator.setter
    def accelerator(self, accelerator: Accelerator) -> None:
        self._accelerator = accelerator

    @property
    def checkpoint_io(self) -> CheckpointIO:
        if self._checkpoint_io is None:
            self._checkpoint_io = TorchCheckpointIO()
        return self._checkpoint_io

    @checkpoint_io.setter
    def checkpoint_io(self, io: Optional[CheckpointIO]) -> None:
        self._checkpoint_io = io

    @property
    def precision_plugin(self) -> Precision:
        return self._precision_plugin if self._precision_plugin is not None else Precision()

    @precision_plugin.setter
    def precision_plugin(self, precision_plugin: Optional[Precision]) -> None:
        self._precision_plugin = precision_plugin

    def _configure_launcher(self) -> None:
        """Attach the launcher based on Strategy."""

    def setup_environment(self) -> None:
        """Setup any processes or distributed connections.

        This must be called by the framework at the beginning of every process, before any distributed communication
        takes place.
        """
        assert self.accelerator is not None
        self.accelerator.setup_device(self.root_device)

    def process_dataloader(self, dataloader: DataLoader) -> DataLoader:
        """Wraps the dataloader if necessary.

        Args:
            dataloader: iterable. Ideally of type: :class:`torch.utils.data.DataLoader`
        """
        return dataloader

    def setup_module_and_optimizers(
        self, module: Module, optimizers: List[Optimizer]
    ) -> Tuple[Module, List[Optimizer]]:
        """Set up a model and multiple optimizers together.

        The returned objects are expected to be in the same order they were passed in. The default implementation will
        call :meth:`_setup_model` and :meth:`_setup_optimizer` on the inputs.
        """
        module = self.setup_module(module)
        optimizers = [self.setup_optimizer(optimizer) for optimizer in optimizers]
        return module, optimizers

    def setup_module(self, module: Module) -> Module:
        """Performs setup for the model, e.g., by wrapping it by another class."""
        return module

    def setup_optimizer(self, optimizer: Optimizer) -> Optimizer:
        """Performs setup for the optimizer, e.g., by wrapping it by another class."""
        return optimizer

    @abstractmethod
    def module_to_device(self, module: Module) -> None:
        """Moves the model to the correct device."""

    def batch_to_device(self, batch: Any, device: Optional[torch.device] = None) -> Any:
        """Moves the batch to the correct device.

        The returned batch is of the same type as the input batch, just
        having all tensors on the correct device.

        Args:
            batch: The batch of samples to move to the correct device
            device: The target device
        """
        device = device or self.root_device
        return move_data_to_device(batch, device)

    @contextlib.contextmanager
    def module_sharded_context(self) -> Generator:
        """Provide hook to create modules in a distributed aware context. This is useful for when we'd like to
        shard the model instantly, which is useful for extremely large models which can save memory and
        initialization time.

        Returns: Model parallel context.
        """
        yield

    def backward(self, tensor: Tensor, module: Optional[Module], *args: Any, **kwargs: Any) -> None:
        r"""Forwards backward-calls to the precision plugin."""
        self.precision_plugin.pre_backward(tensor, module)
        self.precision_plugin.backward(tensor, module, *args, **kwargs)
        self.precision_plugin.post_backward(tensor, module)

    def optimizer_step(
        self,
        optimizer: Steppable,
        **kwargs: Any,
    ) -> Any:
        """Performs the actual optimizer step.

        Args:
            optimizer: the optimizer performing the step
            **kwargs: Any extra arguments to ``optimizer.step``
        """
        return self.precision_plugin.optimizer_step(optimizer, **kwargs)

    @abstractmethod
    def reduce(
        self,
        tensor: Union[Tensor, Any],
        group: Optional[Any] = None,
        reduce_op: Optional[Union[ReduceOp, str]] = "mean",
    ) -> Union[Tensor, Any]:
        """Reduces the given tensor (e.g. across GPUs/processes).

        Args:
            tensor: the tensor to sync and reduce
            group: the process group to reduce
            reduce_op: the reduction operation. Defaults to 'mean'.
                Can also be a string 'sum' or ReduceOp.
        """

    @abstractmethod
    def barrier(self, name: Optional[str] = None) -> None:
        """Synchronizes all processes which blocks processes until the whole group enters this function.

        Args:
            name: an optional name to pass into barrier.
        """

    @abstractmethod
    def broadcast(self, obj: TBroadcast, src: int = 0) -> TBroadcast:
        """Broadcasts an object to all processes.

        Args:
            obj: the object to broadcast
            src: source rank
        """

    @abstractmethod
    def all_gather(self, tensor: Tensor, group: Optional[Any] = None, sync_grads: bool = False) -> Tensor:
        """Perform an all_gather on all processes.

        Args:
            tensor: the tensor to all_gather
            group: the process group to gather results from
            sync_grads: flag that allows users to synchronize gradients for all_gather op
        """

    def reduce_boolean_decision(self, decision: bool) -> bool:
        """Reduce a boolean decision across all processes."""
        return decision

    def save_checkpoint(
        self, checkpoint: Dict[str, Any], filepath: _PATH, storage_options: Optional[Any] = None
    ) -> None:
        """Save model/training states as a checkpoint file through state-dump and file-write.

        Args:
            checkpoint: dict containing model and trainer state
            filepath: write-target file's path
            storage_options: parameter for how to save to storage, passed to ``CheckpointIO`` plugin
        """
        if self.is_global_zero:
            self.checkpoint_io.save_checkpoint(checkpoint, filepath, storage_options=storage_options)

    def get_module_state_dict(self, module: Module) -> Dict[str, Union[Any, Tensor]]:
        """Returns model state."""
        # TODO(lite): Integrate this into Lightning Lite
        return module.state_dict()

    def get_optimizer_state(self, optimizer: Optimizer) -> Dict[str, Tensor]:
        """Returns state of an optimizer.

        Allows for syncing/collating optimizer state from processes in custom plugins.
        """
        if hasattr(optimizer, "consolidate_state_dict"):
            # there are optimizers like Fairscale's OSS or PyTorch's ZeroRedundancyOptimizer that shard their
            # states, and to avoid OOM we consolidate the full state on rank 0 only
            optimizer.consolidate_state_dict()
            return optimizer.state_dict() if self.is_global_zero else {}

        # for optimizers that are not sharded, we return the state dict on all ranks
        return optimizer.state_dict()

    def load_checkpoint(self, checkpoint_path: _PATH) -> Dict[str, Any]:
        torch.cuda.empty_cache()
        return self.checkpoint_io.load_checkpoint(checkpoint_path)

    def load_module_state_dict(self, module: Module, checkpoint: Mapping[str, Any]) -> None:
        # TODO(lite): Integrate this into Lightning Lite
        module.load_state_dict(checkpoint["state_dict"])

    def load_optimizer_state_dict(
        self, optimizers: Union[Optimizer, Iterable[Optimizer]], checkpoint: Mapping[str, Any]
    ) -> None:
        if not isinstance(optimizers, Iterable):
            optimizers = [optimizers]
        optimizer_states = checkpoint["optimizer_states"]
        for optimizer, opt_state in zip(optimizers, optimizer_states):
            optimizer.load_state_dict(opt_state)
            optimizer_to_device(optimizer, self.root_device)

    def remove_checkpoint(self, filepath: _PATH) -> None:
        """Remove checkpoint filepath from the filesystem.

        Args:
            filepath: Path to checkpoint
        """
        if self.is_global_zero:
            self.checkpoint_io.remove_checkpoint(filepath)

    def teardown(self) -> None:
        """This method is called to teardown the training process.

        It is the right place to release memory and free other resources.
        """
        self.precision_plugin.teardown()
        assert self.accelerator is not None
        self.accelerator.teardown()
        self.checkpoint_io.teardown()

    @classmethod
    def register_strategies(cls, strategy_registry: Dict[str, Any]) -> None:
        pass
