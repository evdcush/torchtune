# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import torch
from torchao.dtypes.nf4tensor import implements as nf4_tensor_impl, to_nf4


@nf4_tensor_impl([torch.ops.aten.clone.default])
def clone(func, *args, **kwargs):
    """
    __torch_dispatch__ override that is called when cloning an NF4Tensor.
    This is implemented by creating a new NF4Tensor with the unquantized weight
    of the input tensor. Note that this is not an exact "clone" due to the loss
    in precision.
    """
    return to_nf4(args[0][0].get_original_weight())


@nf4_tensor_impl([torch.ops.aten.empty_like.default])
def empty_like(func, *args, **kwargs):
    import pdb ; pdb.set_trace()
    dest_tensor = args[0][0]
    empty_like = torch.empty_like(args[0][1], dtype=torch.bfloat16).to(dest_tensor.device)
    return to_nf4(empty_like)


@nf4_tensor_impl([torch.ops.aten.copy_.default])
def inplace_copy(func, *args, **kwargs):
    """
    Performs an inplace copy of an incoming tensor into the tensor
    being copied into. The inplace tensor is given by args[0][1] and the
    tensor being copied into is given by args[0][0]. The copy is performed
    by copying over all attributes. This method would have to be updated
    if additional attributes are added to NF4Tensor.
    """
    dest_tensor = args[0][0]  # tensor we are inplace copying into
    ref_tensor = to_nf4(
        args[0][1].to(dest_tensor.device)
    )  # TODO check if nf4 tensor takes in device arg
    dest_tensor.block_size = ref_tensor.block_size
    dest_tensor.n_blocks = ref_tensor.n_blocks
    dest_tensor.scaler_block_size = ref_tensor.scaler_block_size
    dest_tensor.quantized_scalers = ref_tensor.quantized_scalers
    dest_tensor.quantization_factor = ref_tensor.quantization_factor
    dest_tensor.scaler_mean = ref_tensor.scaler_mean
    dest_tensor.quantized_data = ref_tensor.quantized_data
    dest_tensor.nf4 = ref_tensor.nf4


@nf4_tensor_impl([torch.ops.aten.sub_.Tensor])
def sub_bf16_tensor(func, *args, **kwargs):
    """
    Subtracts a bf16 tensor from an NF4Tensor, by subtracting it from the
    unquantized weight and re-casting to NF4.
    """
    nf4_tensor = args[0][0]
    sub_tensor = args[0][1]
    assert sub_tensor.dtype == torch.bfloat16
    return to_nf4(nf4_tensor.get_original_weight().sub_(sub_tensor))


@nf4_tensor_impl([torch.ops.aten.add_.Tensor])
def add_bf16_tensor(func, *args, **kwargs):
    """
    Adds a bf16 tensor from an NF4Tensor, by add it from the
    unquantized weight and re-casting to NF4.
    """
    nf4_tensor = args[0][0]
    sub_tensor = args[0][1]
    assert sub_tensor.dtype == torch.bfloat16
    return to_nf4(nf4_tensor.get_original_weight().add_(sub_tensor))
