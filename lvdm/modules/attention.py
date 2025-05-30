from functools import partial
import math
import torch
from torch import nn, einsum
import torch.nn.functional as F
from einops import rearrange, repeat
import scipy.io as io
import numpy as np
import pickle

try:
    import xformers
    import xformers.ops
    XFORMERS_IS_AVAILBLE = True
except:
    XFORMERS_IS_AVAILBLE = False
from lvdm.common import (
    checkpoint,
    exists,
    default,
)
from lvdm.basics import (
    zero_module,
)

def generate_weight_sequence():
    return [1]*16 # weight_sequence

class RelativePosition(nn.Module):
    """ https://github.com/evelinehong/Transformer_Relative_Position_PyTorch/blob/master/relative_position.py """

    def __init__(self, num_units, max_relative_position):
        super().__init__()
        self.num_units = num_units
        self.max_relative_position = max_relative_position
        self.embeddings_table = nn.Parameter(torch.Tensor(max_relative_position * 2 + 1, num_units))
        nn.init.xavier_uniform_(self.embeddings_table)

    def forward(self, length_q, length_k):
        device = self.embeddings_table.device
        range_vec_q = torch.arange(length_q, device=device)
        range_vec_k = torch.arange(length_k, device=device)
        distance_mat = range_vec_k[None, :] - range_vec_q[:, None]
        distance_mat_clipped = torch.clamp(distance_mat, -self.max_relative_position, self.max_relative_position)
        final_mat = distance_mat_clipped + self.max_relative_position
        final_mat = final_mat.long()
        embeddings = self.embeddings_table[final_mat]
        return embeddings


class CrossAttention(nn.Module):

    def __init__(self, query_dim, context_dim=None, heads=8, dim_head=64, dropout=0., 
                 relative_position=False, temporal_length=None, img_cross_attention=False, injection=False):
        super().__init__()
        inner_dim = dim_head * heads
        context_dim = default(context_dim, query_dim)

        self.scale = dim_head**-0.5
        self.heads = heads
        self.dim_head = dim_head
        self.to_q = nn.Linear(query_dim, inner_dim, bias=False)
        self.to_k = nn.Linear(context_dim, inner_dim, bias=False)
        self.to_v = nn.Linear(context_dim, inner_dim, bias=False)
        self.to_out = nn.Sequential(nn.Linear(inner_dim, query_dim), nn.Dropout(dropout))

        self.image_cross_attention_scale = 1.0
        self.text_context_len = 77
        self.img_cross_attention = img_cross_attention
        if self.img_cross_attention:
            self.to_k_ip = nn.Linear(context_dim, inner_dim, bias=False)
            self.to_v_ip = nn.Linear(context_dim, inner_dim, bias=False)
        
        self.relative_position = relative_position
        if self.relative_position:
            assert(temporal_length is not None)
            self.relative_position_k = RelativePosition(num_units=dim_head, max_relative_position=temporal_length)
            self.relative_position_v = RelativePosition(num_units=dim_head, max_relative_position=temporal_length)
        else:
            ## only used for spatial attention, while NOT for temporal attention
            if XFORMERS_IS_AVAILBLE and temporal_length is None:
                self.forward = self.efficient_forward

        self.injection = injection



    def forward(self, x, context=None, mask=None, context_next=None, use_injection=False, timesteps=None, num_layer=None):

        def get_views(video_length, window_size=16, stride=4):
            num_blocks_time = (video_length - window_size) // stride + 1
            views = []
            for i in range(num_blocks_time):
                t_start = int(i * stride)
                t_end = t_start + window_size
                views.append((t_start,t_end))
            return views

        context_next = get_views(64, 16, 4)

        sa_flag = False
        if context is None:
            sa_flag = True

        # context is always None

        h = self.heads
        

        all_q = self.to_q(x)
        context = default(context, x) 
        ## considering image token additionally
        if context is not None and self.img_cross_attention:
            context, context_img = context[:,:self.text_context_len,:], context[:,self.text_context_len:,:]
            all_k = self.to_k(context)
            all_v = self.to_v(context)
            all_k_ip = self.to_k_ip(context_img)
            all_v_ip = self.to_v_ip(context_img)
        else:
            all_k = self.to_k(context)
            all_v = self.to_v(context)

        count = torch.zeros_like(all_k) ####
        value = torch.zeros_like(all_k)

        if (sa_flag) and (context_next is not None):
            all_q, all_k, all_v = map(lambda t: rearrange(t, 'b n (h d) -> (b h) n d', h=h), (all_q, all_k, all_v))
            if context is not None and self.img_cross_attention:
                all_k_ip, all_v_ip = map(lambda t: rearrange(t, 'b n (h d) -> (b h) n d', h=h), (all_k_ip, all_v_ip))
            
            # ------------------------long frame------------------------

            qk_scale0 = (math.log(64, 16)) ** 0.5 # attention entropy

            all_sim = torch.einsum('b i d, b j d -> b i j',  qk_scale0* all_q, all_k) * self.scale
            if self.relative_position:
                all_len_q, all_len_k, all_len_v = all_.shape[1], all_k.shape[1], all_v.shape[1]
                all_k2 = self.relative_position_k(len_q, len_k)
                all_sim2 = einsum('b t d, t s d -> b t s', all_q, all_k2) * self.scale # TODO check 
                all_sim += all_sim2
            # del all_k

            if exists(mask):
                ## feasible for causal attention mask only
                max_neg_value = -torch.finfo(all_sim.dtype).max
                mask = repeat(mask, 'b i j -> (b h) i j', h=h)
                sim.masked_fill_(~(mask>0.5), max_neg_value)

            # attention, what we cannot get enough of
            all_sim = all_sim.softmax(dim=-1)
            all_out = torch.einsum('b i j, b j d -> b i d', all_sim, all_v)
            if self.relative_position:
                all_v2 = self.relative_position_v(all_len_q, all_len_v)
                all_out2 = einsum('b t s, t s d -> b t d', all_sim, all_v2) # TODO check
                all_out += all_out2
            all_out = rearrange(all_out, '(b h) n d -> b n (h d)', h=h)

            ## considering image token additionally
            if context is not None and self.img_cross_attention:
                all_k_ip, all_v_ip = map(lambda t: rearrange(t, 'b n (h d) -> (b h) n d', h=h), (all_k_ip, all_v_ip))
                all_sim_ip =  torch.einsum('b i d, b j d -> b i j', all_q, all_k_ip) * self.scale
                del all_k_ip
                all_sim_ip = all_sim_ip.softmax(dim=-1)
                all_out_ip = torch.einsum('b i j, b j d -> b i d', all_sim_ip, all_v_ip)
                all_out_ip = rearrange(all_out_ip, '(b h) n d -> b n (h d)', h=h)
                all_out = all_out + self.image_cross_attention_scale * all_out_ip
            # del all_q

            # ------------------------short frame------------------------
            preserve = 0
            tobeprint_list = []
            for t_start, t_end in context_next:
                weight_sequence = generate_weight_sequence()
                weight_tensor = torch.ones_like(count[:, t_start:t_end])
                weight_tensor = weight_tensor * torch.Tensor(weight_sequence).to(x.device).unsqueeze(0).unsqueeze(-1)

                q = all_q[:, t_start:t_end]
                k = all_k[:, t_start:t_end]
                v = all_v[:, t_start:t_end] ###### clip the video frame

                sim = torch.einsum('b i d, b j d -> b i j', q, k) * self.scale
                if self.relative_position:
                    len_q, len_k, len_v = q.shape[1], k.shape[1], v.shape[1]
                    k2 = self.relative_position_k(len_q, len_k)
                    sim2 = einsum('b t d, t s d -> b t s', q, k2) * self.scale # TODO check 
                    sim += sim2
                del k

                if exists(mask):
                    ## feasible for causal attention mask only
                    max_neg_value = -torch.finfo(sim.dtype).max
                    mask = repeat(mask, 'b i j -> (b h) i j', h=h)
                    sim.masked_fill_(~(mask>0.5), max_neg_value)

                # attention, what we cannot get enough of
                sim = sim.softmax(dim=-1)
                out = torch.einsum('b i j, b j d -> b i d', sim, v)
                if self.relative_position:
                    v2 = self.relative_position_v(len_q, len_v)
                    out2 = einsum('b t s, t s d -> b t d', sim, v2) # TODO check
                    out += out2
                out = rearrange(out, '(b h) n d -> b n (h d)', h=h)
                # print('out is', out.shape) # dim=1 is frame

                ## considering image token additionally
                if context is not None and self.img_cross_attention:
                    k_ip = all_k_ip[:, t_start:t_end] #
                    v_ip = all_v_ip[:, t_start:t_end] #
                    sim_ip =  torch.einsum('b i d, b j d -> b i j', q, k_ip) * self.scale
                    del k_ip
                    sim_ip = sim_ip.softmax(dim=-1)
                    out_ip = torch.einsum('b i j, b j d -> b i d', sim_ip, v_ip)
                    out_ip = rearrange(out_ip, '(b h) n d -> b n (h d)', h=h)
                    out = out + self.image_cross_attention_scale * out_ip
                del q

                # --------------------FreePCA begin-------------------
                if (preserve > 0 and timesteps > 250) or (preserve > 3 and timesteps > 500):
                    
                    dim_d = out.shape[-1]
                    ref_out = rearrange(out, 'b n d -> (b d) n', d=dim_d)
                    
                    com_out = all_out[:, t_start:t_end, :] # clip the long frames to short frames
                    com_out = rearrange(com_out, 'b n d -> (b d) n', d=dim_d)
                    # Consistency Feature Decomposition
                    ref_mean = torch.mean(ref_out, dim=-1, keepdim=True)
                    ref_data = ref_out - ref_mean

                    comp_mean = torch.mean(com_out, dim=-1, keepdim=True)
                    comp_data = com_out - comp_mean

                    cov_matrix = torch.matmul(comp_data.t(), comp_data) / (ref_out.size(1) - 1)
                    eigenvalues, eigenvectors = torch.linalg.eig(cov_matrix)

                    # sorted_indices = torch.argsort(eigenvalues.real, descending=True)

                    origin_pca = torch.matmul(ref_data, eigenvectors.real)
                    comp_pca = torch.matmul(comp_data, eigenvectors.real)

                    cos_similarities = []
                    for i in range(origin_pca.size(1)):
                        cos_similarity = F.cosine_similarity(origin_pca[:, i], comp_pca[:, i], dim=0)
                        cos_similarities.append(cos_similarity.item())

                    cos_similarities = torch.tensor(cos_similarities)

                    sorted_indices = torch.argsort(cos_similarities, descending=True)
                    # Progressive Fusion
                    selected_k = min(preserve, 3)  

                    comp_pca[:, sorted_indices[selected_k:]] = 0
                    origin_pca[:, sorted_indices[:selected_k]] = 0

                    fuse_pca = origin_pca + comp_pca

                    out = torch.matmul(fuse_pca, eigenvectors.t().real) + comp_mean 

                    out = rearrange(out, '(b d) n -> b n d', d=dim_d)
                #---------------------FreePCA end---------------------
                preserve += 1
                
                value[:,t_start:t_end] += out * weight_tensor #
                count[:,t_start:t_end] += weight_tensor #

            final_out = torch.where(count>0, value/count, value) # (?, frame, ?)

        else:
            # print('cross atten')
            q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> (b h) n d', h=h), (all_q, all_k, all_v))
            sim = torch.einsum('b i d, b j d -> b i j', q, k) * self.scale
            if self.relative_position:
                len_q, len_k, len_v = q.shape[1], k.shape[1], v.shape[1]
                k2 = self.relative_position_k(len_q, len_k)
                sim2 = einsum('b t d, t s d -> b t s', q, k2) * self.scale # TODO check 
                sim += sim2
            del k

            if exists(mask):
                ## feasible for causal attention mask only
                max_neg_value = -torch.finfo(sim.dtype).max
                mask = repeat(mask, 'b i j -> (b h) i j', h=h)
                sim.masked_fill_(~(mask>0.5), max_neg_value)

            # attention, what we cannot get enough of
            sim = sim.softmax(dim=-1)
            out = torch.einsum('b i j, b j d -> b i d', sim, v)
            if self.relative_position:
                v2 = self.relative_position_v(len_q, len_v)
                out2 = einsum('b t s, t s d -> b t d', sim, v2) # TODO check
                out += out2
            final_out = rearrange(out, '(b h) n d -> b n (h d)', h=h)

            ## considering image token additionally
            if context is not None and self.img_cross_attention:
                k_ip, v_ip = map(lambda t: rearrange(t, 'b n (h d) -> (b h) n d', h=h), (all_k_ip, all_v_ip))
                sim_ip =  torch.einsum('b i d, b j d -> b i j', q, k_ip) * self.scale
                del k_ip
                sim_ip = sim_ip.softmax(dim=-1)
                out_ip = torch.einsum('b i j, b j d -> b i d', sim_ip, v_ip)
                out_ip = rearrange(out_ip, '(b h) n d -> b n (h d)', h=h)
                final_out = final_out + self.image_cross_attention_scale * out_ip
            del q
        

        return self.to_out(final_out)
    
    # spatial attention
    def efficient_forward(self, x, context=None, mask=None, context_next=None, use_injection=False, timesteps=None, num_layer=None):
        sa_flag = False
        if context is None:
            sa_flag = True

        q = self.to_q(x)
        context = default(context, x)

        if not sa_flag: 
            sq_size = x.shape[0]
            if self.injection and use_injection:
                context_new = context[-sq_size:]
            else:
                context_new = context[:sq_size]
        else:
            context_new = context.clone()

        ## considering image token additionally
        if context is not None and self.img_cross_attention:
            context, context_img = context_new[:,:self.text_context_len,:], context_new[:,self.text_context_len:,:]
            k = self.to_k(context)
            v = self.to_v(context)
            k_ip = self.to_k_ip(context_img)
            v_ip = self.to_v_ip(context_img)
        else:
            k = self.to_k(context_new)
            v = self.to_v(context_new)

        b, _, _ = q.shape
        q, k, v = map(
            lambda t: t.unsqueeze(3)
            .reshape(b, t.shape[1], self.heads, self.dim_head)
            .permute(0, 2, 1, 3)
            .reshape(b * self.heads, t.shape[1], self.dim_head)
            .contiguous(),
            (q, k, v),
        )
        # actually compute the attention, what we cannot get enough of
        out = xformers.ops.memory_efficient_attention(q, k, v, attn_bias=None, op=None)

        ## considering image token additionally
        if context is not None and self.img_cross_attention:
            k_ip, v_ip = map(
                lambda t: t.unsqueeze(3)
                .reshape(b, t.shape[1], self.heads, self.dim_head)
                .permute(0, 2, 1, 3)
                .reshape(b * self.heads, t.shape[1], self.dim_head)
                .contiguous(),
                (k_ip, v_ip),
            )
            out_ip = xformers.ops.memory_efficient_attention(q, k_ip, v_ip, attn_bias=None, op=None)
            out_ip = (
                out_ip.unsqueeze(0)
                .reshape(b, self.heads, out.shape[1], self.dim_head)
                .permute(0, 2, 1, 3)
                .reshape(b, out.shape[1], self.heads * self.dim_head)
            )

        if exists(mask):
            raise NotImplementedError
        out = (
            out.unsqueeze(0)
            .reshape(b, self.heads, out.shape[1], self.dim_head)
            .permute(0, 2, 1, 3)
            .reshape(b, out.shape[1], self.heads * self.dim_head)
        )
        if context is not None and self.img_cross_attention:
            out = out + self.image_cross_attention_scale * out_ip
        return self.to_out(out)


class BasicTransformerBlock(nn.Module):

    def __init__(self, dim, n_heads, d_head, dropout=0., context_dim=None, gated_ff=True, checkpoint=True,
                disable_self_attn=False, attention_cls=None, img_cross_attention=False, injection=False):
        super().__init__()
        
        attn_cls = CrossAttention if attention_cls is None else attention_cls
        self.disable_self_attn = disable_self_attn
        self.attn1 = attn_cls(query_dim=dim, heads=n_heads, dim_head=d_head, dropout=dropout,
            context_dim=context_dim if self.disable_self_attn else None, injection=injection)
        self.ff = FeedForward(dim, dropout=dropout, glu=gated_ff)
        self.attn2 = attn_cls(query_dim=dim, context_dim=context_dim, heads=n_heads, dim_head=d_head, dropout=dropout,
            img_cross_attention=img_cross_attention, injection=injection)
        self.norm1 = nn.LayerNorm(dim)
        self.norm2 = nn.LayerNorm(dim)
        self.norm3 = nn.LayerNorm(dim)
        self.checkpoint = checkpoint

    def forward(self, x, context=None, mask=None, context_next=None, use_injection=False, timesteps=None, num_layer=None, **kwargs):
        ## implementation tricks: because checkpointing doesn't support non-tensor (e.g. None or scalar) arguments
        input_tuple = (x,)      ## should not be (x), otherwise *input_tuple will decouple x into multiple arguments
        if context is not None:
            input_tuple = (x, context)
        if mask is not None:
            forward_mask = partial(self._forward, mask=mask)
            return checkpoint(forward_mask, (x,), self.parameters(), self.checkpoint)
        if context is not None and mask is not None:
            input_tuple = (x, context, mask)
        input_tuple = (x, context, mask, context_next, use_injection, timesteps,  num_layer)
        return checkpoint(self._forward, input_tuple, self.parameters(), self.checkpoint)

    def _forward(self, x, context=None, mask=None, context_next=None, use_injection=False, timesteps=None, num_layer=None):
        x = self.attn1(self.norm1(x), context=context if self.disable_self_attn else None, mask=mask, context_next=context_next, use_injection=False, timesteps=timesteps, num_layer=num_layer) + x
        x = self.attn2(self.norm2(x), context=context, mask=mask, context_next=context_next, use_injection=use_injection, timesteps=timesteps, num_layer=num_layer) + x
        x = self.ff(self.norm3(x)) + x
        return x


class SpatialTransformer(nn.Module):
    """
    Transformer block for image-like data in spatial axis.
    First, project the input (aka embedding)
    and reshape to b, t, d.
    Then apply standard transformer action.
    Finally, reshape to image
    NEW: use_linear for more efficiency instead of the 1x1 convs
    """

    def __init__(self, in_channels, n_heads, d_head, depth=1, dropout=0., context_dim=None,
                 use_checkpoint=True, disable_self_attn=False, use_linear=False, img_cross_attention=False, injection=False):
        super().__init__()
        self.in_channels = in_channels
        inner_dim = n_heads * d_head
        self.norm = torch.nn.GroupNorm(num_groups=32, num_channels=in_channels, eps=1e-6, affine=True)
        if not use_linear:
            self.proj_in = nn.Conv2d(in_channels, inner_dim, kernel_size=1, stride=1, padding=0)
        else:
            self.proj_in = nn.Linear(in_channels, inner_dim)

        self.transformer_blocks = nn.ModuleList([
            BasicTransformerBlock(
                inner_dim,
                n_heads,
                d_head,
                dropout=dropout,
                context_dim=context_dim,
                img_cross_attention=img_cross_attention,
                disable_self_attn=disable_self_attn,
                checkpoint=use_checkpoint,
                injection=injection) for d in range(depth)
        ])
        if not use_linear:
            self.proj_out = zero_module(nn.Conv2d(inner_dim, in_channels, kernel_size=1, stride=1, padding=0))
        else:
            self.proj_out = zero_module(nn.Linear(inner_dim, in_channels))
        self.use_linear = use_linear


    def forward(self, x, context=None, **kwargs):
        b, c, h, w = x.shape
        x_in = x
        x = self.norm(x)
        if not self.use_linear:
            x = self.proj_in(x)
        x = rearrange(x, 'b c h w -> b (h w) c').contiguous()
        if self.use_linear:
            x = self.proj_in(x)
        for i, block in enumerate(self.transformer_blocks):
            x = block(x, context=context, **kwargs)
        if self.use_linear:
            x = self.proj_out(x)
        x = rearrange(x, 'b (h w) c -> b c h w', h=h, w=w).contiguous()
        if not self.use_linear:
            x = self.proj_out(x)
        return x + x_in
    
    
class TemporalTransformer(nn.Module):
    """
    Transformer block for image-like data in temporal axis.
    First, reshape to b, t, d.
    Then apply standard transformer action.
    Finally, reshape to image
    """
    def __init__(self, in_channels, n_heads, d_head, depth=1, dropout=0., context_dim=None,
                 use_checkpoint=True, use_linear=False, only_self_att=True, causal_attention=False,
                 relative_position=False, temporal_length=None, injection=False):
        super().__init__()
        self.only_self_att = only_self_att
        self.relative_position = relative_position
        self.causal_attention = causal_attention
        self.in_channels = in_channels
        inner_dim = n_heads * d_head
        self.norm = torch.nn.GroupNorm(num_groups=32, num_channels=in_channels, eps=1e-6, affine=True)
        self.proj_in = nn.Conv1d(in_channels, inner_dim, kernel_size=1, stride=1, padding=0)
        if not use_linear:
            self.proj_in = nn.Conv1d(in_channels, inner_dim, kernel_size=1, stride=1, padding=0)
        else:
            self.proj_in = nn.Linear(in_channels, inner_dim)

        if relative_position:
            assert(temporal_length is not None)
            attention_cls = partial(CrossAttention, relative_position=True, temporal_length=temporal_length)
        else:
            attention_cls = partial(CrossAttention, temporal_length=temporal_length)
        if self.causal_attention:
            assert(temporal_length is not None)
            self.mask = torch.tril(torch.ones([1, temporal_length, temporal_length]))

        if self.only_self_att:
            context_dim = None
        self.transformer_blocks = nn.ModuleList([
            BasicTransformerBlock(
                inner_dim,
                n_heads,
                d_head,
                dropout=dropout,
                context_dim=context_dim,
                attention_cls=attention_cls,
                checkpoint=use_checkpoint,
                injection=injection) for d in range(depth)
        ])
        if not use_linear:
            self.proj_out = zero_module(nn.Conv1d(inner_dim, in_channels, kernel_size=1, stride=1, padding=0))
        else:
            self.proj_out = zero_module(nn.Linear(inner_dim, in_channels))
        self.use_linear = use_linear

    def forward(self, x, context=None, timesteps=None, num_layer=None, **kwargs):
        b, c, t, h, w = x.shape
        x_in = x
        x = self.norm(x)
        x = rearrange(x, 'b c t h w -> (b h w) c t').contiguous()
        if not self.use_linear:
            x = self.proj_in(x)
        x = rearrange(x, 'bhw c t -> bhw t c').contiguous()
        if self.use_linear:
            x = self.proj_in(x)

        if self.causal_attention:
            mask = self.mask.to(x.device)
            mask = repeat(mask, 'l i j -> (l bhw) i j', bhw=b*h*w)
        else:
            mask = None

        if self.only_self_att:
            ## note: if no context is given, cross-attention defaults to self-attention
            for i, block in enumerate(self.transformer_blocks):
                x = block(x, mask=mask, timesteps=timesteps, num_layer=num_layer, **kwargs)
            x = rearrange(x, '(b hw) t c -> b hw t c', b=b).contiguous()
        else:
            x = rearrange(x, '(b hw) t c -> b hw t c', b=b).contiguous()
            context = rearrange(context, '(b t) l con -> b t l con', t=t).contiguous()
            for i, block in enumerate(self.transformer_blocks):
                # calculate each batch one by one (since number in shape could not greater then 65,535 for some package)
                for j in range(b):
                    context_j = repeat(
                        context[j],
                        't l con -> (t r) l con', r=(h * w) // t, t=t).contiguous()
                    ## note: causal mask will not applied in cross-attention case
                    x[j] = block(x[j], context=context_j, timesteps=timesteps, num_layer=num_layer, **kwargs)
        
        if self.use_linear:
            x = self.proj_out(x)
            x = rearrange(x, 'b (h w) t c -> b c t h w', h=h, w=w).contiguous()
        if not self.use_linear:
            x = rearrange(x, 'b hw t c -> (b hw) c t').contiguous()
            x = self.proj_out(x)
            x = rearrange(x, '(b h w) c t -> b c t h w', b=b, h=h, w=w).contiguous()

        return x + x_in
    

class GEGLU(nn.Module):
    def __init__(self, dim_in, dim_out):
        super().__init__()
        self.proj = nn.Linear(dim_in, dim_out * 2)

    def forward(self, x):
        x, gate = self.proj(x).chunk(2, dim=-1)
        return x * F.gelu(gate)


class FeedForward(nn.Module):
    def __init__(self, dim, dim_out=None, mult=4, glu=False, dropout=0.):
        super().__init__()
        inner_dim = int(dim * mult)
        dim_out = default(dim_out, dim)
        project_in = nn.Sequential(
            nn.Linear(dim, inner_dim),
            nn.GELU()
        ) if not glu else GEGLU(dim, inner_dim)

        self.net = nn.Sequential(
            project_in,
            nn.Dropout(dropout),
            nn.Linear(inner_dim, dim_out)
        )

    def forward(self, x):
        return self.net(x)


class LinearAttention(nn.Module):
    def __init__(self, dim, heads=4, dim_head=32):
        super().__init__()
        self.heads = heads
        hidden_dim = dim_head * heads
        self.to_qkv = nn.Conv2d(dim, hidden_dim * 3, 1, bias = False)
        self.to_out = nn.Conv2d(hidden_dim, dim, 1)

    def forward(self, x):
        b, c, h, w = x.shape
        qkv = self.to_qkv(x)
        q, k, v = rearrange(qkv, 'b (qkv heads c) h w -> qkv b heads c (h w)', heads = self.heads, qkv=3)
        k = k.softmax(dim=-1)  
        context = torch.einsum('bhdn,bhen->bhde', k, v)
        out = torch.einsum('bhde,bhdn->bhen', context, q)
        out = rearrange(out, 'b heads c (h w) -> b (heads c) h w', heads=self.heads, h=h, w=w)
        return self.to_out(out)


class SpatialSelfAttention(nn.Module):
    def __init__(self, in_channels):
        super().__init__()
        self.in_channels = in_channels

        self.norm = torch.nn.GroupNorm(num_groups=32, num_channels=in_channels, eps=1e-6, affine=True)
        self.q = torch.nn.Conv2d(in_channels,
                                 in_channels,
                                 kernel_size=1,
                                 stride=1,
                                 padding=0)
        self.k = torch.nn.Conv2d(in_channels,
                                 in_channels,
                                 kernel_size=1,
                                 stride=1,
                                 padding=0)
        self.v = torch.nn.Conv2d(in_channels,
                                 in_channels,
                                 kernel_size=1,
                                 stride=1,
                                 padding=0)
        self.proj_out = torch.nn.Conv2d(in_channels,
                                        in_channels,
                                        kernel_size=1,
                                        stride=1,
                                        padding=0)

    def forward(self, x, **kwargs):
        
        h_ = x
        h_ = self.norm(h_)
        q = self.q(h_)
        k = self.k(h_)
        v = self.v(h_)

        # compute attention
        b,c,h,w = q.shape
        q = rearrange(q, 'b c h w -> b (h w) c')
        k = rearrange(k, 'b c h w -> b c (h w)')
        w_ = torch.einsum('bij,bjk->bik', q, k)

        w_ = w_ * (int(c)**(-0.5))
        w_ = torch.nn.functional.softmax(w_, dim=2)

        # attend to values
        v = rearrange(v, 'b c h w -> b c (h w)')
        w_ = rearrange(w_, 'b i j -> b j i')
        h_ = torch.einsum('bij,bjk->bik', v, w_)
        h_ = rearrange(h_, 'b c (h w) -> b c h w', h=h)
        h_ = self.proj_out(h_)

        return x+h_
