#!/usr/bin/env python3

from typing import NamedTuple
import enum
import logging
import os

import torch
from torch.distributed import rpc
from torch.nn.parallel import DistributedDataParallel
from torch.testing._internal.common_distributed import (
    MultiProcessTestCase,
    requires_gloo,
)
from torch.testing._internal.common_utils import (
    run_tests,
    TEST_WITH_ASAN,
)
from torch.testing._internal.dist_utils import dist_init
from torch._utils_internal import TEST_MASTER_ADDR as MASTER_ADDR
from torch._utils_internal import TEST_MASTER_PORT as MASTER_PORT
import torch.distributed as dist
import torch.distributed.autograd as dist_autograd
import torch.distributed.distributed_c10d as dist_c10d
import torch.nn as nn
import unittest


NUM_EM_ROW = 2
D_SPARSE = 3
D_DENSE = 2
D_HID = 3
D_OUT = 1
NUM_TRAINERS = 4
# Trainers + the master + the remote worker
WORLD_SIZE = NUM_TRAINERS + 2
TRAINER_GROUP = "trainer_group"
TRAINER_RANKS = list(range(1, NUM_TRAINERS + 1))
REMOTE_WORKER_RANK = NUM_TRAINERS + 1
MASTER_RANK = 0


class DdpMode(enum.Enum):
    # Don't apply DDP
    NONE = enum.auto()
    # Apply DDP to the top level nn.Module
    OUTSIDE = enum.auto()
    # Embed DDP inside the top level nn.Module
    INSIDE = enum.auto()


def init_logger():
    logger = logging.getLogger(__name__)
    level = logging.DEBUG if "debug" in os.environ else logging.INFO
    logger.setLevel(level)
    console = logging.StreamHandler()
    formatter = logging.Formatter(
        "%(asctime)s %(filename)s:%(lineno)s %(levelname)s p:%(processName)s t:%(threadName)s: %(message)s"
    )
    console.setFormatter(formatter)
    console.setLevel(level)
    # add the handlers to the logger
    logger.addHandler(console)
    logger.propagate = False
    return logger


gLogger = init_logger()


class FeatureSet(NamedTuple):
    """ A feature set has 2 types of features"""

    dense_features: torch.Tensor
    sparse_features: torch.LongTensor
    values: torch.Tensor


def _call_method(method, rref, *args, **kwargs):
    return method(rref.local_value(), *args, **kwargs)


def _remote_method(method, rref, *args, **kwargs):
    args_tup = tuple([method, rref] + list(args))
    return rpc.rpc_sync(
        rref.owner(), _call_method, args=args_tup, kwargs=kwargs
    )


def _remote_method_async(method, rref, *args, **kwargs):
    args_tup = tuple([method, rref] + list(args))
    return rpc.rpc_async(
        rref.owner(), _call_method, args=args_tup, kwargs=kwargs
    )


class RemoteEM(nn.Module):
    def __init__(self, num_embeddings: int, embedding_dim: int):
        gLogger.info(f"Initing RemoteEM with {num_embeddings} {embedding_dim}")
        super(RemoteEM, self).__init__()
        init_em = [0.5] * embedding_dim
        self.em = nn.EmbeddingBag(
            num_embeddings,
            embedding_dim,
            _weight=torch.Tensor([init_em] * num_embeddings),
        )

    def forward(self, input: torch.Tensor):
        gLogger.debug(f"Running RemoteEM.forward() on: {input}")
        return self.em(input, offsets=torch.LongTensor(range(input.shape[0])))


# Return a linear module with predefined parameters.
def getLinear(d_in, d_out):
    l = nn.Linear(d_in, d_out, bias=False)
    w = torch.ones((d_out, d_in))
    w[0][0] = -1
    w.requires_grad_()
    l.weight.data = w
    return l


class RemoteNet(nn.Module):
    def __init__(self, d_in: int, d_out: int):
        gLogger.info(f"Initing RemoteNet with {d_in} {d_out}")
        super(RemoteNet, self).__init__()
        self.fc = getLinear(d_in, d_out)
        self.relu = nn.ReLU()

    def forward(self, input: torch.Tensor):
        gLogger.debug(f"Running RemoteNet.forward() on: {input}")
        return self.relu(self.fc(input))


class HybridModel(nn.Module):
    def __init__(
        self,
        remote_em_rref: rpc.RRef,
        remote_net_rref: rpc.RRef,
        process_group_for_ddp: dist.ProcessGroup = None,
    ):
        super(HybridModel, self).__init__()
        self.remote_em_rref = remote_em_rref
        self.remote_net_rref = remote_net_rref
        self.fc1 = getLinear(D_DENSE, D_DENSE)
        self.fc2 = getLinear(D_HID, D_OUT)

        self.non_ddp_params = tuple(self.fc1.parameters()) + tuple(
            self.fc2.parameters()
        )
        self.ddp_params = ()

        if process_group_for_ddp is not None:
            self.non_ddp_params, self.ddp_params = (
                tuple(self.fc1.parameters()),
                tuple(self.fc2.parameters()),
            )
            gLogger.info(f"Use DDP for the second local net.")
            self.fc2 = DistributedDataParallel(
                self.fc2,
                process_group=process_group_for_ddp,
                check_reduction=True,
            )

        gLogger.info(
            f"HybridModel has {len(list(self.parameters()))} groups of parameters."
        )

    def forward(self, input: FeatureSet):
        gLogger.debug(f"Running HybridModel.forward on {input}")
        sparse = _remote_method(
            RemoteEM.forward, self.remote_em_rref, input.sparse_features
        )
        # The same size of mini batch.
        assert sparse.shape[0] == input.dense_features.shape[0]
        dense = self.fc1(input.dense_features)
        x = torch.cat((dense, sparse), 1)
        gLogger.debug(f"Concatenated feature: {x}")
        x = _remote_method(RemoteNet.forward, self.remote_net_rref, x)
        return self.fc2(x)


class Trainer:
    def __init__(
        self,
        remote_em_rref: rpc.RRef,
        remote_net_rref: rpc.RRef,
        ddp_mode: DdpMode,
        rank: int,
    ):
        gLogger.info(
            f"Initing trainer process group by trainer #{rank} with ranks {TRAINER_RANKS}"
        )
        self.process_group_for_ddp = dist_c10d.new_group(ranks=TRAINER_RANKS)

        self.remote_em_rref = remote_em_rref
        self.remote_net_rref = remote_net_rref
        self.hybrid_module = HybridModel(
            self.remote_em_rref,
            self.remote_net_rref,
            self.process_group_for_ddp
            if ddp_mode in (DdpMode.INSIDE,)
            else None,
        )
        self.ddp_params, self.non_ddp_params = (
            self.hybrid_module.ddp_params,
            self.hybrid_module.non_ddp_params,
        )
        if ddp_mode == DdpMode.OUTSIDE:
            gLogger.info(f"Wrapping the whole hybrid module into DDP.")
            self.ddp_params += self.non_ddp_params
            self.non_ddp_params = ()
            self.hybrid_module = DistributedDataParallel(
                self.hybrid_module,
                process_group=self.process_group_for_ddp,
                check_reduction=True,
            )
        gLogger.info(
            f"Succeeded in creating a HybridModel instance with "
            f"{len(self.ddp_params)} ddp params and {len(self.non_ddp_params)} "
            f"other local params."
        )

    def __del__(self):
        dist.destroy_process_group(self.process_group_for_ddp)

    def do_backward(self, mini_batch: FeatureSet):
        grads_dict = None
        with dist_autograd.context() as context_id:
            output = self.hybrid_module.forward(mini_batch)
            loss = (output * mini_batch.values).sum()
            dist_autograd.backward(context_id, [loss])
            grads_dict = dist_autograd.get_gradients(context_id)
            gLogger.info(
                f"Loss is {loss} for mini batch: {mini_batch}. "
                f"Grads dict has {len(grads_dict)} entries: {grads_dict}"
            )
        return (
            tuple(grads_dict[param] for param in self.ddp_params),
            tuple(grads_dict[param] for param in self.non_ddp_params),
        )


def get_training_examples():
    n = 16
    training_examples = FeatureSet(
        dense_features=torch.zeros((n, D_DENSE)),
        sparse_features=torch.zeros(n, dtype=torch.long),
        values=torch.zeros(n),
    )
    idx = 0
    # Every example has another one that has exactly the same features but an
    # opposite value. Therefore, their grads cancel each other in all-reduce.
    for value in (-1, 1):
        for x in (-1 * value, 1 * value):
            for y in (1 * value, -1 * value):
                for z in (0, 1):
                    training_examples.dense_features[idx, :] = torch.Tensor(
                        (x, y)
                    )
                    training_examples.sparse_features[idx] = z
                    training_examples.values[idx] = value
                    idx += 1

    # Split the examples among NUM_TRAINERS trainers
    assert 0 == (n % NUM_TRAINERS)
    examples_per_trainer = int(n / NUM_TRAINERS)
    return [
        FeatureSet(
            dense_features=training_examples.dense_features[
                start : start + examples_per_trainer, :
            ],
            sparse_features=training_examples.sparse_features[
                start : start + examples_per_trainer
            ],
            values=training_examples.values[
                start : start + examples_per_trainer
            ],
        )
        for start in range(0, n, examples_per_trainer)
    ]


@unittest.skipIf(
    TEST_WITH_ASAN,
    "Skip ASAN as torch + multiprocessing spawn have known issues",
)
class TestDdpUnderDistAutograd(MultiProcessTestCase):
    rpc_backend = rpc.backend_registry.BackendType.PROCESS_GROUP
    rpc_backend_options = None

    @property
    def world_size(self) -> int:
        return WORLD_SIZE

    def remote_worker_name(self) -> str:
        # The name has to be consistent with that in 'dist_init' decorator.
        return f"worker{REMOTE_WORKER_RANK}"

    def trainer_name(self, rank):
        # The name has to be consistent with that in 'dist_init' decorator.
        return f"worker{rank}"

    def setUp(self):
        super(TestDdpUnderDistAutograd, self).setUp()

        os.environ["MASTER_ADDR"] = str(MASTER_ADDR)
        os.environ["MASTER_PORT"] = str(MASTER_PORT)
        self._spawn_processes()

    def tearDown(self):
        super(TestDdpUnderDistAutograd, self).tearDown()

    @dist_init
    def _remote_worker_process(self):
        process_group_for_ddp = dist_c10d.new_group(ranks=TRAINER_RANKS)
        gLogger.info(f"The remote worker is running.")
        dist.destroy_process_group(process_group_for_ddp)
        gLogger.info(f"Exiting remote worker.")

    @dist_init
    def _trainer_process(self, rank: int):
        gLogger.info(f"Running the trainer #{rank}...")

    @dist_init
    def _master_process(self, ddp_mode: DdpMode):
        gLogger.info(f"Running the master process...")
        process_group_for_ddp = dist_c10d.new_group(ranks=TRAINER_RANKS)
        remote_em_rref = rpc.remote(
            self.remote_worker_name(), RemoteEM, args=(NUM_EM_ROW, D_SPARSE)
        )
        remote_net_rref = rpc.remote(
            self.remote_worker_name(),
            RemoteNet,
            args=(D_DENSE + D_SPARSE, D_HID),
        )
        gLogger.info(f"Created remote rrefs on master")
        self.do_test_on_master(ddp_mode, remote_em_rref, remote_net_rref)
        dist.destroy_process_group(process_group_for_ddp)

    def do_test_on_master(
        self,
        ddp_mode: DdpMode,
        remote_em_rref: rpc.RRef,
        remote_net_rref: rpc.RRef,
    ):
        trainer_rrefs = []
        for rank in TRAINER_RANKS:
            trainer = self.trainer_name(rank)
            trainer_rrefs.append(
                rpc.remote(
                    trainer,
                    Trainer,
                    args=(remote_em_rref, remote_net_rref, ddp_mode, rank),
                )
            )

        training_examples = get_training_examples()
        for _ in range(3):
            futures = []
            for idx, trainer_rref in enumerate(trainer_rrefs):
                futures.append(
                    _remote_method_async(
                        Trainer.do_backward,
                        trainer_rref,
                        training_examples[idx],
                    )
                )

            for future in futures:
                ddp_grads, non_ddp_grads = future.wait()
                for grad in ddp_grads:
                    self.assertAlmostEqual(
                        0.0,
                        float(grad.norm()),
                        msg="The grad for any ddp parameter should be zeros, because "
                        "the training examples' grads cancel each other.",
                    )
                for grad in non_ddp_grads:
                    self.assertNotAlmostEqual(
                        0.0,
                        float(grad.norm()),
                        msg="The grad for any non-ddp parameter shouldn't be zeros",
                    )

    def _do_test(self, ddp_mode):
        if self.rank == MASTER_RANK:
            self._master_process(ddp_mode)
        elif self.rank == REMOTE_WORKER_RANK:
            self._remote_worker_process()
        elif self.rank in TRAINER_RANKS:
            self._trainer_process(self.rank)
        else:
            raise RuntimeError(f"Unknow process rank: {self.rank}")

    @requires_gloo()
    def test_backward_no_ddp(self):
        self._do_test(DdpMode.NONE)

    @requires_gloo()
    def test_backward_ddp_outside(self):
        self._do_test(DdpMode.OUTSIDE)

    @requires_gloo()
    def test_backward_ddp_inside(self):
        self._do_test(DdpMode.INSIDE)


if __name__ == "__main__":
    run_tests()
