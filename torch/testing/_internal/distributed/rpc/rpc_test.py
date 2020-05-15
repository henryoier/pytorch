import concurrent.futures
import sys
import time
import unittest
from collections import namedtuple
from functools import partial
from unittest import mock

import torch
import torch.distributed as dist
import torch.distributed.rpc as rpc
import torch.testing._internal.dist_utils as dist_utils
from torch.distributed.rpc import RRef, _get_debug_info, _rref_context_get_debug_info
from torch.distributed.rpc.api import _delete_all_user_rrefs, _use_rpc_pickler
from torch.distributed.rpc.internal import (
    PythonUDF,
    RPCExecMode,
    _internal_rpc_pickler,
    _build_rpc_profiling_key,
)
from torch.testing._internal.common_distributed import skip_if_lt_x_gpu
from torch.testing._internal.common_utils import IS_MACOS, load_tests
from torch.testing._internal.dist_utils import (
    dist_init,
    get_function_event,
    get_shutdown_error_regex,
    get_timeout_error_regex,
    initialize_pg,
    wait_until_node_failure,
    wait_until_pending_users_flushed,
    worker_name,
)
from torch.testing._internal.distributed.rpc.rpc_agent_test_fixture import (
    RpcAgentTestFixture,
)
from torch.testing._internal.common_utils import TemporaryFileName
from torch.testing._internal.distributed.rpc.faulty_rpc_agent_test_fixture import (
    FaultyRpcAgentTestFixture,
)
from torch.testing._internal.distributed.rpc.tensorpipe_rpc_agent_test_fixture import (
    TensorPipeRpcAgentTestFixture,
)


def foo_add():
    return torch.add(torch.ones(1), torch.ones(1))


def requires_process_group_agent(message=""):
    def decorator(old_func):
        return unittest.skipUnless(
            dist_utils.TEST_CONFIG.rpc_backend_name == "PROCESS_GROUP", message
        )(old_func)

    return decorator


VALUE_FUTURE = concurrent.futures.Future()
DONE_FUTURE = concurrent.futures.Future()


class StubRpcAgent:
    def __init__(self, world_size):
        self.world_size = world_size

    def get_worker_infos(self):
        return {
            rpc.WorkerInfo(name=worker_name(rank), id=rank)
            for rank in range(self.world_size)
        }


def _stub_construct_rpc_backend_options_handler(**kwargs):
    return mock.Mock()  # RpcBackendOptions.


def _stub_init_rpc_backend_handler(store, name, rank, world_size, rpc_backend_options):
    return StubRpcAgent(world_size=world_size)


def set_value(value):
    VALUE_FUTURE.set_result(value)


def wait_for_value_future():
    return VALUE_FUTURE.result()


def set_and_check_done(value):
    VALUE_FUTURE.set_result(value)
    return DONE_FUTURE.result()


# it is used to test python user defined function over rpc
# classes and functions are used to test python user defined class and
# methods over rpc
TensorClass = namedtuple("TensorClass", ["tensors"])


class MyPickleClass:
    def __init__(self):
        self.t = None

    def __getstate__(self):
        (pickled_python_udf, tensors) = _internal_rpc_pickler.serialize(
            PythonUDF(my_tensor_function, (torch.ones(2, 2), torch.ones(2, 2)), None)
        )
        return (pickled_python_udf, tensors)

    def __setstate__(self, obj):
        python_udf = _internal_rpc_pickler.deserialize(obj[0], obj[1])
        result = python_udf.func(python_udf.args[0], python_udf.args[1])
        self.t = result

    def set(self, val):
        self.t = val


class MyClass:
    def __init__(self, a):
        self.a = a

    def my_instance_method(self, b):
        return self.a + b

    @classmethod
    def my_class_method(cls, d, e):
        return d + e

    @staticmethod
    def my_static_method(f):
        return f > 10

    def increment_value(self, increment):
        self.a += increment

    def get_value(self):
        return self.a


def _call_method_on_rref(method, rref, *args, **kwargs):
    return method(rref.local_value(), *args, **kwargs)


def get_rref_list(values):
    return [RRef(MyClass(a)) for a in values]


def add_rref_to_value(rref, value):
    return rref.to_here() + value


def run_nested_pickle(pickle_cls_instance, tensor):
    return pickle_cls_instance.t + tensor


def build_complex_tensors():
    a = torch.ones(3, 3)
    b = [a, a]
    c = [b, b]
    d = [a, b]
    e = {a: d}
    return [a, b, c, d, e]

def non_cont_test(t_view, t_cont):
    if t_view.is_contiguous():
        raise Exception('t_view is contiguous!')
    if not t_cont.is_contiguous():
        raise Exception('t_cont is not contiguous!')
    if not torch.equal(t_view, t_cont):
        raise Exception('t_view is not equal to t_cont!')
    return t_view

def my_function(a, b, c):
    return a + b + c


def my_tensor_function(a, b):
    return a + b


def my_sleep_func(seconds=1):
    time.sleep(seconds)


def my_complex_tensor_function(list_input, tensor_class_input, dict_input):
    res = list_input[0]
    for t in list_input:
        res += t
    for k, v in dict_input.items():
        res += v
    complex_tensors = tensor_class_input.tensors
    return (res, complex_tensors[0], complex_tensors[1], complex_tensors[2])


def my_rref_function(rref_a, rref_b):
    return rref_a.to_here() + rref_b.to_here()


def delayed_add(a, b, seconds=0.05):
    time.sleep(seconds)
    return a + b


def no_result():
    print("do nothing")

def raise_or_inc(value):
    if value.numel() == 2:
        raise ValueError("Expected error")
    return value + 1

def nested_rpc(dst):
    return rpc.rpc_sync(dst, torch.add, args=(torch.ones(2, 2), 1))


def multi_layer_nested_async_rpc(dst, world_size, ttl):
    # this method returns immediately without blocking the callee, but will
    # generate additional requests.
    if ttl > 0:
        current_dst = worker_name(dst)
        next_dst = (dst + 1) % world_size
        rpc.rpc_async(
            current_dst,
            multi_layer_nested_async_rpc,
            args=(next_dst, world_size, ttl - 1),
        )
        return 0


def nested_rref(dst):
    return (
        rpc.remote(dst, torch.add, args=(torch.ones(2, 2), 1)),
        rpc.remote(dst, torch.add, args=(torch.ones(2, 2), 2)),
    )


def nested_remote(dst):
    rref = rpc.remote(dst, torch.add, args=(torch.ones(2, 2), 3))
    return rref.to_here()


def rref_forward_chain(dst, world_size, rref, ttl):
    if ttl > 0:
        current_dst = worker_name(dst)
        next_dst = (dst + 1) % world_size
        ret_rref = rpc.remote(
            current_dst, rref_forward_chain, args=(next_dst, world_size, rref, ttl - 1)
        )
        return [ret_rref]
    else:
        return rref.to_here()


def rpc_return_rref(dst):
    return rpc.remote(dst, torch.add, args=(torch.ones(2, 2), 1))


def light_rpc():
    return 0


def heavy_rpc(tensor):
    for i in range(1, 100):
        tensor *= i
        tensor /= i + 1
    return 0


@torch.jit.script
def heavy_rpc_torchscript(tensor):
    for i in range(1, 100):
        tensor *= i
        tensor /= i + 1
    return 0


@torch.jit.script
def my_script_func(tensor):
    return torch.add(tensor, tensor)


def raise_func():
    raise ValueError("Expected error")


global_rref = None


def set_global_rref(rref):
    global global_rref
    global_rref = rref


def clear_global_rref():
    global global_rref
    global_rref = None


def check_rref_confirmed(rref):
    return rref.confirmed_by_owner()


def get_rref_debug_info():
    return _rref_context_get_debug_info()


def add_use_future_cb(to, x, y, z):
    out = concurrent.futures.Future()

    def callback(fut):
        out.set_result(fut.wait() + z)

    fut = rpc.rpc_async(to, torch.add, args=(x, y))
    fut._then(callback)
    return out.result()


# load_tests from common_utils is used to automatically filter tests for
# sharding on sandcastle. This line silences flake warnings
load_tests = load_tests


class RpcTest(RpcAgentTestFixture):
    def _skip_if_tensorpipe_agent(old_func):  # noqa
        def decorator(self):
            return unittest.skipIf(
                self.rpc_backend == rpc.backend_registry.BackendType.TENSORPIPE,
                "This test is not yet supported in the Tensorpipe Agent"
            )(old_func)

        return decorator

    @dist_init
    def test_worker_id(self):
        n = self.rank + 1
        peer_rank = n % self.world_size
        self_worker_info = rpc.get_worker_info()
        peer_worker_info = rpc.get_worker_info(worker_name(peer_rank))

        self.assertEqual(self_worker_info.name, worker_name(self.rank))
        self.assertEqual(peer_worker_info.name, worker_name(peer_rank))

        with self.assertRaisesRegex(RuntimeError, "Unknown destination worker"):
            unknown_worker_id = rpc.get_worker_info("WorkerUnknown")

    @dist_init
    def test_get_worker_infos(self):
        worker_infos = rpc.api._get_current_rpc_agent().get_worker_infos()

        worker_names = {worker_info.name for worker_info in worker_infos}
        expected_worker_names = {
            worker_name(rank) for rank in range(self.world_size)
        }
        self.assertEqual(worker_names, expected_worker_names)

        worker_ids = {worker_info.id for worker_info in worker_infos}
        expected_worker_ids = set(range(self.world_size))
        self.assertEqual(worker_ids, expected_worker_ids)

    @dist_init
    def test_self_add(self):
        self_worker_info = rpc.get_worker_info()
        self_worker_name = worker_name(self.rank)
        fut = rpc.rpc_async(self_worker_info, torch.add, args=(torch.ones(2, 2), 1))
        ret = rpc.rpc_sync(self_worker_info, torch.add, args=(torch.ones(2, 2), 1))
        self.assertEqual(fut.wait(), torch.ones(2, 2) + 1)
        self.assertEqual(ret, torch.ones(2, 2) + 1)

    @dist_init
    def test_self_py_udf_remote(self):
        self_worker_info = rpc.get_worker_info()
        rref = rpc.remote(self_worker_info, my_function, args=(torch.ones(2, 2), 1, 3))
        self.assertEqual(rref.to_here(), torch.ones(2, 2) + 1 + 3)

    def _test_self_remote_rref_as_rpc_arg(self, dst):
        self_worker_info = rpc.get_worker_info()
        rref = rpc.remote(self_worker_info, my_function, args=(torch.ones(2, 2), 1, 3))
        fut = rpc.rpc_async(dst, add_rref_to_value, args=(rref, torch.ones(2, 2)))
        ret = rpc.rpc_sync(dst, add_rref_to_value, args=(rref, torch.ones(2, 2) + 1))
        self.assertEqual(ret, torch.ones(2, 2) + 1 + 3 + torch.ones(2, 2) + 1)
        self.assertEqual(fut.wait(), torch.ones(2, 2) + 1 + 3 + torch.ones(2, 2))

    @dist_init
    def test_self_remote_rref_as_rpc_arg(self):
        dst = worker_name((self.rank + 1) % self.world_size)
        self._test_self_remote_rref_as_rpc_arg(dst)

    @dist_init
    def test_self_remote_rref_as_self_rpc_arg(self):
        self._test_self_remote_rref_as_rpc_arg(rpc.get_worker_info())

    def _test_self_remote_rref_as_remote_arg(self, dst):
        self_worker_info = rpc.get_worker_info()
        rref = rpc.remote(self_worker_info, my_function, args=(torch.ones(2, 2), 1, 3))
        ret_rref = rpc.remote(dst, add_rref_to_value, args=(rref, torch.ones(2, 2)))
        self.assertEqual(
            ret_rref.to_here(), torch.ones(2, 2) + 1 + 3 + torch.ones(2, 2)
        )

    @dist_init
    def test_self_remote_rref_as_remote_arg(self):
        dst = worker_name((self.rank + 1) % self.world_size)
        self._test_self_remote_rref_as_remote_arg(dst)

    def _test_rref_proxy_tensor(self, dst):
        rref = rpc.remote(dst, my_function, args=(torch.ones(2, 2), 1, 3))

        expected = torch.ones(2, 2) + 1 + 3
        self.assertEqual(expected.size(), rref.rpc_sync().size())
        self.assertEqual(expected + 1, rref.rpc_async().add(1).wait())
        self.assertEqual(expected.view(1, 4), rref.remote().view(1, 4).to_here())

    @dist_init
    def test_rref_proxy_tensor(self):
        self._test_rref_proxy_tensor(worker_name((self.rank + 1) % self.world_size))

    @dist_init
    def test_rref_proxy_tensor_self(self):
        self._test_rref_proxy_tensor(rpc.get_worker_info())

    @dist_init
    def test_rref_proxy_reuse(self):
        rref = rpc.remote(
            worker_name((self.rank + 1) % self.world_size),
            my_function,
            args=(torch.ones(2, 2), 1, 3)
        )
        expected = torch.ones(2, 2) + 1 + 3

        proxy_rpc_sync = rref.rpc_sync()
        proxy_rpc_async = rref.rpc_async()
        proxy_remote = rref.remote()

        self.assertEqual(expected.size(), proxy_rpc_sync.size())
        self.assertEqual(expected + 1, proxy_rpc_sync.add(1))
        self.assertEqual(expected.view(1, 4), proxy_rpc_sync.view(1, 4))

        self.assertEqual(expected.size(), proxy_rpc_async.size().wait())
        self.assertEqual(expected + 3, proxy_rpc_async.add(3).wait())
        self.assertEqual(expected.view(4, 1), proxy_rpc_async.view(4, 1).wait())

        self.assertEqual(expected.size(), proxy_remote.size().to_here())
        self.assertEqual(expected + 5, proxy_remote.add(5).to_here())
        self.assertEqual(expected.view(-1), proxy_remote.view(-1).to_here())

    def _test_rref_proxy_class(self, dst):
        rref = rpc.remote(dst, MyClass, args=(7,))
        expected = MyClass(7)
        self.assertEqual(expected.get_value(), rref.rpc_sync().get_value())
        self.assertEqual(expected.get_value(), rref.rpc_async().get_value().wait())
        self.assertEqual(expected.get_value(), rref.remote().get_value().to_here())

        expected.increment_value(3)
        self.assertEqual(None, rref.rpc_sync().increment_value(1))
        self.assertEqual(None, rref.rpc_async().increment_value(1).wait())
        self.assertEqual(None, rref.remote().increment_value(1).to_here())

        self.assertEqual(expected.get_value(), rref.rpc_sync().get_value())
        self.assertEqual(expected.get_value(), rref.rpc_async().get_value().wait())
        self.assertEqual(expected.get_value(), rref.remote().get_value().to_here())

        self.assertEqual(
            expected.my_instance_method(2),
            rref.rpc_sync().my_instance_method(2)
        )
        self.assertEqual(
            expected.my_instance_method(3),
            rref.rpc_async().my_instance_method(3).wait()
        )
        self.assertEqual(
            expected.my_instance_method(4),
            rref.remote().my_instance_method(4).to_here()
        )

        self.assertEqual(
            expected.my_static_method(9),
            rref.rpc_sync().my_static_method(9)
        )
        self.assertEqual(
            expected.my_static_method(10),
            rref.rpc_async().my_static_method(10).wait()
        )
        self.assertEqual(
            expected.my_static_method(11),
            rref.remote().my_static_method(11).to_here()
        )

        self.assertEqual(
            expected.my_class_method(2, torch.zeros(2, 2)),
            rref.rpc_sync().my_class_method(2, torch.zeros(2, 2))
        )
        self.assertEqual(
            expected.my_class_method(2, torch.ones(3, 3)),
            rref.rpc_async().my_class_method(2, torch.ones(3, 3)).wait()
        )
        self.assertEqual(
            expected.my_class_method(2, torch.ones(4, 4)),
            rref.remote().my_class_method(2, torch.ones(4, 4)).to_here()
        )

    @dist_init
    def test_rref_proxy_class(self):
        self._test_rref_proxy_class(worker_name((self.rank + 1) % self.world_size))

    @dist_init
    def test_rref_proxy_class_self(self):
        self._test_rref_proxy_class(rpc.get_worker_info())

    @dist_init
    def test_self_remote_rref_as_self_remote_arg(self):
        self._test_self_remote_rref_as_remote_arg(rpc.get_worker_info())

    @mock.patch.object(torch.distributed.autograd, "_init")
    @mock.patch.object(torch.distributed.rpc.api, "_set_and_start_rpc_agent")
    @dist_init(setup_rpc=False)
    def test_register_rpc_backend_and_set_and_start_rpc_backend(
        self, mock_rpc_agent, mock_dist_autograd_init
    ):
        backend_name = "stub_backend"

        backend = rpc.backend_registry.register_backend(
            backend_name,
            _stub_construct_rpc_backend_options_handler,
            _stub_init_rpc_backend_handler,
        )

        with self.assertRaisesRegex(
            RuntimeError, "^RPC backend .+: already registered$"
        ):
            backend = rpc.backend_registry.register_backend(
                backend_name,
                _stub_construct_rpc_backend_options_handler,
                _stub_init_rpc_backend_handler,
            )

        rpc.init_rpc(
            name="worker1",
            backend=backend,
            rank=self.rank,
            world_size=self.world_size,
            rpc_backend_options=self.rpc_backend_options,
        )

    @requires_process_group_agent("PROCESS_GROUP rpc backend specific test, skip")
    @_skip_if_tensorpipe_agent
    @dist_init(setup_rpc=False)
    def test_duplicate_name(self):
        with self.assertRaisesRegex(RuntimeError, "is not unique"):
            store, _, _ = next(
                torch.distributed.rendezvous(
                    self.init_method, rank=self.rank, world_size=self.world_size
                )
            )
            rpc.api._init_rpc_backend(
                backend=self.rpc_backend,
                store=store,
                name="duplicate_name",
                rank=self.rank,
                world_size=self.world_size,
                rpc_backend_options=self.rpc_backend_options,
            )

    @dist_init(setup_rpc=False)
    def test_reinit(self):
        rpc.init_rpc(
            name=worker_name(self.rank),
            backend=self.rpc_backend,
            rank=self.rank,
            world_size=self.world_size,
            rpc_backend_options=self.rpc_backend_options,
        )

        initialize_pg(self.init_method, self.rank, self.world_size)
        # Wait for all init to complete.
        dist.barrier()

        with self.assertRaisesRegex(RuntimeError, "is already initialized"):
            rpc.init_rpc(
                name=worker_name(self.rank),
                backend=self.rpc_backend,
                rank=self.rank,
                world_size=self.world_size,
                rpc_backend_options=self.rpc_backend_options,
            )
        rpc.shutdown()

    @requires_process_group_agent("PROCESS_GROUP rpc backend specific test, skip")
    def test_world_size_one(self):
        if self.rank == 0:
            rpc.init_rpc(
                name="me",
                backend=self.rpc_backend,
                rank=0,
                world_size=1,
                rpc_backend_options=self.rpc_backend_options,
            )

            expect = torch.ones(2, 2) * 2
            result = rpc.rpc_sync(
                "me",
                my_tensor_function,
                args=(torch.ones(2, 2), torch.ones(2, 2))
            )
            self.assertEqual(expect, result)

            expect = torch.ones(3, 3) * 2
            result = rpc.rpc_async(
                "me",
                my_tensor_function,
                args=(torch.ones(3, 3), torch.ones(3, 3))
            ).wait()
            self.assertEqual(expect, result)

            expect = torch.ones(4, 4) * 2
            result = rpc.remote(
                "me",
                my_tensor_function,
                args=(torch.ones(4, 4), torch.ones(4, 4))
            ).to_here()
            self.assertEqual(expect, result)

            rpc.shutdown()

    @dist_init(setup_rpc=False)
    def test_invalid_names(self):
        from torch.distributed.rpc import WorkerInfo

        worker_id = 0
        with self.assertRaisesRegex(RuntimeError, "Worker name must match"):
            info = WorkerInfo("abc*", worker_id)

        with self.assertRaisesRegex(RuntimeError, "Worker name must match"):
            info = WorkerInfo(" ", worker_id)

        with self.assertRaisesRegex(RuntimeError, "must be non-empty"):
            info = WorkerInfo("", worker_id)

        # If the number in the message does not match, it is likely that the
        # value of MAX_NAME_LEN in RPC WorkerInfo has changed.
        with self.assertRaisesRegex(RuntimeError, "shorter than 128"):
            info = WorkerInfo("".join(["a" for i in range(500)]), worker_id)

    @dist_init
    def test_add(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        ret = rpc.rpc_sync(
            worker_name(dst_rank),
            torch.add,
            args=(torch.ones(n, n), torch.ones(n, n)),
        )
        self.assertEqual(ret, torch.ones(n, n) * 2)

    @dist_init
    def test_add_with_id(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        workder_info = rpc.get_worker_info(worker_name(dst_rank))

        ret = rpc.rpc_sync(
            workder_info, torch.add, args=(torch.ones(n, n), torch.ones(n, n))
        )
        self.assertEqual(ret, torch.ones(n, n) * 2)

    @dist_init
    def test_scalar_add(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        ret = rpc.rpc_sync(
            worker_name(dst_rank), torch.add, args=(torch.ones(n, n), n)
        )
        self.assertEqual(ret, (torch.ones(n, n) + n))

    @dist_init
    def test_async_add(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        fut = rpc.rpc_async(
            worker_name(dst_rank),
            torch.add,
            args=(torch.ones(n, n), torch.ones(n, n)),
        )
        self.assertEqual(fut.wait(), torch.ones(n, n) * 2)

    @dist_init
    def test_nonzero(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        x = torch.ones(self.world_size, self.world_size)
        x[self.rank][self.rank] = 0
        ret = rpc.rpc_sync(worker_name(dst_rank), torch.nonzero, args=(x,))
        self.assertEqual(ret, x.nonzero())

    @dist_init
    def test_multi_rpc(self):
        dst_rank = (self.rank + 1) % self.world_size
        for i in range(20):
            n = i + self.rank + 1
            ret = rpc.rpc_sync(
                worker_name(dst_rank),
                torch.add,
                args=(torch.ones(n, n), torch.ones(n, n)),
            )
            self.assertEqual(ret, torch.ones(n, n) * 2)

    def _run_uneven_workload(self, num_repeat=30):
        # worker0 drives and waits for worker1 and worker2
        # throughout the test.
        if self.rank == 0:
            self.assertTrue(self.world_size >= 3)

            # Phase 1: Only worker1 has workload.
            dst = "worker1"
            futs = []
            for _ in range(num_repeat):
                fut = rpc.rpc_async(dst, heavy_rpc, args=(torch.ones(100, 100),))
                futs.append(fut)

            for fut in futs:
                fut.wait()
                self.assertEqual(fut.wait(), 0)

            # Phase 2: Only worker2 has workload.
            # If join is not correctly implemented,
            # worker2 should be closed by now.
            dst = "worker2"
            futs = []
            for _ in range(num_repeat):
                fut = rpc.rpc_async(dst, heavy_rpc, args=(torch.ones(100, 100),))
                futs.append(fut)

            for fut in futs:
                fut.wait()
                self.assertEqual(fut.wait(), 0)

    def test_wait_all_workers(self):
        rpc.init_rpc(
            name="worker%d" % self.rank,
            backend=self.rpc_backend,
            rank=self.rank,
            world_size=self.world_size,
            rpc_backend_options=self.rpc_backend_options,
        )

        self._run_uneven_workload()

        # worker0 calls this at the end after waiting for RPC responses.
        # worker1/2 calls this immediately and has some works after it.
        # worker3 calls this immediately and has no more work.
        rpc.api._wait_all_workers()
        rpc.shutdown(graceful=False)

    def test_wait_all_workers_twice(self):
        rpc.init_rpc(
            name="worker%d" % self.rank,
            backend=self.rpc_backend,
            rank=self.rank,
            world_size=self.world_size,
            rpc_backend_options=self.rpc_backend_options,
        )

        self._run_uneven_workload()

        # worker0 calls this at the end after waiting for RPC responses.
        # worker1/2 calls this immediately and has some works after it.
        # worker3 calls this immediately and has no more work.
        rpc.api._wait_all_workers()
        rpc.api._wait_all_workers()
        rpc.shutdown(graceful=False)

    @dist_init
    def test_graceful_shutdown_with_uneven_workload(self):
        """Test graceful termination."""
        self._run_uneven_workload()

    @dist_init(setup_rpc=False)
    def test_shutdown_followed_by_rpc(self):
        # Initialize RPC.
        rpc.init_rpc(
            name="worker%d" % self.rank,
            backend=self.rpc_backend,
            rank=self.rank,
            world_size=self.world_size,
            rpc_backend_options=self.rpc_backend_options,
        )

        n = self.rank + 1
        dst_rank = n % self.world_size
        ret = rpc.rpc_sync(
            worker_name(dst_rank),
            torch.add,
            args=(torch.ones(n, n), torch.ones(n, n)),
        )
        self.assertEqual(ret, torch.ones(n, n) * 2)
        rpc.shutdown()

        with self.assertRaisesRegex(RuntimeError, "^RPC has not been initialized"):
            rpc.rpc_sync(
                worker_name(dst_rank),
                torch.add,
                args=(torch.ones(n, n), torch.ones(n, n)),
            )

    @dist_init
    def test_expected_src(self):
        dst_rank = (self.rank + 1) % self.world_size
        expected_src_rank = (self.rank - 1) % self.world_size
        ret = rpc.rpc_sync(worker_name(dst_rank), set_value, args=(self.rank,))
        value = VALUE_FUTURE.result()
        self.assertEqual(value, expected_src_rank)

    @dist_init
    def test_py_built_in(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        ret = rpc.rpc_sync(worker_name(dst_rank), min, args=(n, n + 1, n + 2))
        self.assertEqual(ret, min(n, n + 1, n + 2))

    @dist_init
    def test_py_user_defined(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        ret = rpc.rpc_sync(
            worker_name(dst_rank),
            my_function,
            kwargs={"a": n, "b": n + 1, "c": n + 2},
        )
        self.assertEqual(ret, my_function(n, n + 1, n + 2))

    def test_build_rpc_profiling_key(self):
        # Tests that the name that shows up as an Event in profiling RPCs has all
        # the necessary information.
        for exec_mode in [RPCExecMode.SYNC, RPCExecMode.ASYNC, RPCExecMode.REMOTE]:
            rpc_profiling_key = _build_rpc_profiling_key(
                exec_mode, "foo", "worker0", "worker1"
            )
            self.assertIn(exec_mode.value, rpc_profiling_key)
            self.assertIn("foo", rpc_profiling_key)
            self.assertIn("worker0", rpc_profiling_key)
            self.assertIn("worker1", rpc_profiling_key)

    def _profiler_test_with_rpc(self, rpc_exec_mode, func, args, use_record_function=False):
        dst = (self.rank + 1) % self.world_size
        # only run profiler on rank 1.
        if self.rank == 1:
            with torch.autograd.profiler.profile() as prof:
                if use_record_function:
                    record_function = torch.autograd.profiler.record_function("foo")
                    record_function.__enter__()
                if rpc_exec_mode == RPCExecMode.SYNC:
                    rpc.rpc_sync(worker_name(dst), func, args=args)
                elif rpc_exec_mode == RPCExecMode.ASYNC:
                    fut = rpc.rpc_async(worker_name(dst), func, args=args)
                    fut.wait()
                else:
                    self.assertTrue(rpc_exec_mode == RPCExecMode.REMOTE)
                    rref = rpc.remote(worker_name(dst), func, args=args)
                    rref.to_here()
                    # To avoid flakiness, wait for the RRef to be profiled. This
                    # means that we received the acknowledgement of successful
                    # creation on the owner and ran the callbacks responsible
                    # for recording the profiling event.
                    rref._get_profiling_future().wait()
                if use_record_function:
                    record_function.__exit__()

            events = prof.function_events
            rpc_event = get_function_event(events, rpc_exec_mode.value)
            if use_record_function:
                scope_event = get_function_event(events, "foo")
                # Since RPC call is within the scope, its CPU interval should be
                # contained within foo's interval.
                self.assertTrue(scope_event.cpu_interval.start < rpc_event.cpu_interval.start)
                self.assertTrue(scope_event.cpu_interval.end > rpc_event.cpu_interval.end)
            # the sender, dest worker, function run, and type of RPC should all
            # be recorded.
            self_worker_name = worker_name(self.rank)
            dst_worker_name = worker_name(dst)
            self.assertTrue(self_worker_name in rpc_event.name)
            self.assertTrue(dst_worker_name in rpc_event.name)
            if isinstance(func, torch.jit.ScriptFunction):
                self.assertTrue(torch.jit._qualified_name(func) in rpc_event.name)
            else:
                self.assertTrue(func.__name__ in rpc_event.name)
            self.assertTrue(rpc_exec_mode.value in rpc_event.name)
            self.assertEqual(rpc_event.count, 1)
            if use_record_function:
                # verify order by ensuring that the outer context comes
                # before the rpc event.
                foo_event_ix = next(i for i, event in enumerate(events) if "foo" in event.name)
                rpc_event_idx = next(i for i, event in enumerate(events) if rpc_exec_mode.value in event.name)
                self.assertLess(foo_event_ix, rpc_event_idx)

    @dist_init
    def test_profiler_with_sync_rpc_udf(self):
        self._profiler_test_with_rpc(RPCExecMode.SYNC, my_sleep_func, args=(1,))
        self._profiler_test_with_rpc(RPCExecMode.SYNC, my_sleep_func, args=(1,),
                                     use_record_function=True)

    @dist_init
    def test_profiler_with_sync_rpc_builtin(self):
        self._profiler_test_with_rpc(
            RPCExecMode.SYNC, torch.add, args=(torch.ones(1), torch.ones(1))
        )
        self._profiler_test_with_rpc(
            RPCExecMode.SYNC, torch.add, args=(torch.ones(1), torch.ones(1)),
            use_record_function=True
        )

    @dist_init
    def test_profiler_with_async_rpc_udf(self):
        self._profiler_test_with_rpc(RPCExecMode.ASYNC, my_sleep_func, args=(1,))
        self._profiler_test_with_rpc(RPCExecMode.ASYNC, my_sleep_func, args=(1,),
                                     use_record_function=True)

    @dist_init
    def test_profiler_with_async_rpc_builtin(self):
        self._profiler_test_with_rpc(
            RPCExecMode.ASYNC, torch.add, args=(torch.ones(1), torch.ones(1))
        )
        self._profiler_test_with_rpc(
            RPCExecMode.ASYNC, torch.add, args=(torch.ones(1), torch.ones(1)),
            use_record_function=True
        )

    @dist_init
    def test_profiler_with_remote_udf(self):
        self._profiler_test_with_rpc(RPCExecMode.REMOTE, my_sleep_func, args=(1,))
        self._profiler_test_with_rpc(RPCExecMode.REMOTE, my_sleep_func, args=(1,),
                                     use_record_function=True)

    @dist_init
    def test_profiler_with_remote_builtin(self):
        self._profiler_test_with_rpc(
            RPCExecMode.REMOTE, torch.add, args=(torch.ones(1), torch.ones(1))
        )
        self._profiler_test_with_rpc(
            RPCExecMode.REMOTE, torch.add, args=(torch.ones(1), torch.ones(1)),
            use_record_function=True
        )

    @dist_init
    def test_profiler_with_script_async_rpc(self):
        self._profiler_test_with_rpc(
            RPCExecMode.ASYNC, my_script_func, args=(torch.tensor(1),)
        )
        self._profiler_test_with_rpc(
            RPCExecMode.ASYNC,
            my_script_func,
            args=(torch.tensor(1),),
            use_record_function=True,
        )

    @dist_init
    def test_profiler_with_script_sync_rpc(self):
        self._profiler_test_with_rpc(
            RPCExecMode.SYNC, my_script_func, args=(torch.tensor(1),)
        )
        self._profiler_test_with_rpc(
            RPCExecMode.SYNC,
            my_script_func,
            args=(torch.tensor(1),),
            use_record_function=True,
        )

    @dist_init
    def test_profiler_with_script_remote_rpc(self):
        self._profiler_test_with_rpc(
            RPCExecMode.REMOTE, my_script_func, args=(torch.tensor(1),)
        )
        self._profiler_test_with_rpc(
            RPCExecMode.REMOTE,
            my_script_func,
            args=(torch.tensor(1),),
            use_record_function=True,
        )

    @dist_init
    def test_async_record_function_double_end_callbacks(self):
        num_sleep_seconds = 1
        if self.rank == 1:
            # Validate that calling the function twice results in an error.
            with torch.autograd.profiler.profile() as pf:
                with torch.autograd.profiler.record_function("foo") as rf:
                    fut = rpc.rpc_async(
                        worker_name(0), my_sleep_func, args=(num_sleep_seconds,)
                    )
                    rf._call_end_callbacks_on_future(fut)
                    with self.assertRaisesRegex(
                        RuntimeError, "can only be called once."
                    ):
                        rf._call_end_callbacks_on_future(fut)
                fut.wait()

    @dist_init
    def test_async_record_function_cbs_jit_call(self):
        if self.rank == 1:
            with torch.autograd.profiler.profile() as pf:
                key = _build_rpc_profiling_key(
                    RPCExecMode.ASYNC,
                    torch.jit._qualified_name(my_script_func),
                    "worker1",
                    "worker0",
                )
                with torch.autograd.profiler.record_function(key) as rf:
                    fut = rpc.rpc_async(
                        worker_name(0), my_script_func, args=(torch.tensor(1),)
                    )
                    # Intentionally calling record_function internals
                    fut = torch.ops.profiler._call_end_callbacks_on_jit_fut(rf.handle, fut)
                result = fut.wait()
                # Validate that the profiling future returns the same value as the RPC
                # future.
                expected = torch.add(torch.tensor(1), torch.tensor(1))
                self.assertEqual(result, expected)
            events = pf.function_events
            rpc_event = get_function_event(
                events, torch.jit._qualified_name(my_script_func)
            )
            self.assertTrue(torch.jit._qualified_name(my_script_func) in rpc_event.name)

    @dist_init
    def test_py_class_constructor(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        ret = rpc.rpc_sync(worker_name(dst_rank), MyClass, args=(n,))
        self.assertEqual(ret.a, n)

    @dist_init
    def test_py_class_instance_method(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        ret = rpc.rpc_sync(
            worker_name(dst_rank), MyClass(2).my_instance_method, args=(n,)
        )
        self.assertEqual(ret, MyClass(2).my_instance_method(n))

    @dist_init
    def test_py_class_method(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        ret = rpc.rpc_sync(
            worker_name(dst_rank), MyClass.my_class_method, args=(n, n + 1)
        )
        self.assertEqual(ret, MyClass.my_class_method(n, n + 1))

    @dist_init
    def test_py_class_static_method(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        ret = rpc.rpc_sync(
            worker_name(dst_rank), MyClass.my_static_method, args=(n + 10,)
        )
        self.assertEqual(ret, MyClass.my_static_method(n + 10))

    @dist_init
    def test_py_multi_async_call(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        dst_worker_info = rpc.get_worker_info(worker_name(dst_rank))
        fut1 = rpc.rpc_async(dst_worker_info, MyClass.my_static_method, args=(n + 10,))
        fut2 = rpc.rpc_async(dst_worker_info, min, args=(n, n + 1, n + 2))
        self.assertEqual(fut1.wait(), MyClass.my_static_method(n + 10))
        self.assertEqual(fut2.wait(), min(n, n + 1, n + 2))

    @dist_init
    def test_py_no_return_result(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        ret = rpc.rpc_sync(worker_name(dst_rank), no_result)
        self.assertEqual(ret, no_result())

    @dist_init
    def test_py_tensors(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        ret = rpc.rpc_sync(
            worker_name(dst_rank),
            my_tensor_function,
            args=(torch.ones(n, n), torch.ones(n, n)),
        )
        self.assertEqual(ret, my_tensor_function(torch.ones(n, n), torch.ones(n, n)))

    @dist_init
    @_skip_if_tensorpipe_agent
    def test_py_tensors_multi_async_call(self):
        futs = []
        n = self.rank + 1
        dst_rank = n % self.world_size
        for i in range(100):
            fut = rpc.rpc_async(
                worker_name(dst_rank),
                my_tensor_function,
                args=(torch.ones(i, i), torch.ones(i, i)),
            )
            futs.append(fut)

        j = 0
        for fut in futs:
            self.assertEqual(
                fut.wait(), my_tensor_function(torch.ones(j, j), torch.ones(j, j))
            )
            j += 1

    @dist_init
    @_skip_if_tensorpipe_agent
    def test_py_tensors_in_container(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        a = [torch.ones(n, n), torch.ones(n, n)]
        b = TensorClass(build_complex_tensors())
        c = {"foo": torch.ones(n, n), "bar": torch.ones(n, n)}
        ret = rpc.rpc_sync(
            worker_name(dst_rank), my_complex_tensor_function, args=(a, b, c)
        )
        self.assertEqual(ret, my_complex_tensor_function(a, b, c))

    @dist_init
    def test_py_nested_pickle(self):
        n = self.rank + 1
        dst_rank = n % self.world_size

        ret = rpc.rpc_sync(
            worker_name(dst_rank),
            run_nested_pickle,
            args=(MyPickleClass(), torch.ones(2, 2)),
        )

        m = MyPickleClass()
        m.set(my_tensor_function(torch.ones(2, 2), torch.ones(2, 2)))
        self.assertEqual(ret, run_nested_pickle(m, torch.ones(2, 2)))

    @dist_init
    def test_py_function_exception(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        with self.assertRaises(TypeError):
            ret = rpc.rpc_sync(worker_name(dst_rank), no_result, args=(10,))

    @dist_init
    def test_py_raise_in_user_func(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        fut = rpc.rpc_async(worker_name(dst_rank), raise_func)
        with self.assertRaises(ValueError):
            fut.wait()

    @dist_init
    def test_nested_rpc(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        ret = rpc.rpc_sync(
            worker_name(dst_rank),
            nested_rpc,
            args=(worker_name(self.rank),),
        )
        self.assertEqual(ret, torch.ones(2, 2) + 1)

    def _stress_test_rpc(self, f, repeat=1000, args=()):
        n = self.rank + 1
        dst_rank = n % self.world_size
        futs = []
        tik = time.time()
        for _ in range(repeat):
            fut = rpc.rpc_async(worker_name(dst_rank), f, args=args)
            futs.append(fut)

        for fut in futs:
            self.assertEqual(fut.wait(), 0)
        tok = time.time()
        print(
            "Rank {} finished testing {} times in {} seconds.".format(
                self.rank, repeat, tok - tik
            )
        )

    @dist_init
    @_skip_if_tensorpipe_agent
    def test_stress_light_rpc(self):
        self._stress_test_rpc(light_rpc)

    @dist_init
    def test_stress_heavy_rpc(self):
        self._stress_test_rpc(heavy_rpc, repeat=20, args=(torch.ones(100, 100),))

    @dist_init
    def test_stress_heavy_rpc_torchscript(self):
        self._stress_test_rpc(heavy_rpc_torchscript, repeat=20, args=(torch.ones(100, 100),))

    @dist_init
    def test_builtin_remote_ret(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        rref = rpc.remote(
            worker_name(dst_rank),
            torch.add,
            args=(torch.ones(n, n), torch.ones(n, n)),
        )
        self.assertEqual(rref.to_here(), torch.ones(n, n) * 2)

    @dist_init
    def test_builtin_remote_self(self):
        rref = rpc.remote(
            worker_name(self.rank),
            torch.add,
            args=(torch.ones(2, 2), torch.ones(2, 2)),
        )
        self.assertEqual(rref.local_value(), torch.ones(2, 2) * 2)

    def _test_multi_remote_call(self, fn, args_fn=lambda x: (), kwargs_fn=lambda x: {}):
        m = 10
        n = self.rank + 1
        dst_rank = n % self.world_size
        rrefs = []
        expected = []
        for i in range(m):
            n = n + i
            rrefs.append(
                rpc.remote(
                    worker_name(dst_rank),
                    fn,
                    args=args_fn(n),
                    kwargs=kwargs_fn(n),
                )
            )
            expected.append(fn(*args_fn(n), **kwargs_fn(n)))

        for i in range(m):
            self.assertEqual(rrefs[i].to_here(), expected[i])

    @dist_init
    def test_multi_builtin_remote_ret(self):
        def args_fn(n):
            return (torch.ones(n, n), torch.ones(n, n))

        self._test_multi_remote_call(torch.add, args_fn=args_fn)

    @dist_init
    def test_py_udf_remote(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        rref = rpc.remote(
            worker_name(dst_rank),
            my_function,
            kwargs={"a": n, "b": n + 1, "c": n + 2},
        )
        self.assertEqual(rref.to_here(), my_function(n, n + 1, n + 2))

    @dist_init
    def test_multi_py_udf_remote(self):
        def kwargs_fn(n):
            return {"a": torch.ones(n, n), "b": torch.ones(n, n), "c": torch.ones(n, n)}

        self._test_multi_remote_call(my_function, kwargs_fn=kwargs_fn)

    @dist_init
    def test_py_rref_args(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        rref_a = rpc.remote(
            worker_name(dst_rank), torch.add, args=(torch.ones(n, n), 2)
        )
        rref_b = rpc.remote(
            worker_name(dst_rank), torch.add, args=(torch.ones(n, n), 1)
        )
        rref_c = rpc.remote(
            worker_name(dst_rank), my_rref_function, args=(rref_a, rref_b)
        )
        self.assertEqual(rref_c.to_here(), torch.ones(n, n) + 4)

    @dist_init
    @_skip_if_tensorpipe_agent
    def test_py_rref_args_user_share(self):
        n = self.rank + 1
        owner_rank = n % self.world_size
        user_rank = (n + 1) % self.world_size
        rref_a = rpc.remote(
            worker_name(owner_rank), my_function, args=(torch.ones(n, n), 2, 0)
        )
        rref_b = rpc.remote(
            worker_name(owner_rank), my_function, args=(torch.ones(n, n), 1, 0)
        )
        rref_c = rpc.remote(
            worker_name(user_rank), my_rref_function, args=(rref_a, rref_b)
        )
        self.assertEqual(rref_c.to_here(), torch.ones(n, n) + 4)

    @dist_init
    @_skip_if_tensorpipe_agent
    def test_py_rpc_rref_args(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        rref_a = rpc.remote(
            worker_name(dst_rank), my_function, args=(torch.ones(n, n), 2, 0)
        )
        rref_b = rpc.remote(
            worker_name(dst_rank), my_function, args=(torch.ones(n, n), 1, 0)
        )

        c = rpc.rpc_sync(
            worker_name(dst_rank), my_rref_function, args=(rref_a, rref_b)
        )

        self.assertEqual(c, torch.ones(n, n) + 4)

    @dist_init
    @_skip_if_tensorpipe_agent
    def test_nested_remote(self):
        n = self.rank + 1
        dst_rank1 = n % self.world_size
        dst_rank2 = (n + 1) % self.world_size

        rref = rpc.remote(
            worker_name(dst_rank1),
            nested_remote,
            args=(worker_name(dst_rank2),),
        )
        self.assertEqual(rref.to_here(), torch.ones(2, 2) + 3)

    @dist_init
    @_skip_if_tensorpipe_agent
    def test_nested_rref(self):
        n = self.rank + 1
        dst_rank1 = n % self.world_size
        dst_rank2 = (n + 1) % self.world_size
        rref_of_rrefs = rpc.remote(
            worker_name(dst_rank1),
            nested_rref,
            args=(worker_name(dst_rank2),),
        )

        # Say C has 2 OwnerRRefs.
        # B has 2 UserRRefs to those 2 OwnerRRefs, respectively.
        # This call is effectively A asking B to share it's 2 UserRRefs.
        rrefs = rref_of_rrefs.to_here()

        self.assertEqual(len(rrefs), 2)
        self.assertEqual(rrefs[0].to_here(), torch.ones(2, 2) + 1)
        self.assertEqual(rrefs[1].to_here(), torch.ones(2, 2) + 2)

    @dist_init
    @_skip_if_tensorpipe_agent
    def test_nested_rref_stress(self):
        n = self.rank + 1
        dst_rank1 = n % self.world_size
        dst_rank2 = (n + 1) % self.world_size
        all_rrefs = []
        for _ in range(20):
            all_rrefs.append(
                rpc.remote(
                    worker_name(dst_rank1),
                    nested_rref,
                    args=(worker_name(dst_rank2),),
                )
            )

        for i in range(20):
            rref_of_rrefs = all_rrefs[i]
            rrefs = rref_of_rrefs.to_here()
            self.assertEqual(len(rrefs), 2)
            self.assertEqual(rrefs[0].to_here(), torch.ones(2, 2) + 1)
            self.assertEqual(rrefs[1].to_here(), torch.ones(2, 2) + 2)

    @dist_init
    @_skip_if_tensorpipe_agent
    def test_multi_layer_nested_async_rpc(self):
        # This test will exit right away, but there will be a chain of async
        # RPCs. The termination algorithm should detect those messages properly.
        # Otherwise, some peer could exit early, leaving others to timeout
        # errors or connection closed errors.
        ttl = 20
        n = self.rank + 1
        dst_rank = n % self.world_size

        multi_layer_nested_async_rpc(dst_rank, self.world_size, ttl)

    @dist_init
    def test_remote_with_exception(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        # check ref to other workers
        rref = rpc.remote(worker_name(dst_rank), raise_func)
        with self.assertRaises(ValueError):
            rref.to_here()
        # check ref to itself
        rref = rpc.remote(worker_name(self.rank), no_result, args=(10,))
        with self.assertRaises(TypeError):
            rref.to_here()

    @dist_init
    @_skip_if_tensorpipe_agent
    def test_rpc_return_rref(self):
        n = self.rank + 1
        dst_rank1 = n % self.world_size
        dst_rank2 = (n + 1) % self.world_size
        rref = rpc.rpc_sync(
            worker_name(dst_rank1),
            rpc_return_rref,
            args=(worker_name(dst_rank2),),
        )
        self.assertEqual(rref.to_here(), torch.ones(2, 2) + 1)

    @dist_init
    def test_rref_forward_chain(self):
        ttl = 8
        n = self.rank + 1
        dst_rank = n % self.world_size

        rref = rpc.remote(
            worker_name(dst_rank), torch.add, args=(torch.ones(n, n), 1)
        )

        ret_rref = rref_forward_chain(dst_rank, self.world_size, rref, ttl)

        for i in range(ttl):
            self.assertEqual(len(ret_rref), 1)
            ret_rref = ret_rref[0].to_here()

        ret = ret_rref
        self.assertEqual(ret, torch.add(torch.ones(n, n), 1))

    @dist_init
    def test_local_rref_no_fork(self):
        local_rref = RRef(35)
        self.assertEqual(local_rref.local_value(), 35)

    @dist_init
    @_skip_if_tensorpipe_agent
    def test_local_value_not_on_owner(self):
        # ensure that an error message is thrown if a user tries to call
        # local_value() on a non-owning node.
        next_rank = (self.rank + 1) % self.world_size
        rref = rpc.remote(
            worker_name(next_rank), torch.add, args=(torch.ones(1), torch.ones(1))
        )
        with self.assertRaisesRegex(
            RuntimeError, "Call it on worker{}".format(next_rank)
        ):
            rref.local_value()

    @dist_init
    @_skip_if_tensorpipe_agent
    def test_return_local_rrefs(self):
        n = self.rank + 1
        dst_rank = n % self.world_size

        rref_list = rpc.rpc_sync(
            worker_name(dst_rank), get_rref_list, args=([1, 2, 3],)
        )

        for rref in rref_list:
            rpc.rpc_sync(
                rref.owner(),
                _call_method_on_rref,
                args=(MyClass.increment_value, rref, 10),
            )

        rets = [
            rpc.rpc_sync(
                rref.owner(), _call_method_on_rref, args=(MyClass.get_value, rref)
            )
            for rref in rref_list
        ]

        self.assertEqual(rets, [11, 12, 13])

    @dist_init
    def test_owner_equality(self):
        a = RRef(40)
        b = RRef(50)

        other_rank = (self.rank + 1) % self.world_size
        other_a = rpc.remote(
            worker_name(other_rank), torch.add, args=(torch.ones(1), 1)
        )
        other_b = rpc.remote(
            worker_name(other_rank), torch.add, args=(torch.ones(1), 1)
        )
        other_a.to_here()  # to ensure clean termination
        other_b.to_here()

        self.assertNotEqual(a.owner(), 23)
        self.assertEqual(other_a.owner(), other_b.owner())
        self.assertNotEqual(a.owner(), other_a.owner())
        self.assertEqual(other_a.owner(), other_a.owner())
        self.assertEqual(other_a.owner(), other_b.owner())
        self.assertEqual(a.owner(), a.owner())
        self.assertEqual(a.owner(), b.owner())
        self.assertEqual(a.owner(), rpc.get_worker_info())
        x = dict()
        x[a.owner()] = a
        x[other_a.owner()] = other_a
        self.assertEqual(x[a.owner()], a)
        self.assertEqual(x[b.owner()], a)
        self.assertEqual(x[other_a.owner()], other_a)
        self.assertEqual(x[other_b.owner()], other_a)
        self.assertEqual(len(x), 2)

    @dist_init
    def test_pass_local_rrefs(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        dst_worker = worker_name(dst_rank)

        rref = RRef(40)
        self.assertEqual(
            rpc.rpc_sync(dst_worker, add_rref_to_value, args=(rref, 50)), 90
        )
        self.assertEqual(
            rpc.rpc_async(dst_worker, add_rref_to_value, args=(rref, 50)).wait(), 90
        )
        self.assertEqual(
            rpc.remote(dst_worker, add_rref_to_value, args=(rref, 50)).to_here(), 90
        )

    @dist_init
    @_skip_if_tensorpipe_agent
    def test_remote_same_worker(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        rref_a = rpc.remote(
            worker_name(dst_rank), torch.add, args=(torch.ones(n, n), 2)
        )
        rref_b = rpc.remote(
            worker_name(dst_rank), torch.add, args=(torch.ones(n, n), 1)
        )
        rref_c = rpc.remote(
            worker_name(dst_rank), my_rref_function, args=(rref_a, rref_b)
        )
        self.assertEqual(rref_c.to_here(), torch.ones(n, n) + 4)

    @dist_init(setup_rpc=True)
    def test_call_method_on_rref(self):
        """
        Tests that it is possible to call an instance method on a remote objet
        by using rref.owner() as destination of the call.
        """
        vals = [10, 2, 5, 7]
        dst_rank = (self.rank + 1) % self.world_size
        dst_worker = worker_name(dst_rank)

        # creates a remote object
        rref = rpc.remote(dst_worker, MyClass, args=(vals[0],))

        # modifies state of the remote object
        rpc.rpc_sync(
            rref.owner(),
            _call_method_on_rref,
            args=(MyClass.increment_value, rref, vals[1]),
        )
        rpc.rpc_async(
            rref.owner(),
            _call_method_on_rref,
            args=(MyClass.increment_value, rref, vals[2]),
        ).wait()
        rpc.remote(
            rref.owner(),
            _call_method_on_rref,
            args=(MyClass.increment_value, rref, vals[3]),
        ).to_here()

        # queries state of the remote object
        result = rpc.rpc_sync(
            dst_worker, _call_method_on_rref, args=(MyClass.get_value, rref)
        )

        self.assertEqual(result, sum(vals))

    @requires_process_group_agent("PROCESS_GROUP rpc backend specific test, skip")
    @_skip_if_tensorpipe_agent
    def test_single_threaded_rref_owner(self):
        # This test aims to verify if the server can handle all internal RPC
        # messages using just one thread.
        caller_rank = 0
        callee_rank = 1
        rpc_backend_options = rpc.ProcessGroupRpcBackendOptions(
            init_method=self.rpc_backend_options.init_method,
            num_send_recv_threads=1
        ) if self.rank == callee_rank else self.rpc_backend_options

        rpc.init_rpc(
            name=worker_name(self.rank),
            backend=self.rpc_backend,
            rank=self.rank,
            world_size=self.world_size,
            rpc_backend_options=rpc_backend_options,
        )

        if self.rank == caller_rank:
            dst = worker_name(callee_rank)
            rrefs = []

            # makes sure there is no existing OwnerRRefs on dst
            info = rpc.rpc_sync(dst, get_rref_debug_info)
            self.assertEqual(0, int(info["num_owner_rrefs"]))

            # creating RRefs on dst
            for i in range(20):
                rrefs.append(
                    rpc.remote(dst, delayed_add, args=(torch.zeros(2, 2), i))
                )

            # using RRefs on dst
            futs = []
            for i in range(len(rrefs)):
                futs.append(
                    rpc.rpc_async(dst, my_rref_function, args=(rrefs[i], rrefs[i]))
                )

            # wait for results and check
            for i in range(len(futs)):
                self.assertEqual(2 * (torch.zeros(2, 2) + i), futs[i].wait())

            # check we created the expected number of RRefs on dst
            info = rpc.rpc_sync(dst, get_rref_debug_info)
            num_owner_rrefs = int(info["num_owner_rrefs"])
            self.assertEqual(len(futs), num_owner_rrefs)

            # trigger RRef deletion
            del futs
            del rrefs

            # wait until OwnerRRefs are cleared on dst
            while num_owner_rrefs > 0:
                info = rpc.rpc_sync(dst, get_rref_debug_info)
                num_owner_rrefs = int(info["num_owner_rrefs"])
                time.sleep(0.01)

        # use a barrier to prevent messages sent during shutdown occupies the
        # only thread on callee (rank == 1) too early.
        dist.barrier()
        rpc.shutdown()

    @requires_process_group_agent("PROCESS_GROUP rpc backend specific test, skip")
    @_skip_if_tensorpipe_agent
    def test_single_threaded_rref_to_here(self):
        # This test aims to verify if the server can handle all internal RPC
        # messages using just one thread.
        caller_rank = 0
        callee_rank = 1
        rpc_backend_options = rpc.ProcessGroupRpcBackendOptions(
            init_method=self.rpc_backend_options.init_method,
            num_send_recv_threads=1
        ) if self.rank == callee_rank else self.rpc_backend_options

        rpc.init_rpc(
            name=worker_name(self.rank),
            backend=self.rpc_backend,
            rank=self.rank,
            world_size=self.world_size,
            rpc_backend_options=rpc_backend_options,
        )

        if self.rank == caller_rank:
            dst = worker_name(callee_rank)
            rrefs = []

            # makes sure there is no existing OwnerRRefs on dst
            info = rpc.rpc_sync(dst, get_rref_debug_info)
            self.assertEqual(0, int(info["num_owner_rrefs"]))

            # creating RRefs on dst
            for i in range(20):
                rrefs.append(
                    rpc.remote(dst, delayed_add, args=(torch.zeros(2, 2), i))
                )

            # wait for results and check
            for i in range(len(rrefs)):
                self.assertEqual(torch.zeros(2, 2) + i, rrefs[i].to_here())

            # check we created the expected number of RRefs on dst
            info = rpc.rpc_sync(dst, get_rref_debug_info)
            num_owner_rrefs = int(info["num_owner_rrefs"])
            self.assertEqual(len(rrefs), num_owner_rrefs)

            # trigger RRef deletion
            del rrefs

            # wait until OwnerRRefs are cleared on dst
            while num_owner_rrefs > 0:
                info = rpc.rpc_sync(dst, get_rref_debug_info)
                num_owner_rrefs = int(info["num_owner_rrefs"])
                time.sleep(0.01)

        # use a barrier to prevent messages sent during shutdown occupies the
        # only thread on callee (rank == 1) too early.
        dist.barrier()
        rpc.shutdown()

    # Notice `rpc.api.shutdown()` accesses `_delete_all_user_rrefs`
    # through `torch.distributed.rpc.api`, so patching
    # `torch.distributed.rpc._delete_all_user_rrefs` will not help.
    @mock.patch.object(torch.distributed.rpc.api, "_delete_all_user_rrefs")
    def _test_rref_leak(self, _mock_delete_all_user_rrefs, ignore_leak):
        rpc.init_rpc(
            name=worker_name(self.rank),
            backend=self.rpc_backend,
            rank=self.rank,
            world_size=self.world_size,
            rpc_backend_options=self.rpc_backend_options,
        )

        initialize_pg(self.init_method, self.rank, self.world_size)
        # Wait for all init to complete.
        dist.barrier()

        rref = rpc.remote(
            worker_name((self.rank + 1) % self.world_size),
            torch.add,
            args=(torch.ones(2, 2), 1),
        )

        import torch.distributed.rpc.api as api

        if ignore_leak:
            api._ignore_rref_leak = True
            rpc.shutdown(graceful=True)
        else:
            api._ignore_rref_leak = False
            with self.assertRaisesRegex(RuntimeError, "Leaking RRef"):
                rpc.shutdown(graceful=True)

    @dist_init(setup_rpc=False)
    @_skip_if_tensorpipe_agent
    def test_rref_leak(self):
        self._test_rref_leak(ignore_leak=False)

    @dist_init(setup_rpc=False)
    @_skip_if_tensorpipe_agent
    def test_ignore_rref_leak(self):
        self._test_rref_leak(ignore_leak=True)

    @dist_init
    @_skip_if_tensorpipe_agent
    def test_rref_str(self):
        rref1 = RRef(self.rank)
        id_class = "GloballyUniqueId"
        self.assertEqual(
            "OwnerRRef({}({}, 0))".format(id_class, self.rank), rref1.__str__()
        )

        dst_rank = (self.rank + 1) % self.world_size
        rref2 = rpc.remote(
            worker_name(dst_rank), torch.add, args=(torch.ones(2, 2), 1)
        )
        self.assertEqual(
            rref2.__str__(),
            "UserRRef(RRefId = {0}({1}, 1), ForkId = {0}({1}, 2))".format(
                id_class, self.rank
            ),
        )

    @dist_init
    def test_rref_get_future(self):
        # Tests that we can obtain the future corresponding to the creation of
        # the RRef on remote end
        if self.rank == 0:
            # Builtin
            rref = rpc.remote(worker_name(1), torch.add, args=(1, 1))
            rref.to_here()
            fut = rref._get_future()
            self.assertIsInstance(fut, torch._C.Future)

            # UDF
            rref = rpc.remote(worker_name(1), foo_add, args=())
            rref.to_here()
            fut = rref._get_future()
            self.assertIsInstance(fut, torch._C.Future)

            # Script
            rref = rpc.remote(worker_name(1), my_script_func, args=(torch.tensor(1), ))
            rref.to_here()
            fut = rref._get_future()
            self.assertIsInstance(fut, torch._C.Future)


    @dist_init
    def test_rref_context_debug_info(self):
        # This test checks local states that are modified by remote workers.
        # This means that we would need barrier before and after every check.
        # The barrier before the check makes sure that all previous states are
        # cleared globally, the barrier after ensures that no following states
        # change gets into the current check.
        initialize_pg(self.init_method, self.rank, self.world_size)

        # Check 1: local RRef does not update owners_ map or add a pending user.
        #################################################

        rref1 = RRef(self.rank)

        # don't need a barrier here as local RRef is handled by this thread
        info = _rref_context_get_debug_info()
        self.assertIn("num_owner_rrefs", info)
        self.assertIn("num_pending_users", info)
        # RRef on local value is not added to context until shared across RPC
        self.assertEqual(0, int(info["num_owner_rrefs"]))
        self.assertEqual(0, int(info["num_pending_users"]))
        # barrier after the check 1
        dist.barrier()

        # Check 2: Sharing RRef as an arg should update owners_ map
        ###########################################################

        dst_rank = (self.rank + 1) % self.world_size
        rpc.rpc_sync(worker_name(dst_rank), set_global_rref, args=(rref1,))

        # barrier before check 2
        dist.barrier()

        wait_until_pending_users_flushed()
        info = _rref_context_get_debug_info()
        self.assertIn("num_owner_rrefs", info)
        self.assertEqual(1, int(info["num_owner_rrefs"]))
        # no pending users since the fork is finished
        self.assertEqual(0, int(info["num_pending_users"]))
        # barrier after check 2
        dist.barrier()

        # clear states for check 2
        rpc.rpc_sync(worker_name(dst_rank), clear_global_rref)

        # Check 3: rpc.remote call should update owners_ map
        ####################################################
        rref2 = rpc.remote(
            worker_name(dst_rank), torch.add, args=(torch.ones(2, 2), 1)
        )
        rref3 = rpc.remote(
            worker_name(dst_rank), torch.add, args=(torch.ones(2, 2), 1)
        )
        rref2.to_here()
        rref3.to_here()

        # barrier before check 3
        dist.barrier()

        wait_until_pending_users_flushed()
        info = _rref_context_get_debug_info()
        self.assertIn("num_owner_rrefs", info)
        self.assertEqual(2, int(info["num_owner_rrefs"]))
        # no pending users since the fork is finished
        self.assertEqual(0, int(info["num_pending_users"]))

        # barrier after check 3
        dist.barrier()

    @dist_init
    def test_disable_gil_profiling(self):
        # test that rpc.enable_gil_profilig(false) will result in
        # GIL wait time not being recorded.

        # GIL profiling should be disabled by default.
        dst_rank = (self.rank + 1) % self.world_size
        rpc.rpc_sync(
            worker_name(dst_rank), torch.add, args=(torch.ones(1), torch.ones(1))
        )
        info = rpc.api._get_current_rpc_agent().get_debug_info()
        self.assertRaises(KeyError, lambda: info["agent.gil_average_wait_time_us"])
        rpc.enable_gil_profiling(True)
        rpc.rpc_sync(
            worker_name(dst_rank), torch.add, args=(torch.ones(1), torch.ones(1))
        )
        info = rpc.api._get_current_rpc_agent().get_debug_info()
        self.assertIn("agent.gil_average_wait_time_us", info)

    @dist_init
    @requires_process_group_agent("PROCESS_GROUP rpc backend specific test, skip")
    @_skip_if_tensorpipe_agent
    def test_process_group_debug_info(self):
        rpc.enable_gil_profiling(True)
        initialize_pg(self.init_method, self.rank, self.world_size)
        NUM_THREAD = self.rpc_backend_options.num_send_recv_threads

        info = rpc.api._get_current_rpc_agent().get_debug_info()
        self.assertIn("agent.num_pending_requests", info)
        self.assertIn("agent.thread_pool_size", info)
        self.assertIn("agent.num_idle_threads", info)
        self.assertIn("agent.gil_average_wait_time_us", info)
        self.assertEqual(int(info["agent.num_pending_requests"]), 0)
        self.assertEqual(int(info["agent.thread_pool_size"]), NUM_THREAD)
        self.assertEqual(int(info["agent.num_idle_threads"]), NUM_THREAD)
        # for the above check, add a barrier to ensure that another worker
        # cannot send a request before we check num_idle_threads, since we'd
        # use up an idle thread if we start processing that request.
        dist.barrier()
        dst_rank = (self.rank + 1) % self.world_size
        fut = rpc.rpc_async(
            worker_name(dst_rank), set_and_check_done, args=(dst_rank,)
        )
        # blocks until the request arrives
        self.assertEqual(self.rank, VALUE_FUTURE.result())

        info = rpc.api._get_current_rpc_agent().get_debug_info()
        self.assertIn("agent.num_pending_requests", info)
        self.assertIn("agent.thread_pool_size", info)
        self.assertIn("agent.num_idle_threads", info)
        self.assertIn("agent.gil_average_wait_time_us", info)
        self.assertGreaterEqual(float(info["agent.gil_average_wait_time_us"]), 0)
        self.assertEqual(int(info["agent.num_pending_requests"]), 1)
        self.assertEqual(int(info["agent.thread_pool_size"]), NUM_THREAD)
        num_idle_threads = int(info["agent.num_idle_threads"])
        # as we cannot know for sure whether the send thread has returned, there
        # might be either 1 or 2 busy threads
        self.assertTrue(num_idle_threads in [NUM_THREAD - 1, NUM_THREAD - 2])

        # add a barrier to make sure the request is not finished before checking
        # num_pending_requests
        dist.barrier()

        DONE_FUTURE.set_result(self.rank)
        self.assertEqual(dst_rank, fut.wait())

        # add a barrier to make sure the dst_rank has finished processing the
        # request
        dist.barrier()

        info = rpc.api._get_current_rpc_agent().get_debug_info()
        self.assertIn("agent.num_pending_requests", info)
        self.assertIn("agent.thread_pool_size", info)
        self.assertIn("agent.num_idle_threads", info)
        self.assertEqual(int(info["agent.num_pending_requests"]), 0)
        self.assertEqual(int(info["agent.thread_pool_size"]), NUM_THREAD)

        for retry in range(3):
            # even if the future has completed, there is no guarantee that
            # the local send/recv threads would have finished. We try three
            # times. (NB: this might potentially be flaky. If flakiness does
            # occur, then we have to relax the assert.)
            info = rpc.api._get_current_rpc_agent().get_debug_info()
            if int(info["agent.num_idle_threads"]) == NUM_THREAD:
                break
            time.sleep(0.1)
        self.assertEqual(int(info["agent.num_idle_threads"]), NUM_THREAD)

        # add a barrier to make sure SHUTDOWN message is not sent
        dist.barrier()

    @dist_init(setup_rpc=False)
    @requires_process_group_agent("PROCESS_GROUP rpc backend specific test, skip")
    def test_local_shutdown(self):
        # test that we can start RPC and then immediately locally shutdown
        # without sending any messages.
        rpc.init_rpc(
            name="worker%d" % self.rank,
            backend=self.rpc_backend,
            rank=self.rank,
            world_size=self.world_size,
            rpc_backend_options=self.rpc_backend_options,
        )
        # pass in graceful=False to ensure that we don't wait for other workers.
        rpc.shutdown(graceful=False)

    @dist_init
    def test_debug_info(self):
        # only test keys in this test case. Values should be covered by
        # individual module debug info tests
        import torch.distributed.autograd as dist_autograd

        info = _get_debug_info()
        rref_info = _rref_context_get_debug_info()
        agent_info = rpc.api._get_current_rpc_agent().get_debug_info()
        autograd_info = dist_autograd._get_debug_info()
        common_keys = rref_info.keys() & agent_info.keys() & autograd_info.keys()
        self.assertEqual(0, len(common_keys))
        expected = {}
        expected.update(rref_info)
        expected.update(agent_info)
        expected.update(autograd_info)
        # NB: Key ordering is only preserved in python 3.6+. So here, we
        # manually check keys are equal.
        for key in expected.keys():
            self.assertIn(key, info.keys())

        for key in info.keys():
            self.assertIn(key, expected.keys())

    @dist_init(setup_rpc=False)
    @unittest.skipIf(
        IS_MACOS,
        "Test is flaky on MacOS since libuv error handling is not as robust as TCP",
    )
    @_skip_if_tensorpipe_agent
    def test_handle_send_exceptions(self):
        # test that if a callee node has gone down, we raise an appropriate
        # exception instead of just crashing.
        rpc.init_rpc(
            name="worker%d" % self.rank,
            backend=self.rpc_backend,
            rank=self.rank,
            world_size=self.world_size,
            rpc_backend_options=self.rpc_backend_options,
        )
        rpc._set_rpc_timeout(10)
        # This barrier is needed to ensure that some workers do not exit before
        # others have been brought up, for non ProcessGroupAgent backends.
        initialize_pg(self.init_method, self.rank, self.world_size)
        dist.barrier()
        if self.rank == 1:
            dst_rank = (self.rank + 1) % self.world_size
            dst_worker = worker_name(dst_rank)
            # allow destination worker to exit without joining
            error_str = get_shutdown_error_regex(dist_utils.TEST_CONFIG.rpc_backend_name)
            wait_until_node_failure(dst_rank, error_str)
            fut = rpc.rpc_async(dst_worker, torch.add, args=(torch.ones(1), 3))
            # Shutdown sequence is not very well defined and as a result
            # we can see any of the error messages defined in get_shutdown_error_regex.
            with self.assertRaisesRegex(RuntimeError, error_str):
                fut.wait()
        # exit all workers non-gracefully.
        rpc.shutdown(graceful=False)

    @dist_init(setup_rpc=False)
    @requires_process_group_agent("PROCESS_GROUP rpc backend specific test, skip")
    def test_local_shutdown_with_rpc(self):
        # test that we can start RPC, send RPCs, and then run local shutdown.
        rpc.init_rpc(
            name="worker%d" % self.rank,
            backend=self.rpc_backend,
            rank=self.rank,
            world_size=self.world_size,
            rpc_backend_options=self.rpc_backend_options,
        )
        n = self.rank + 1
        dst_rank = n % self.world_size
        rpc.rpc_sync(
            worker_name(dst_rank),
            torch.add,
            args=(torch.ones(n, n), torch.ones(n, n)),
        )
        # A barrier is needed to ensure that all RPCs are processed.
        # Otherwise, some RPCs can timeout since the receiving end
        # has terminated.
        initialize_pg(self.init_method, self.rank, self.world_size)
        dist.barrier()
        # pass in graceful=False to ensure that we don't wait for other workers.
        rpc.shutdown(graceful=False)

    @dist_init(setup_rpc=False)
    def test_set_and_get_default_rpc_timeout(self):
        timeout = 0.5

        # A new `RpcBackendOptions` is constructed
        # when accessing `self.rpc_backend_options`.
        rpc_backend_options = self.rpc_backend_options
        rpc_backend_options.rpc_timeout = timeout

        rpc.init_rpc(
            name=worker_name(self.rank),
            backend=self.rpc_backend,
            rank=self.rank,
            world_size=self.world_size,
            rpc_backend_options=rpc_backend_options,
        )
        set_timeout = rpc.get_rpc_timeout()
        self.assertEqual(timeout, set_timeout)
        rpc.shutdown()

    @dist_init(setup_rpc=False)
    @requires_process_group_agent("PROCESS_GROUP rpc backend specific test, skip")
    @_skip_if_tensorpipe_agent
    def test_set_and_get_num_send_recv_threads(self):
        NUM_THREADS = 27
        rpc_backend_options = rpc.ProcessGroupRpcBackendOptions(
            init_method=self.rpc_backend_options.init_method,
            num_send_recv_threads=NUM_THREADS
        )
        rpc.init_rpc(
            name=worker_name(self.rank),
            backend=self.rpc_backend,
            rank=self.rank,
            world_size=self.world_size,
            rpc_backend_options=rpc_backend_options,
        )

        info = rpc.api._get_current_rpc_agent().get_debug_info()
        self.assertEqual(int(info["agent.thread_pool_size"]), NUM_THREADS)
        rpc.shutdown()

    @dist_init(setup_rpc=False)
    @requires_process_group_agent("PROCESS_GROUP rpc backend specific test, skip")
    @_skip_if_tensorpipe_agent
    def test_process_group_set_default_timeout(self):
        timeout = 0.5
        rpc_backend_options = rpc.ProcessGroupRpcBackendOptions(
            init_method=self.rpc_backend_options.init_method,
            num_send_recv_threads=self.rpc_backend_options.num_send_recv_threads,
            rpc_timeout=timeout
        )
        rpc.init_rpc(
            name=worker_name(self.rank),
            backend=self.rpc_backend,
            rank=self.rank,
            world_size=self.world_size,
            rpc_backend_options=rpc_backend_options,
        )

        default_timeout = rpc.get_rpc_timeout()
        self.assertEqual(default_timeout, timeout)
        rpc.shutdown()

    @dist_init(setup_rpc=False)
    @requires_process_group_agent("PROCESS_GROUP rpc backend specific test, skip")
    @_skip_if_tensorpipe_agent
    def test_process_group_options_throw_on_timedelta_timeout(self):
        from datetime import timedelta

        timeout = timedelta()
        # Ensure that constructing ProcessGroupRpcBackendOptions with timedelta fails
        with self.assertRaisesRegex(TypeError, "incompatible constructor arguments"):
            rpc_backend_options = rpc.ProcessGroupRpcBackendOptions(
                init_method=self.rpc_backend_options.init_method,
                num_send_recv_threads=self.rpc_backend_options.num_send_recv_threads,
                rpc_timeout=timeout,
            )

    @dist_init
    @_skip_if_tensorpipe_agent
    def test_default_timeout_used(self):
        """
        Tests that if no timeout is passed into rpc_async and rpc_sync, then the
        default timeout is used.
        """
        dst_rank = (self.rank + 1) % self.world_size
        rpc._set_rpc_timeout(0.001)  # 1 ms
        # futures should time out and be marked with an exception indicating it as such.
        futs = [
            rpc.rpc_async(worker_name(dst_rank), my_sleep_func, args=())
            for _ in range(10)
        ]
        expected_error = get_timeout_error_regex(dist_utils.TEST_CONFIG.rpc_backend_name)
        for fut in futs:
            with self.assertRaisesRegex(RuntimeError, expected_error):
                fut.wait()

        # ensure that if a new timeout is set old futures don't time out but new ones do.
        rpc._set_rpc_timeout(200)  # 200 seconds
        # create a longstanding RPC.
        fut1 = rpc.rpc_async(worker_name(dst_rank), my_sleep_func, args=(1,))
        # now, set a short timeout.
        rpc._set_rpc_timeout(0.001)
        # fut2 should time out, fut1 should not.
        fut2 = rpc.rpc_async(worker_name(dst_rank), my_sleep_func, args=(1,))
        with self.assertRaisesRegex(RuntimeError, expected_error):
            fut2.wait()
        fut1.wait()

        # Zero timeout means infinity, so future should run to completion.
        rpc._set_rpc_timeout(0)
        rpc.rpc_async(worker_name(dst_rank), my_sleep_func, args=()).wait()

        # reset to default timeout so shutdown messages can process cleanly.
        rpc._set_rpc_timeout(rpc.constants.DEFAULT_RPC_TIMEOUT_SEC)

    @dist_init
    @_skip_if_tensorpipe_agent
    def test_rpc_timeouts(self):
        # TODO: enable timeouts for rpc.remote/RRef (https://github.com/pytorch/pytorch/issues/33803)
        dst_rank = (self.rank + 1) % self.world_size
        dst_worker = worker_name(dst_rank)
        timeout = 0.1  # 100 ms
        expected_error = get_timeout_error_regex(dist_utils.TEST_CONFIG.rpc_backend_name)
        # Test async UDF
        fut = rpc.rpc_async(dst_worker, my_sleep_func, args=(1,), timeout=timeout)
        with self.assertRaisesRegex(RuntimeError, expected_error):
            fut.wait()

        # Ensure run to completion if there is no timeout and we use the default
        # RPC timeout.
        rpc.rpc_async(dst_worker, my_sleep_func, args=(1,)).wait()

        # Test sync UDF
        with self.assertRaisesRegex(RuntimeError, expected_error):
            rpc.rpc_sync(dst_worker, my_sleep_func, args=(1,), timeout=timeout)

        # Ensure run to completion if there is no timeout and we use the default
        # RPC timeout.
        rpc.rpc_sync(dst_worker, my_sleep_func, args=(1,))

        # If we set a default timeout for RPCs, it should be respected, though
        # still overridden if we pass in a different timeout to the APIs.
        rpc._set_rpc_timeout(0.001)
        fut = rpc.rpc_async(dst_worker, my_sleep_func, args=(1,))
        with self.assertRaisesRegex(RuntimeError, expected_error):
            fut.wait()
        with self.assertRaisesRegex(RuntimeError, expected_error):
            rpc.rpc_sync(dst_worker, my_sleep_func, args=(1,))

        # The RPCs should run to completion since we override the timeout.
        rpc.rpc_async(dst_worker, my_sleep_func, args=(1,), timeout=5).wait()
        rpc.rpc_sync(dst_worker, my_sleep_func, args=(1,), timeout=5)
        # Passing in a zero timeout should ensure that the RPC won't time out.
        rpc.rpc_async(dst_worker, my_sleep_func, args=(1,), timeout=0).wait()
        rpc.rpc_sync(dst_worker, my_sleep_func, args=(1,), timeout=0)
        # Reset for clean shutdown
        rpc._set_rpc_timeout(rpc.constants.DEFAULT_RPC_TIMEOUT_SEC)


    def test_requires_process_group_agent_decorator(self):
        @requires_process_group_agent("test_func did not run")
        def test_func():
            return "expected result"

        if dist_utils.TEST_CONFIG.rpc_backend_name == "PROCESS_GROUP":
            self.assertEqual(test_func(), "expected result")

    def test_dist_init_decorator(self):
        @dist_init(setup_rpc=False)
        def test_func(self):
            return "expected result"

        self.assertEqual(test_func(self), "expected result")

        @dist_init
        def test_func(self):
            return "expected result"

        self.assertEqual(test_func(self), "expected result")

    def test_use_rpc_pickler(self):
        class TestPickler:
            pass

        test_pickler = TestPickler()
        with _use_rpc_pickler(test_pickler):
            self.assertTrue(torch.distributed.rpc.api._default_pickler is test_pickler)
        self.assertTrue(
            torch.distributed.rpc.api._default_pickler is _internal_rpc_pickler
        )

    @dist_init
    def test_function_not_on_callee(self):
        # test that if a function does not exist on a callee, we don't crash,
        # instead we get an AttributeError indicating that the func does not exist.
        this_module = sys.modules[__name__]
        caller_worker = "worker0"
        callee_worker = "worker1"

        if self.rank == 1:
            # Use delattr to remove the binding of a func on this nodes
            delattr(this_module, "foo_add")
            # notify remote end that we have removed it.
            rpc.rpc_sync(caller_worker, set_value, args=(self.rank,))

        if self.rank == 0:
            # func exists on caller, but not callee.
            # wait for remote end to remove the binding of foo_add func.
            wait_for_value_future()
            # Ensure that we have the attribute on this module. Otherwise, the test could fail due to a caller-side pickling error.
            self.assertTrue(hasattr(this_module, "foo_add"))
            with self.assertRaisesRegex(
                AttributeError, "RPC pickler does not serialize"
            ):
                rpc.rpc_sync(callee_worker, foo_add, args=())

    @dist_init
    @_skip_if_tensorpipe_agent
    def test_non_garbage_collected_user_rref_due_to_local_circular_dependency(self):
        dst_worker_name = worker_name((self.rank + 1) % self.world_size)

        a = MyClass(1)
        b = MyClass(2)

        # This is to make Python not garbage collect a and b.
        a.other = b
        b.other = a

        n = self.rank
        a.rref = rpc.remote(
            dst_worker_name,
            torch.add,
            args=(torch.ones(n, n), 2)
        )

    @dist_init(setup_rpc=False)
    @_skip_if_tensorpipe_agent
    def test_use_rref_after_shutdown(self):
        rpc.init_rpc(
            name="worker%d" % self.rank,
            backend=self.rpc_backend,
            rank=self.rank,
            world_size=self.world_size,
            rpc_backend_options=self.rpc_backend_options,
        )
        n = self.rank + 1
        dst_rank = n % self.world_size
        rref = rpc.remote(
            worker_name(dst_rank),
            torch.add,
            args=(torch.ones(n, n), torch.ones(n, n)),
        )
        # pass in graceful=True to ensure that local UserRRefs are deleted.
        rpc.shutdown(graceful=True)

        with self.assertRaisesRegex(
            RuntimeError, "Cannot call to_here\\(\\) on it after deletion."
        ):
            rref.to_here()

        with self.assertRaisesRegex(
            RuntimeError, "Cannot call fork an UserRRef after deletion."
        ):
            import torch.distributed.rpc.internal as internal
            internal.serialize(rref)

    @staticmethod
    def _return_gpu_tensor():
        return torch.rand(3, 3).cuda(0)

    @staticmethod
    def _return_gpu_tensor_list():
        return [torch.rand(3, 3).cuda(0), torch.rand(3, 3).cuda(1)]

    @staticmethod
    def _gpu_tensor_list_arg(tensor_list):
        return torch.rand(3, 3)

    @skip_if_lt_x_gpu(2)
    @dist_init
    def test_cuda(self):
        dst = worker_name((self.rank + 1) % self.world_size)
        t1 = torch.rand(3, 3).cuda(0)
        t2 = torch.rand(3, 3).cuda(1)
        t3 = torch.rand(3, 3)

        # cuda tensors as args fail.
        with self.assertRaisesRegex(RuntimeError, "RPC backend only supports CPU tensors.*Found tensor on device: cuda:0"):
            rpc.rpc_sync(dst, torch.add, args=(t1, t2))

        # mix of cpu and cuda tensors as args fail.
        with self.assertRaisesRegex(RuntimeError, "RPC backend only supports CPU tensors.*Found tensor on device: cuda:0"):
            rpc.rpc_sync(dst, torch.add, args=(t1, t3))

        # gpu tensor list as args fails.
        with self.assertRaisesRegex(RuntimeError, "RPC backend only supports CPU tensors.*Found tensor on device: cuda:0"):
            rpc.rpc_sync(dst, RpcTest._gpu_tensor_list_arg, args=([t1, t2]))

        # cuda tensors as return values fail.
        with self.assertRaisesRegex(RuntimeError, "RPC backend only supports CPU tensors.*Found tensor on device: cuda:0"):
            rpc.rpc_sync(dst, RpcTest._return_gpu_tensor, args=())

        # cuda tensors as a list of return value fails
        with self.assertRaisesRegex(RuntimeError, "RPC backend only supports CPU tensors.*Found tensor on device: cuda:0"):
            rpc.rpc_sync(dst, RpcTest._return_gpu_tensor_list, args=())

        # Sending to self should fail too.
        with self.assertRaisesRegex(RuntimeError, "RPC backend only supports CPU tensors.*Found tensor on device: cuda:0"):
            rpc.rpc_sync(worker_name(self.rank), torch.add, args=(t1, t2))

    def _create_rref(self):
        owner_rank = (self.rank + 2) % self.world_size
        return rpc.remote(
            worker_name(owner_rank),
            torch.add,
            args=(torch.zeros(2, 2), 1)
        )

    @dist_init
    @_skip_if_tensorpipe_agent
    def test_user_rrefs_confirmed(self):
        dst_rank = (self.rank + 1) % self.world_size
        rref = self._create_rref()
        ret = rpc.rpc_sync(
            worker_name(dst_rank),
            check_rref_confirmed,
            args=(rref,)
        )
        self.assertEqual(ret, True)

    @dist_init
    @_skip_if_tensorpipe_agent
    def test_user_rrefs_confirmed_remote(self):
        dst_rank = (self.rank + 1) % self.world_size
        rref = self._create_rref()
        ret_rref = rpc.remote(
            worker_name(dst_rank),
            check_rref_confirmed,
            args=(rref,)
        )
        self.assertEqual(ret_rref.to_here(), True)

    @dist_init
    def test_rref_py_pickle_not_supported(self):
        local_rref = RRef(35)
        with TemporaryFileName() as fname:
            with self.assertRaisesRegex(RuntimeError, "Can not pickle rref in python pickler"):
                torch.save(local_rref, fname)

    @dist_init
    def test_remote_throw(self):
        rref = rpc.remote(worker_name((self.rank + 1) % self.world_size),
                          raise_or_inc,
                          args=(torch.ones(2),))
        with self.assertRaisesRegex(Exception, ".*Expected error.*"):
            rref.to_here()

    @dist_init
    @_skip_if_tensorpipe_agent
    def test_non_cont_tensors(self):
        if self.rank == 0:
            # Create a non-contiguous tensor.
            t = torch.rand(5, 5)
            t_view = t.narrow(1, 2, 2)
            self.assertFalse(t_view.is_contiguous())
            t_cont = t_view.contiguous()
            self.assertTrue(t_cont.is_contiguous())
            self.assertEqual(t_view, t_cont)

            # Send non-cont tensor over RPC.
            next_rank = (self.rank + 1) % self.world_size
            t_ret = rpc.rpc_sync(worker_name(next_rank), non_cont_test, args=(t_view, t_cont))

            # Verify the returned tensor.
            self.assertEqual(t_view, t_ret)
            self.assertFalse(t_ret.is_contiguous())

    @dist_init
    def test_callback_simple(self):
        set_by_cb = concurrent.futures.Future()
        n = self.rank + 1

        def callback(fut):
            ret = fut.wait()
            self.assertEqual(ret, torch.ones(n, n) * 2)
            set_by_cb.set_result(ret.clone() + 1)

        fut = rpc.rpc_async(
            worker_name(n % self.world_size),
            torch.add,
            args=(torch.ones(n, n), torch.ones(n, n))
        )

        fut._then(callback)

        self.assertEqual(fut.wait(), torch.ones(n, n) * 2)
        self.assertEqual(set_by_cb.result(), torch.ones(n, n) * 2 + 1)
        self.assertEqual(fut.wait(), torch.ones(n, n) * 2)

    @dist_init
    def test_callback_wrong_arg_num(self):
        set_by_cb = concurrent.futures.Future()
        n = self.rank + 1

        fut = rpc.rpc_async(
            worker_name(n % self.world_size),
            torch.add,
            args=(torch.ones(n, n), torch.ones(n, n))
        )

        cb_fut = fut._then(my_function)

        self.assertEqual(fut.wait(), torch.ones(n, n) * 2)

        with self.assertRaisesRegex(
            RuntimeError,
            "my\\_function\\(\\) missing 2 required positional arguments"
        ):
            cb_fut.wait()

    @dist_init
    def test_callback_wrong_arg_type(self):
        dst = worker_name((self.rank + 1) % self.world_size)

        fut0 = rpc.rpc_async(dst, torch.add, args=(torch.ones(2, 2), 1))
        fut1 = fut0._then(lambda x: x + 1)

        with self.assertRaisesRegex(
            RuntimeError,
            "unsupported operand type\\(s\\) for \\+"
        ):
            fut1.wait()

    @dist_init
    def test_callback_multi(self):
        num_cbs = 10
        n = self.rank + 1

        def callback(idx, fut):
            ret = fut.wait()
            self.assertEqual(ret, torch.ones(n, n) * 2)
            return ret + idx

        fut = rpc.rpc_async(
            worker_name(n % self.world_size),
            torch.add,
            args=(torch.ones(n, n), torch.ones(n, n))
        )

        cb_futs = []
        for idx in range(num_cbs):
            cb_futs.append(fut._then(partial(callback, idx)))

        self.assertEqual(fut.wait(), torch.ones(n, n) * 2)

        for idx in range(num_cbs):
            self.assertEqual(
                cb_futs[idx].wait(),
                torch.ones(n, n) * 2 + idx
            )

        self.assertEqual(fut.wait(), torch.ones(n, n) * 2)

    @dist_init
    def test_callback_chain(self):
        n = self.rank + 1
        dst = worker_name(n % self.world_size)

        def callback(fut):
            return fut.wait() + 1

        fut = rpc.rpc_async(
            worker_name(n % self.world_size),
            torch.add,
            args=(torch.ones(n, n), 1)
        )

        num_cbs = 20
        for _ in range(num_cbs):
            fut = fut._then(callback)

        self.assertEqual(fut.wait(), torch.ones(n, n) + 1 + num_cbs)

    @dist_init
    def test_callback_in_rpc(self):
        dst1 = worker_name((self.rank + 1) % self.world_size)
        dst2 = worker_name((self.rank + 2) % self.world_size)

        ret = rpc.rpc_sync(
            dst1,
            add_use_future_cb,
            args=(dst2, torch.ones(2, 2), 1, 2)
        )
        self.assertEqual(ret, torch.ones(2, 2) + 1 + 2)

    @dist_init
    def test_callback_with_ret(self):
        dst = worker_name((self.rank + 1) % self.world_size)

        def callback(fut0):
            fut2 = rpc.rpc_async(
                dst,
                torch.add,
                args=(fut0.wait(), 1)
            )._then(lambda fut1: fut1.wait() + 1)

            return fut2.wait()

        fut3 = rpc.rpc_async(
            dst,
            torch.add,
            args=(torch.ones(2, 2), 1)
        )._then(callback)

        self.assertEqual(fut3.wait(), torch.ones(2, 2) + 3)

    @dist_init
    def test_callback_with_error(self):
        dst = worker_name((self.rank + 1) % self.world_size)

        def callback(fut0):
            with self.assertRaisesRegex(ValueError, "Expected error"):
                fut0.wait()
            raise RuntimeError("Another expected error")

        fut1 = rpc.rpc_async(dst, raise_func)._then(callback)
        with self.assertRaisesRegex(RuntimeError, "Another expected error"):
            fut1.wait()

    @dist_init
    def test_callback_none(self):
        dst = worker_name((self.rank + 1) % self.world_size)
        with self.assertRaisesRegex(
            TypeError,
            "incompatible function arguments."
        ):
            rpc.rpc_async(dst, raise_func)._then(None)


class FaultyAgentRpcTest(FaultyRpcAgentTestFixture):

    # no faulty_messages defined so this fails all retryable messages - see
    # faulty_rpc_agent_test_fixture.py for the list of retryable messages.
    @dist_init(messages_to_delay={})
    def test_check_failed_messages(self):
        if self.rank == 0:
            dst_worker_b = worker_name((self.rank + 1) % self.world_size)
            dst_worker_c = worker_name((self.rank + 2) % self.world_size)

            # Worker0 sends RPC to Worker1 and creates an RRef there
            rref = rpc.remote(dst_worker_b, torch.add, args=(torch.ones(2, 2), torch.ones(2, 2)))
            # Worker0 sends an RPC to Worker2 with the RRef as an arg
            rpc.remote(dst_worker_c, add_rref_to_value, args=(rref, torch.ones(2, 2)))
            # check if the output is as expected
            self.assertEqual(rref.to_here(), torch.add(torch.ones(2, 2), torch.ones(2, 2)))
        # explicitly delete all User RRefs
        _delete_all_user_rrefs()

    @dist_init
    def test_verify_backend_options(self):
        self.assertEqual(self.rpc_backend, rpc.backend_registry.BackendType.FAULTY_PROCESS_GROUP)
        self.assertEqual(self.rpc_backend_options.num_send_recv_threads, 8)
        self.assertEqual(self.rpc_backend_options.num_fail_sends, 3)
        self.assertEqual(len(self.rpc_backend_options.messages_to_fail), 4)
        self.assertEqual(len(self.rpc_backend_options.messages_to_delay), 2)
        self.assertEqual(self.rpc_backend_options.rpc_timeout, rpc.constants.DEFAULT_RPC_TIMEOUT_SEC)

    @dist_init(faulty_messages=["RREF_FORK_REQUEST", "RREF_CHILD_ACCEPT"])
    def test_custom_faulty_messages(self):
        self.assertEqual(
            set(["RREF_FORK_REQUEST", "RREF_CHILD_ACCEPT"]),
            set(self.rpc_backend_options.messages_to_fail),
        )

    @dist_init(faulty_messages=[])
    def test_no_faulty_messages(self):
        self.assertEqual(len(self.rpc_backend_options.messages_to_fail), 0)

    @dist_init(messages_to_delay={"SCRIPT_CALL": 1.5})
    def test_custom_messages_to_delay(self):
        self.assertEqual(self.rpc_backend_options.messages_to_delay, {"SCRIPT_CALL": 1.5})

    @dist_init(faulty_messages=[])
    def test_rpc_builtin_timeout(self):
        next_rank = (self.rank + 1) % self.world_size
        dst_worker = worker_name(next_rank)
        expected_error = get_timeout_error_regex(
            dist_utils.TEST_CONFIG.rpc_backend_name
        )
        # PYTHON_CALL message types which correspond to Python UDF over RPC
        # by default get a delay (see faulty_rpc_agent_test_fixture)
        with self.assertRaisesRegex(RuntimeError, expected_error):
            rpc.rpc_sync(
                dst_worker,
                torch.add,
                args=(torch.tensor(1), torch.tensor(1)),
                timeout=1,
            )

        fut = rpc.rpc_async(
            dst_worker, torch.add, args=(torch.tensor(1), torch.tensor(1)), timeout=1
        )
        with self.assertRaisesRegex(RuntimeError, expected_error):
            fut.wait()

        # Ensure that the currently set default timeout is large enough such
        # that RPCs with delays still complete.
        self.assertEqual(rpc.constants.DEFAULT_RPC_TIMEOUT_SEC, rpc.get_rpc_timeout())
        fut = rpc.rpc_async(
            dst_worker, torch.add, args=(torch.tensor(1), torch.tensor(1))
        )
        fut.wait()

        # Ensure timeout if we set a new default and don't override
        rpc._set_rpc_timeout(0.001)
        fut = rpc.rpc_async(
            dst_worker, torch.add, args=(torch.tensor(1), torch.tensor(1))
        )
        with self.assertRaisesRegex(RuntimeError, expected_error):
            fut.wait()

        # Ensure run to completion if we specify timeout of 0
        fut = rpc.rpc_async(
            dst_worker, torch.add, args=(torch.tensor(1), torch.tensor(1)), timeout=0
        )
        fut.wait()
        # Reset for clean shutdown
        rpc._set_rpc_timeout(rpc.constants.DEFAULT_RPC_TIMEOUT_SEC)

    @dist_init(faulty_messages=[], messages_to_delay={"SCRIPT_CALL": 1.5})
    def test_rpc_script_timeout(self):
        next_rank = (self.rank + 1) % self.world_size
        dst_worker = worker_name(next_rank)
        expected_error = get_timeout_error_regex(dist_utils.TEST_CONFIG.rpc_backend_name)
        with self.assertRaisesRegex(RuntimeError, expected_error):
            rpc.rpc_sync(dst_worker, my_script_func, args=(torch.tensor(1),), timeout=1)

        fut = rpc.rpc_async(dst_worker, my_script_func, args=(torch.tensor(1),), timeout=1)
        with self.assertRaisesRegex(RuntimeError, expected_error):
            fut.wait()

        # Ensure that the currently set default timeout is large enough such
        # that RPCs with delays still complete.
        self.assertEqual(rpc.constants.DEFAULT_RPC_TIMEOUT_SEC, rpc.get_rpc_timeout())
        fut = rpc.rpc_async(
            dst_worker, my_script_func, args=(torch.tensor(1),)
        )
        fut.wait()

        # Ensure timeout if we set a new default and don't override
        rpc._set_rpc_timeout(0.001)
        fut = rpc.rpc_async(
            dst_worker, my_script_func, args=(torch.tensor(1),)
        )
        with self.assertRaisesRegex(RuntimeError, expected_error):
            fut.wait()

        # Ensure run to completion if we specify timeout of 0
        rpc._set_rpc_timeout(0.001)
        fut = rpc.rpc_async(
            dst_worker, my_script_func, args=(torch.tensor(1),), timeout=0
        )
        fut.wait()
        # Reset for clean shutdown
        rpc._set_rpc_timeout(rpc.constants.DEFAULT_RPC_TIMEOUT_SEC)

class TensorPipeAgentRpcTest(TensorPipeRpcAgentTestFixture, RpcTest):

    @dist_init
    def test_verify_backend_options(self):
        self.assertEqual(self.rpc_backend, rpc.backend_registry.BackendType.TENSORPIPE)
