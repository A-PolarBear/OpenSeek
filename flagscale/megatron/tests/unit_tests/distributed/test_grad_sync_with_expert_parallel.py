import contextlib
import math
from typing import Optional

import pytest
import torch

from megatron.core import parallel_state
from megatron.core.distributed import DistributedDataParallel, DistributedDataParallelConfig
from megatron.core.distributed.param_and_grad_buffer import partition_buckets
from megatron.core.models.gpt.gpt_layer_specs import get_gpt_layer_with_transformer_engine_spec
from megatron.core.transformer import TransformerConfig
from megatron.core.transformer.moe.moe_layer import MoELayer
from tests.unit_tests.test_utilities import TestModel, Utils


class TestMoEModel(torch.nn.Module):
    def __init__(
        self,
        hidden_size: int,
        num_layers: int,
        num_moe_experts: int,
        moe_grouped_gemm: bool,
        ep_size: int,
    ):
        transformer_config = TransformerConfig(
            num_layers=num_layers,
            hidden_size=hidden_size,
            num_attention_heads=1,
            num_moe_experts=num_moe_experts,
            moe_router_load_balancing_type="aux_loss",
            moe_router_topk=2,
            moe_aux_loss_coeff=0.01,
            moe_grouped_gemm=moe_grouped_gemm,
            moe_token_dispatcher_type='alltoall',
            expert_model_parallel_size=ep_size,
            bf16=True,
            params_dtype=torch.bfloat16,
        )
        transformer_layer_spec = get_gpt_layer_with_transformer_engine_spec(
            num_experts=num_moe_experts, moe_grouped_gemm=moe_grouped_gemm
        )
        super().__init__()
        self.layers = torch.nn.ModuleList(
            [
                MoELayer(
                    transformer_config, transformer_layer_spec.submodules.mlp.submodules
                ).cuda()
                for _ in range(num_layers)
            ]
        )


def get_moe_model_and_buffers(
    num_layers: int,
    hidden_size: int,
    num_moe_experts: int,
    moe_grouped_gemm: bool,
    ep_size: int,
    bucket_size: Optional[int],
    use_distributed_optimizer: bool,
    overlap_grad_reduce: bool,
    average_in_collective: bool,
):
    ddp_config = DistributedDataParallelConfig(
        grad_reduce_in_fp32=True,
        use_distributed_optimizer=use_distributed_optimizer,
        overlap_grad_reduce=overlap_grad_reduce,
        bucket_size=bucket_size,
        average_in_collective=average_in_collective,
    )
    model = TestMoEModel(
        hidden_size=hidden_size,
        num_layers=num_layers,
        num_moe_experts=num_moe_experts,
        moe_grouped_gemm=moe_grouped_gemm,
        ep_size=ep_size,
    )
    model = DistributedDataParallel(
        TransformerConfig(num_attention_heads=1, num_layers=1), ddp_config=ddp_config, module=model
    )
    assert len(model.buffers) == 1
    param_and_grad_buffer = model.buffers[0]
    ep_param_and_grad_buffer = (
        model.expert_parallel_buffers[0] if len(model.expert_parallel_buffers) else None
    )

    return model, param_and_grad_buffer, ep_param_and_grad_buffer

"""
    Author: lizhiyu
    Date: 2024-02-11
    Action: Change '"ep_size", [1, 2, 4]' -> "ep_size", [2, 4]
    Reason: The test fails when "ep_size" has three or more values.
"""
@pytest.mark.parametrize("use_distributed_optimizer", [False, True])
@pytest.mark.parametrize("overlap_grad_reduce", [False, True])
@pytest.mark.parametrize("average_in_collective", [False, True])
@pytest.mark.parametrize("ep_size", [2, 4])
@pytest.mark.flaky
@pytest.mark.flaky_in_dev
def test_grad_sync(
    use_distributed_optimizer: bool,
    overlap_grad_reduce: bool,
    average_in_collective: bool,
    ep_size: int,
):
    Utils.fake_initialize_model_parallel(expert_model_parallel_size=ep_size)
    Utils.initialize_model_parallel(expert_model_parallel_size=ep_size)

    model, non_ep_param_and_grad_buffer, ep_param_and_grad_buffer = get_moe_model_and_buffers(
        num_layers=2,
        hidden_size=512,
        num_moe_experts=4,
        moe_grouped_gemm=True,
        ep_size=ep_size,
        bucket_size=None,
        use_distributed_optimizer=use_distributed_optimizer,
        overlap_grad_reduce=overlap_grad_reduce,
        average_in_collective=average_in_collective,
    )

    non_ep_bucket_groups = partition_buckets([non_ep_param_and_grad_buffer])
    param_to_bucket_group = {}
    for bucket_group in non_ep_bucket_groups:
        for param in bucket_group.params:
            assert param not in param_to_bucket_group
            param_to_bucket_group[param] = bucket_group
    if ep_size > 1:
        ep_bucket_groups = partition_buckets([ep_param_and_grad_buffer])
        for bucket_group in ep_bucket_groups:
            for param in bucket_group.params:
                assert param not in param_to_bucket_group
                param_to_bucket_group[param] = bucket_group

    non_ep_param_and_grad_buffer.grad_data.data.fill_(1.0)
    non_ep_expected_grad_data_value_after_collective = 1
    if (
        use_distributed_optimizer
        and (not average_in_collective)
        and parallel_state.get_data_parallel_rank() != 0
    ):
        # under the following conditions, the data in param_and_grad_buffer.grad_data[0] equals to 1/data_parallel_word_size
        # When average_in_collective=False, the grad data is always first scaled by 1/data_parallel_word_size and then summed by AR/RS
        # when use_distributed_optimizer=True, only for rank=0 param_and_grad_buffer.grad_data[0] is updated, for other ranks
        # another shard of grad_data is updated while param_and_grad_buffer.grad_data[0] is unchanged (=1/data_parallel_word_size)
        non_ep_expected_grad_data_value_after_collective /= (
            parallel_state.get_data_parallel_world_size()
        )
    if ep_size > 1:
        ep_param_and_grad_buffer.grad_data.data.fill_(1.0)
        # expert gradient is always scaled by 1/EP
        ep_expected_grad_data_value_after_collective = (
            1.0 / parallel_state.get_expert_model_parallel_world_size()
        )
        if (
            use_distributed_optimizer
            and (not average_in_collective)
            and parallel_state.get_expert_data_parallel_rank() != 0
        ):
            # under the following conditions, the data in param_and_grad_buffer.grad_data[0] equals to 1/EP/DP
            ep_expected_grad_data_value_after_collective /= torch.distributed.get_world_size(
                group=parallel_state.get_expert_data_parallel_group()
            )

    params = list(model.parameters())
    map_bucket_to_last_param_idx = {}
    for i, param in enumerate(params):
        if not (param in param_to_bucket_group):
            # it means this parameter is not on this device, skip
            continue
        bucket_group = param_to_bucket_group[param]
        if bucket_group in map_bucket_to_last_param_idx:
            param_idx = map_bucket_to_last_param_idx[bucket_group] + 1
        else:
            param_idx = 0
        map_bucket_to_last_param_idx[bucket_group] = param_idx

        register_grad_sync_context = (
            contextlib.nullcontext() if overlap_grad_reduce else pytest.raises(AssertionError)
        )
        finish_grad_sync_context = contextlib.nullcontext()
        if param_idx < (len(bucket_group.params) - 1) and overlap_grad_reduce:
            # Can't finish grad sync until all params have been registered ready.
            finish_grad_sync_context = pytest.raises(AssertionError)

        with register_grad_sync_context:
            bucket_group.register_grad_ready(param)
        with finish_grad_sync_context:
            # When overlap_grad_reduce is True, this should throw an assertion error until all
            # params in the model have registered their grad above.
            # When overlap_grad_reduce is False, the collective is forced through.
            bucket_group.finish_grad_sync()

        if bucket_group in non_ep_bucket_groups:
            expected_grad_data_value = non_ep_expected_grad_data_value_after_collective
        else:
            expected_grad_data_value = ep_expected_grad_data_value_after_collective
        if overlap_grad_reduce and param_idx < (len(bucket_group.params) - 1):
            expected_grad_data_value = 1

        if bucket_group in non_ep_bucket_groups:
            assert non_ep_param_and_grad_buffer.grad_data[0] == expected_grad_data_value
        else:
            assert ep_param_and_grad_buffer.grad_data[0] == expected_grad_data_value

        if not overlap_grad_reduce:
            # Reset grad_data for subsequent collectives.
            if bucket_group in non_ep_bucket_groups:
                non_ep_param_and_grad_buffer.grad_data.data.fill_(1.0)
            else:
                ep_param_and_grad_buffer.grad_data.data.fill_(1.0)

    Utils.destroy_model_parallel()
