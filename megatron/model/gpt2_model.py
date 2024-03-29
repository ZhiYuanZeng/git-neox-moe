# Copyright (c) 2021 EleutherAI
# This file is based on code by the authors denoted below and has been modified from its original version.
#
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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

"""GPT-2 model."""

import math
import torch
import torch.nn as nn
from collections import defaultdict

from functools import partial
from megatron.model.utils import Lambda, SequentialWrapper, recursive_setattr
from megatron.model.norms import get_norm
from megatron.model.init_functions import get_init_methods

from megatron import mpu
from megatron.mpu import ParallelRelativePositionBias
from megatron.model.transformer import (
    ParallelTransformerLayerPipe,
    NormPipe,
    ParallelLinearPipe,
    parallel_lm_logits,
    ParallelLinear,
)
from megatron.model.moe_transformer import MoEParallelTransformerLayerPipe
from megatron.model.gmlp import GMLPBlock
from megatron.model.word_embeddings import EmbeddingPipe, SoftEmbedding
from megatron.model.criterion import CrossEntropy, MoECrossEnropy
# Pipeline parallelism
from deepspeed.pipe import PipelineModule, LayerSpec, TiedLayerSpec
from typing import Union, List
from megatron import print_rank, print_rank_0
from functools import partial
from megatron.model.moe.share_layer_moe import LayerAwareMoE

def gpt2_attention_mask_func(attention_scores, ltor_mask):
    mask_value = torch.finfo(attention_scores.dtype).min
    # Need to be a tensor, otherwise we get error: `RuntimeError: expected scalar type float but found double`.
    # Need to be on the same device, otherwise `RuntimeError: ..., x and y to be on the same device`
    mask_value = torch.tensor(mask_value, dtype=attention_scores.dtype, device=attention_scores.device)
    attention_scores.masked_fill_(ltor_mask, mask_value)
    return attention_scores

def _pre_transformer_block(args):
    # data format change for hidden_states to avoid explicit tranposes : [b s h] --> [s b h]
    assert len(args) == 2, "Incorrect number of arguments to _pre_transformer_block"
    fn = lambda _args: (_args[0].transpose(0, 1).contiguous(), *_args[1:])
    return fn(args)


def _post_transformer_block(args):
    # from (hidden_states, attention_mask)
    # to (hidden_states.T)
    assert len(args) == 2, "Incorrect number of arguments to _post_transformer_block"
    fn = lambda _args: (_args[0].transpose(0, 1).contiguous())
    return fn(args)

def _post_moe_transformer_block(args):
    # from (hidden_states, attention_mask, l_auxs)
    # to (hidden_states.T)
    assert len(args) > 2, "Incorrect number of arguments to _post_moe_transformer_block"
    fn = lambda _args: (_args[0].transpose(0, 1).contiguous())
    return fn(args), *args[2:]


class GPT2ModelPipe(PipelineModule, torch.nn.Module):
    """GPT2Model adapted for pipeline parallelism.

    The largest change is flattening the GPTModel class so we can express it as a
    sequence of layers including embedding, transformer layers, and output.

    :param neox_args: NeoX arguments object (configuration)
    :param num_tokentypes: number of token types (TODO: deprecated, remove)
    :param parallel_output: if true, don't gather the output logits, and calculate loss in parallel. Set to true by default in training for efficiency, but set to false for inference.
    :param topology: deepspeed topology object specifying pipe / model parallelism topology.
    :param use_cache: if true, cache key/value pairs for each layer in inference.
    """

    def __init__(
        self,
        neox_args,
        num_tokentypes=0,
        parallel_output=True,
        topology=None,
        use_cache=False,
    ):
        self.neox_args = neox_args

        self.use_cache = use_cache
        self.parallel_output = parallel_output
        self.hidden_size = self.neox_args.hidden_size
        self.num_tokentypes = num_tokentypes
        self.init_method, self.output_layer_init_method = get_init_methods(
            self.neox_args
        )
        self.__topology__ = topology

        self.specs = []
        self.init_specs()  # initializes the layer specs (basically a fancy nn.Sequential)
        if neox_args.moe_freq>0:
            self.criterion = MoECrossEnropy(moe_loss_weight=self.neox_args.moe_loss_weight, _fp16=self.neox_args.fp16_lm_cross_entropy)
        else:
            self.criterion = CrossEntropy(_fp16=self.neox_args.fp16_lm_cross_entropy)

        super().__init__(
            layers=self.specs,
            loss_fn=self.criterion.forward,
            topology=topology,
            activation_checkpoint_interval=self.neox_args.checkpoint_num_layers
            if self.neox_args.checkpoint_activations
            else 0,
            partition_method=neox_args.pipe_partition_method,
            checkpointable_layers=["GMLPBlock", "ParallelTransformerLayerPipe"],
        )

    def insert_layers(
        self, layers: Union[nn.Module, nn.ModuleList, nn.Sequential, List], idx
    ):
        """
        inserts the layers in `layers` into the pipe model at `idx`.
        """
        if isinstance(layers, nn.Module):
            self.specs.insert(idx, layers)
        elif any(
            [isinstance(layers, nn.ModuleList), isinstance(layers, nn.Sequential)]
        ):
            self.specs[idx:idx] = layers
        elif isinstance(layers, list):
            assert all(
                [hasattr(l, "__call__") for l in layers]
            ), "all items in `layers` must be Callables"
            self.specs[idx:idx] = layers
        else:
            raise ValueError(
                f"layer passed into {self.__class__.__name__}.insert_layer() should be either an nn.Module, an nn.ModuleList, an nn.Sequential object, or a list of callables not a {type(layers)}"
            )

        # re-initialize parent class
        super().__init__(
            layers=self.specs,
            loss_fn=self.loss_fn,
            topology=self.__topology__,
            activation_checkpoint_interval=self.activation_checkpoint_interval,
            partition_method=self.neox_args.pipe_partition_method,
            checkpointable_layers=["GMLPBlock", "ParallelTransformerLayerPipe"],
        )

    def init_specs(self):

        weight_tying = not self.neox_args.no_weight_tying
        self.specs = []

        # Embedding layer
        # input will be (input_ids, position_ids, attention_mask)

        if weight_tying:
            self.specs.append(
                TiedLayerSpec(
                    "embed",
                    EmbeddingPipe,
                    self.neox_args,
                    self.hidden_size,
                    self.neox_args.padded_vocab_size,
                    self.neox_args.max_position_embeddings,
                    self.neox_args.hidden_dropout,
                    self.init_method,
                    self.num_tokentypes,
                    tied_weight_attr="word_embeddings_weight",
                )
            )
        else:
            self.specs.append(
                LayerSpec(
                    EmbeddingPipe,
                    self.neox_args,
                    self.hidden_size,
                    self.neox_args.padded_vocab_size,
                    self.neox_args.max_position_embeddings,
                    self.neox_args.hidden_dropout,
                    self.init_method,
                    self.num_tokentypes,
                )
            )

        # NB: the attention mask always needs to be the *last* item in the args when being passed from
        # one stage to the next, because deepspeed is hacks on top of hacks.
        #
        # outputs are now (hidden_states,  attention_mask)

        self.specs.append(_pre_transformer_block)

        # T5 RPE positional embedding
        if self.neox_args.pos_emb == "rpe":
            hidden_size_per_attention_head = mpu.divide(
                self.neox_args.hidden_size, self.neox_args.num_attention_heads
            )
            rpe_scale = math.sqrt(hidden_size_per_attention_head)
            rpe_emb = ParallelRelativePositionBias(
                neox_args=self.neox_args,
                scale=rpe_scale,
                causal=True,
                num_buckets=self.neox_args.rpe_num_buckets,
                max_distance=self.neox_args.rpe_max_distance,
                heads=self.neox_args.num_attention_heads,
            )

        # Transformer layers
        for i in range(self.neox_args.num_layers):
            layer_type = self.neox_args.attention_config[i]
            if layer_type in ["gmlp", "amlp"]:
                self.specs.append(
                    LayerSpec(
                        GMLPBlock,
                        init_method=self.init_method,
                        layer_number=i,
                        output_layer_init_method=self.output_layer_init_method,
                        neox_args=self.neox_args,
                        mask_fn=gpt2_attention_mask_func,
                    )
                )
            else:
                if self.neox_args.moe_freq > 0 and i%self.neox_args.moe_freq==(self.neox_args.moe_freq-1):
                    layer_cls = MoEParallelTransformerLayerPipe
                else:
                    layer_cls = ParallelTransformerLayerPipe
                # TODO: implement shared moe layers
                if self.neox_args.moe_share_layers is not None and layer_cls == MoEParallelTransformerLayerPipe:
                    assert self.neox_args.pipe_parallel_size <= 1, "not support using pp and sharing moe layers together"
                    group_size = self.neox_args.moe_share_layers.get('group_size', self.neox_args.num_layers)
                    assert self.neox_args.num_layers % group_size == 0
                    group_idx = i // group_size
                    # only share params inside group
                    self.specs.append(
                        TiedLayerSpec(
                            "moe"+'_group_'+str(group_idx),
                            MoEParallelTransformerLayerPipe,
                            neox_args=self.neox_args,
                            attention_mask_func=gpt2_attention_mask_func,
                            init_method=self.init_method,
                            output_layer_init_method=self.output_layer_init_method,
                            layer_number=i,
                            rpe=rpe_emb if self.neox_args.pos_emb == "rpe" else None,
                            rotary=self.neox_args.pos_emb == "rotary",
                            use_cache=self.use_cache,
                        )
                    )

                else:
                    self.specs.append(
                        LayerSpec(
                            layer_cls,
                            neox_args=self.neox_args,
                            attention_mask_func=gpt2_attention_mask_func,
                            init_method=self.init_method,
                            output_layer_init_method=self.output_layer_init_method,
                            layer_number=i,
                            rpe=rpe_emb if self.neox_args.pos_emb == "rpe" else None,
                            rotary=self.neox_args.pos_emb == "rotary",
                            use_cache=self.use_cache,
                        )
                    )

        # used to drop attention mask + reshape hidden states
        if self.neox_args.moe_freq > 0:
            self.specs.append(_post_moe_transformer_block)
        else:
            self.specs.append(_post_transformer_block)
            
        # NormPipe is a (deprecated) helper class that used to be used to pass presents along the pipeline - since presents are now cached to the `TransformerLayer` class this is no longer needed
        norm, eps = get_norm(self.neox_args)
        self.specs.append(
            LayerSpec(NormPipe, norm, self.neox_args.hidden_size, eps=eps)
        )

        # outputs are now a single tensor: hidden_states

        def _logits_helper(embedding, lm_output):
            """Just a wrapper to massage inputs/outputs from pipeline."""
            if isinstance(lm_output, (list, tuple)):
                logits = parallel_lm_logits(
                    lm_output[0], embedding.word_embeddings_weight, self.parallel_output
                )
                return logits, *lm_output[1:]
            else:
                return parallel_lm_logits(
                    lm_output, embedding.word_embeddings_weight, self.parallel_output
                )

        if weight_tying:
            self.specs.append(
                TiedLayerSpec(
                    "embed",
                    EmbeddingPipe,
                    self.neox_args,
                    self.hidden_size,
                    self.neox_args.padded_vocab_size,
                    self.neox_args.max_position_embeddings,
                    self.neox_args.hidden_dropout,
                    self.init_method,
                    self.num_tokentypes,
                    forward_fn=_logits_helper,
                    tied_weight_attr="word_embeddings_weight",
                )
            )
        else:
            self.specs.append(
                LayerSpec(
                    ParallelLinearPipe,
                    neox_args=self.neox_args,
                    init_method=self.init_method,
                    parallel_output=self.parallel_output,
                    is_last_layer=True,
                )
            )

    def _set_parallel_output(self, value):
        # sets the parallel output value of the final layer to value
        final_layer = list(self.forward_funcs)[-1]
        if isinstance(final_layer, (ParallelLinearPipe, ParallelLinear)):
            final_layer.final_linear.set_parallel_output(value)

    def inference_mode(self, use_cache=True):
        """
        Sets up the model for inference by turning on k/v caching (if specified) and setting `parallel output` of the final layer to false,
        so logits are gathered across model parallel ranks.

        :param cache: (bool) True if you want to use caching during inference, False otherwise
        """
        # first set caching to true if specified
        recursive_setattr(self.forward_funcs, "use_cache", use_cache, assert_type=bool)
        # then set parallel output of the final layer to false so we don't have to gather the output manually
        self._set_parallel_output(False)
        recursive_setattr(self.forward_funcs, "training", False)

    def train_mode(self):
        """
        Sets up the model for training by turning off k/v caching and setting `parallel output` of the final layer to True,
        so logits are not gathered across model parallel ranks, and loss is computed in parallel (more efficient).
        """
        # set caching to false
        recursive_setattr(self.forward_funcs, "use_cache", False)
        # then set parallel output to true (more efficient training)
        self._set_parallel_output(True)
        recursive_setattr(self.forward_funcs, "training", True)

    def clear_cache(self):
        """
        Recursively clears the kv cache on all layers
        """
        recursive_setattr(self.forward_funcs, "layer_past", None)

    def to_sequential(self):
        """
        Transforms the PipelineModule to a plain nn.Sequential module
        :return:
        """
        layers = []
        tied_layers = defaultdict(list)
        for n, spec in enumerate(self.specs):
            if isinstance(spec, TiedLayerSpec):
                if spec.key in tied_layers:
                    # receiver
                    if 'moe' in spec.key:
                        assert spec.typename == MoEParallelTransformerLayerPipe
                        assert isinstance(tied_layers[spec.key][0], MoEParallelTransformerLayerPipe)
                        experts = tied_layers[spec.key][0].experts
                        layers.append(
                            spec.build(log=False, experts=experts)
                        )
                    else:
                        layers.append(
                            Lambda(lambda x: spec.forward_fn(tied_layers[spec.key][0], x))
                        )
                else:
                    # owner
                    module = spec.build(log=False)
                    layers.append(module)
                    tied_layers[spec.key].append(module)
            elif isinstance(spec, LayerSpec):
                layers.append(spec.build(log=False))
            elif hasattr(spec, "__call__"):
                # check that it's a callable function
                layers.append(Lambda(spec))
            else:
                raise ValueError(f"Layer number {n} ({spec}) Not recognized")
        
        if self.neox_args.moe_freq > 0 and self.neox_args.moe_share_layers is not None and self.neox_args.moe_share_layers['num_z']>1:
            moe_layer_indices = []
            for i,layer in enumerate(layers):
                if isinstance(layer, MoEParallelTransformerLayerPipe):
                    moe_layer_indices.append(i)
            num_moe_layers = len(moe_layer_indices)
            for i in moe_layer_indices:
                layers[i].moe_layer.set_layer_gate(
                    num_layers=num_moe_layers, layer_idx=i, 
                    num_z=self.neox_args.moe_share_layers['num_z'],
                    layer_gate_type=self.neox_args.moe_share_layers['layer_gate_type'])

        model = SequentialWrapper(
            layers,
            self.activation_checkpoint_interval,
            self.activation_checkpoint_func,
            parent_class_name=self.__class__.__name__,
        )
        return model

    def forward(self, forward_input):
        return super().forward(forward_input)