from deepspeed.moe.layer import MoE, MOELayer, TopKGate, Experts
from deepspeed.moe.sharded_moe import einsum, _AllToAll
from torch.nn import Module
import torch
from megatron.model.moe.baselayer import BaseLayer
from megatron.model.moe.hier_moe import HierBalancedMoELayer, LocalGate, LocalExperts
from deepspeed.utils.logging import log_dist
from collections import OrderedDict
from torch import Tensor

class MoeFromDense(MoE):
    """
    the moe-from-dense model should be identical with the dense model at the beginning of the training
    the top1 routing should not multiply probability on the expert outputs
    
    """
    def __init__(self, hidden_size, expert, num_experts=1, ep_size=1, k=1, capacity_factor=1, 
                 eval_capacity_factor=1, min_capacity=4, use_residual=False, noisy_gate_policy: str = None, 
                 drop_tokens: bool = True, use_rts=True, use_tutel: bool = False, enable_expert_tensor_parallelism: bool = False, 
                 aux_loss_weight: dict = None, use_elbo=False, experts=None, post=None, hier_moe=None, gate_st=False, unrouted_type='all', **kwargs):
        super(MoeFromDense, self).__init__(
            hidden_size=hidden_size, 
            expert=expert, 
            num_experts=num_experts, 
            ep_size=ep_size, 
            k=k, 
            capacity_factor=capacity_factor, 
            eval_capacity_factor=eval_capacity_factor, 
            min_capacity=min_capacity, 
            use_residual=use_residual, 
            noisy_gate_policy=noisy_gate_policy, 
            drop_tokens=drop_tokens, 
            use_rts=use_rts, 
            use_tutel=use_tutel, 
            enable_expert_tensor_parallelism=enable_expert_tensor_parallelism, 
            aux_loss_weight=aux_loss_weight,
            use_elbo=use_elbo,
            experts=experts,
            gate_st=gate_st
        )
        
        assert post is None or post in ['local', 'balanced'], post
        if post == 'local':
            self.deepspeed_moe = LocalPostMoELayer.from_moe_layer(self.deepspeed_moe)
            self.deepspeed_moe.unrouted_type = unrouted_type
        elif post == 'balanced':
            self.deepspeed_moe = BalancedPostMoELayer.from_moe_layer(self.deepspeed_moe)
            self.deepspeed_moe.unrouted_type = unrouted_type

        if hier_moe is not None:
            inside_k = hier_moe['inside_k']
            if experts is None:
                experts = LocalExperts(expert, self.num_local_experts, self.expert_group_name)
            else:
                experts = LocalExperts.from_existing_experts(experts, expert_group_name=self.expert_group_name)
            gate_st = True
            crossgpu_gate = TopKGate(hidden_size, ep_size, k, capacity_factor, eval_capacity_factor, min_capacity, noisy_gate_policy, drop_tokens, \
                                  use_rts, aux_loss_weight=aux_loss_weight, gate_st=gate_st)
            insidegpu_gate = LocalGate(hidden_size, num_experts=self.num_local_experts, k=inside_k, aux_loss_weight=aux_loss_weight, \
                                    gate_st=gate_st, expert_group_name=self.expert_group_name)
            self.deepspeed_moe = HierBalancedMoELayer(crossgpu_gate,
                                insidegpu_gate,
                                experts,
                                self.expert_group_name,
                                self.ep_size,
                                self.num_local_experts,
                                use_tutel=use_tutel,
                                use_elbo=use_elbo)

def assert_all_experts_are_same(experts):
    def assert_two_modules_are_same(m1, m2):
        for p1, p2 in zip(m1.parameters(), m2.parameters()):
            assert p1.data.shape == p2.data.shape
            assert torch.allclose(p1.data, p2.data)
    for e in experts.deepspeed_experts:
        assert_two_modules_are_same(e, experts.deepspeed_experts[0])

def assert_close(x1, x2):
    assert torch.allclose(x1, x2, atol=1e-6), f'max distance:{torch.max(torch.abs(x1-x2))}'

class LocalPostMoELayer(MOELayer):
    def __init__(self,
                    gate: Module,
                    experts: Module,
                    ep_group_name,
                    ep_size,
                    num_local_experts: int,
                    use_tutel: bool = False,
                    use_elbo = False,
                    unrouted_type='all') -> None:        
        assert_all_experts_are_same(experts)
        super().__init__(gate, experts, ep_group_name, ep_size, num_local_experts, use_tutel, use_elbo)
        self.unrouted_type=unrouted_type
        self.ep_rank = torch.distributed.get_rank(self.ep_group)
        self.ep_world_size = torch.distributed.get_world_size(self.ep_group)
        self.num_experts = self.ep_world_size * self.num_local_experts
        assert self.num_experts == self.gate.wg.weight.shape[0]

    @classmethod
    def from_moe_layer(cls, moe_layer:MOELayer):
        return cls(moe_layer.gate, moe_layer.experts, moe_layer.ep_group_name, moe_layer.ep_size, moe_layer.num_local_experts)

    def forward(self, *inputs):
        # Implement Algorithm 2 from GShard paper.
        d_model = inputs[0].shape[-1]

        # Initial implementation -> Reshape into S tokens by dropping sequence dimension.
        # Reshape into G groups so that each group can distribute tokens equally
        # group_size = kwargs['group_size'] if 'group_size' in kwargs.keys() else 1
        reshaped_inputs = inputs[0].reshape(-1, d_model)

        self.l_aux, combine_weights, dispatch_mask, self.exp_counts, routing_probs = self.gate(
            reshaped_inputs, inputs[1], return_gates=True)
        dispatched_inputs = einsum(
            "sec,sm->ecm", dispatch_mask.type_as(inputs[0]), reshaped_inputs
        )  # TODO: heavy memory usage due to long sequence length


        dispatched_inputs = _AllToAll.apply(self.ep_group, dispatched_inputs)

        # Re-shape after all-to-all: ecm -> gecm
        dispatched_inputs = dispatched_inputs.reshape(self.ep_size, self.num_local_experts, -1, d_model)

        expert_output = self.experts(dispatched_inputs)

        expert_output = _AllToAll.apply(self.ep_group, expert_output)

        # Re-shape back: gecm -> ecm
        expert_output = expert_output.reshape(self.ep_size * self.num_local_experts, -1, d_model)

        if self.gate.k == 1:
            combine_weights = combine_weights-combine_weights.detach() + dispatch_mask
            # combine_weights = dispatch_mask
            self.unrouted_type = 'all'
        
        combined_output = einsum("sec,ecm->sm", combine_weights.type_as(inputs[0]), expert_output)

        if self.unrouted_type == 'all': # both first and second routing are failed
            routed_mask = (dispatch_mask!=0).any(-1).any(-1)
            combined_output[~routed_mask], _ = self.post_routing(reshaped_inputs[~routed_mask]) # if tokens are unrouted, computed at local devices
        elif self.unrouted_type == 'any': # either first or second routing are failed
            combine_weights_sum = dispatch_mask.sum(dim=-1).sum(dim=-1) # number of experts that each token is sent to
            if self.gate.k == 3:
                routed_mask = (combine_weights_sum >= 2) # for top3 routing, we are actually doing top2 routing, only few of tokens are processed by the third expert
            else:
                routed_mask = (combine_weights_sum >= self.gate.k) # TODO: combine k=3 and post routing
            unrouted_mask = ~routed_mask
            masked_routing_probs = routing_probs[unrouted_mask].type_as(combined_output) # routing probs of unrouted tokens

            # we do not need to consider second experts routing, since the first routing is in prior
            # the unrouted routing must be the second routing or both first and second routng
            gates_mask = combine_weights_sum[unrouted_mask] > 0 # among unrouted tokens, which tokens are totally unrouted, which are partially unrouted
            top1_probs = torch.max(masked_routing_probs, dim=-1, keepdim=True).values * gates_mask.unsqueeze(dim=-1) # routing probs of successfully top1-routing tokens
            top1_outputs = combined_output[unrouted_mask]

            # postprocess unrouted tokens
            post_routing_outputs, post_routing_expert_indices = self.post_routing(reshaped_inputs[unrouted_mask])
            post_routing_probs = torch.gather(
                masked_routing_probs, dim=1, index=post_routing_expert_indices.unsqueeze(dim=-1))
            assert top1_probs.shape == post_routing_probs.shape, f'{top1_probs.shape=}, {post_routing_probs.shape=}'
            norm = torch.clamp(top1_probs+post_routing_probs, min=torch.finfo(top1_probs.dtype).eps)
            combined_output[unrouted_mask] = (post_routing_outputs * post_routing_probs + top1_probs * top1_outputs) / norm
        out = combined_output.reshape(inputs[0].shape)
        
        return out
    
    def post_routing(self, inputs:Tensor):
        num_tokens = inputs.shape[0]
        assert num_tokens % self.num_local_experts == 0
        num_tokens_per_experts = inputs.shape[0] // self.num_local_experts
        local_expert_indices = torch.tensor(
            [i//num_tokens_per_experts for i in range(inputs.shape[0])], device=inputs.device)
        global_expert_indices = self.ep_rank * self.num_local_experts + local_expert_indices
        assert torch.all(global_expert_indices < self.num_experts), f'{self.ep_rank=}, {self.num_local_experts=}, {local_expert_indices=}'
        return self.experts(inputs.unsqueeze(dim=0)).squeeze(dim=0), global_expert_indices

class BaseLayerNoStateDict(BaseLayer):
    def state_dict(self, *args, **kwargs):
        return OrderedDict()

class BalancedPostMoELayer(LocalPostMoELayer):
    def __init__(self, gate: Module, experts: Module, ep_group_name, ep_size, num_local_experts: int, use_tutel: bool = False, use_elbo=False, unrouted_type='all') -> None:
        super().__init__(gate, experts, ep_group_name, ep_size, num_local_experts, use_tutel, use_elbo, unrouted_type)
     
    def post_routing(self, inputs):
        if not hasattr(self, 'base_layer'):
            self.base_layer = BaseLayerNoStateDict.from_moe_layer(self)
            self.base_layer.gate.gate_st = True

        num_experts = self.gate.wg.weight.shape[0]
        if inputs.shape[0] < num_experts or inputs.shape[0] % num_experts != 0:
            pad_len = num_experts - inputs.shape[0] % num_experts
            padded_inputs = torch.cat(
                [
                    inputs, 
                    torch.zeros([pad_len, inputs.shape[-1]], dtype=inputs.dtype, device=inputs.device)
                ], dim=0
            )
            results =  self.base_layer(padded_inputs)
            results = results[:-pad_len]
        else:
            results = self.base_layer(inputs)
        return results
        