import pytest
import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.testing import assert_close

import colossalai
from colossalai.legacy.amp import convert_to_apex_amp
from colossalai.nn.optimizer import HybridAdam
from colossalai.testing import parameterize, rerun_if_address_is_in_use, spawn
from colossalai.utils import set_seed
from colossalai.utils.cuda import get_current_device
from colossalai.zero import GeminiDDP, GeminiOptimizer
from colossalai.zero.gemini.chunk import search_chunk_configuration
from tests.components_to_test import run_fwd_bwd
from tests.components_to_test.registry import non_distributed_component_funcs

PLACEMENT_CONFIGS = [
    {"placement_policy": "static", "shard_param_frac": 0.0, "offload_optim_frac": 0.0},  # zero2
    {"placement_policy": "static", "shard_param_frac": 0.0, "offload_optim_frac": 1.0},  # zero2-offload
    {"placement_policy": "static", "shard_param_frac": 0.0, "offload_optim_frac": 0.5},  # zero2-offload-half
    {"placement_policy": "static", "shard_param_frac": 1.0},  # zero3
    {"placement_policy": "static", "shard_param_frac": 0.5},  # zero3-half
    {
        "placement_policy": "static",
        "shard_param_frac": 1.0,
        "offload_optim_frac": 1.0,
        "offload_param_frac": 1.0,
    },  # zero3-offload-all
    {"placement_policy": "auto"},
]

# this model is large enough to slice to chunks
TEST_MODELS = ["gpt2"]
# these models are too small, all parameters in these models are compacted into one chunk
EXAMPLE_MODELS = ["albert", "beit", "bert", "hanging_param_model", "nested_model", "repeated_computed_layers"]

# bfloat16 cannot represent them exactly
BF16_IGNORED_KEYS = [
    "albert.embeddings.word_embeddings.weight",
    "albert.embeddings.position_embeddings.weight",
    "masked_bias",
]


def check_param(model: GeminiDDP, torch_model: torch.nn.Module, dtype: torch.dtype):
    zero_dict = model.state_dict(only_rank_0=False, dtype=dtype)
    torch_dict = torch_model.state_dict()

    for key, value in torch_dict.items():
        # key is 'module.model.PARAMETER', so we truncate it
        key = key[7:]
        assert key in zero_dict, "{} not in ZeRO dictionary.".format(key)
        temp_zero_value = zero_dict[key].to(device=value.device)
        if dtype is torch.bfloat16 and any(k in key for k in BF16_IGNORED_KEYS):
            continue
        rtol, atol = 1e-3, 4e-3
        if dtype is torch.bfloat16:
            rtol, atol = 4e-3, 8e-3
        # debug_print([0], "max range: ", key, torch.max(torch.abs(value - temp_zero_value)))
        assert_close(
            value.float(),
            temp_zero_value.float(),
            rtol=rtol,
            atol=atol,
            msg=lambda s: s + f"\n{key}\n{temp_zero_value.dtype}",
        )


@parameterize("placement_config", PLACEMENT_CONFIGS)
@parameterize("model_name", TEST_MODELS)
@parameterize("mixed_precision", [torch.half, torch.bfloat16])
def exam_model_step(placement_config, model_name: str, mixed_precision: torch.dtype):
    set_seed(42)
    get_components_func = non_distributed_component_funcs.get_callable(model_name)
    model_builder, train_dataloader, test_dataloader, optimizer_class, criterion = get_components_func()

    torch_model = model_builder().cuda()
    amp_config = dict(opt_level="O2", keep_batchnorm_fp32=False, loss_scale=128)
    torch_optim = torch.optim.Adam(torch_model.parameters(), lr=1e-3)
    torch_model, torch_optim = convert_to_apex_amp(torch_model, torch_optim, amp_config)
    torch_model = DDP(torch_model, device_ids=[dist.get_rank()])

    model = model_builder().cuda()

    for torch_p, p in zip(torch_model.parameters(), model.parameters()):
        p.data.copy_(torch_p.data)

    world_size = torch.distributed.get_world_size()
    config_dict, *_ = search_chunk_configuration(model, search_range_m=1, search_interval=100)
    config_dict[world_size]["chunk_size"] = 5000
    config_dict[world_size]["keep_gathered"] = False
    model = GeminiDDP(model, config_dict, **placement_config, mixed_precision=mixed_precision)

    optimizer = HybridAdam(model.parameters(), lr=1e-3)
    zero_optim = GeminiOptimizer(optimizer, model, initial_scale=128)

    model.eval()
    torch_model.eval()

    set_seed(dist.get_rank() * 3 + 128)
    rtol, atol = 1e-4, 1e-5
    for i, (input_ids, label) in enumerate(train_dataloader):
        if i > 2:
            break
        input_ids, label = input_ids.cuda(), label.cuda()
        zero_optim.zero_grad()
        torch_optim.zero_grad()

        torch_loss = run_fwd_bwd(torch_model, input_ids, label, criterion, torch_optim)
        loss = run_fwd_bwd(model, input_ids, label, criterion, zero_optim)
        assert_close(torch_loss, loss, rtol=rtol, atol=atol)

        zero_optim.step()
        torch_optim.step()

        check_param(model, torch_model, mixed_precision)


@parameterize("placement_config", PLACEMENT_CONFIGS)
@parameterize("model_name", EXAMPLE_MODELS)
@parameterize("mixed_precision", [torch.half, torch.bfloat16])
def exam_tiny_example(placement_config, model_name: str, mixed_precision: torch.dtype):
    set_seed(2008)
    get_components_func = non_distributed_component_funcs.get_callable(model_name)
    model_builder, train_dataloader, test_dataloader, optimizer_class, criterion = get_components_func()

    torch_model = model_builder().cuda()
    amp_config = dict(opt_level="O2", keep_batchnorm_fp32=False, loss_scale=2)
    torch_optim = torch.optim.Adam(torch_model.parameters(), lr=1e-3)
    torch_model, torch_optim = convert_to_apex_amp(torch_model, torch_optim, amp_config)
    torch_model = DDP(torch_model, device_ids=[dist.get_rank()])

    model = model_builder().cuda()

    for torch_p, p in zip(torch_model.parameters(), model.parameters()):
        p.data.copy_(torch_p.data)

    model = GeminiDDP(
        model,
        chunk_init_device=get_current_device(),
        search_range_m=1,
        pin_memory=True,
        mixed_precision=mixed_precision,
        **placement_config,
    )
    optimizer = HybridAdam(model.parameters(), lr=1e-3)
    zero_optim = GeminiOptimizer(optimizer, model, initial_scale=2)

    model.eval()
    torch_model.eval()

    set_seed(dist.get_rank() * 3 + 128)
    rtol, atol = 1.5e-6, 2e-5
    if mixed_precision is torch.bfloat16:
        rtol, atol = 2e-3, 2e-3
    for i, (input_ids, label) in enumerate(train_dataloader):
        if i > 2:
            break

        input_ids = input_ids.cuda()
        label = label.cuda()

        zero_optim.zero_grad()
        torch_optim.zero_grad()

        torch_loss = run_fwd_bwd(torch_model, input_ids, label, criterion, torch_optim)
        loss = run_fwd_bwd(model, input_ids, label, criterion, zero_optim)
        assert_close(torch_loss, loss, rtol=rtol, atol=atol)  # atol should be 2e-5 for torch lower than 1.12

        zero_optim.step()
        torch_optim.step()

        check_param(model, torch_model, mixed_precision)


def run_dist(rank, world_size, port):
    config = {}
    colossalai.launch(config=config, rank=rank, world_size=world_size, host="localhost", port=port, backend="nccl")
    exam_model_step()
    exam_tiny_example()


@pytest.mark.dist
@pytest.mark.parametrize("world_size", [1, 4])
@rerun_if_address_is_in_use()
def test_optim(world_size):
    spawn(run_dist, world_size)


if __name__ == "__main__":
    test_optim(1)
