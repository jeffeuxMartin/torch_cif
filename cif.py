import torch
from typing import Optional, Tuple
from torch import Tensor


def prob_check(tensor, eps=1e-10, neg_inf=-1e8, logp=False):
    assert not torch.isnan(tensor).any(), (
        "Nan in a probability tensor."
    )
    # Add the eps here to prevent errors introduced by precision
    if logp:
        assert tensor.le(0).all() and tensor.ge(neg_inf).all(), (
            "Incorrect values in a log-probability tensor"
            ", -inf <= tensor <= 0"
        )
    else:
        assert tensor.le(1.0 + eps).all() and tensor.ge(0.0 - eps).all(), (
            "Incorrect values in a probability tensor"
            ", 0.0 <= tensor <= 1.0"
        )


def cif_function(
    input: Tensor,
    alpha: Tensor,
    beta: float = 1.0,
    padding_mask: Optional[Tensor] = None,
    target_lengths: Optional[Tensor] = None,
    max_output_length: Optional[int] = None,
    eps: float = 1e-4,
) -> Tuple[Tensor, Tensor, Tensor]:
    r""" A batched computation implementation of continuous integrate and fire (CIF)
    https://arxiv.org/abs/1905.11235

    Args:
        input (Tensor): (N, S, C) Input features to be integrated.
        alpha (Tensor): (N, S) Weights corresponding to each elements in the
            input. It is expected to be after sigmoid function.
        beta (float): the threshold used for determine firing.
        padding_mask (Tensor, optional): (N, S) A binary mask representing
            padded elements in the input.
        target_lengths (Tensor, optional): (N,) Desired length of the targets
            for each sample in the minibatch.
        max_output_length (int, optional): The maximum valid output length used
            in inference. The alpha is scaled down if the sum exceeds this value.
        eps (float, optional): Epsilon to prevent underflow for divisions.
            Default: 1e-4

    Returns: Tuple (output, feat_lengths, alpha_sum)
        output (Tensor): (N, T, C) The output integrated from the source.
        feat_lengths (Tensor): (N,) The output length for each element in batch.
        alpha_sum (Tensor): (N,) The sum of alpha for each element in batch.
            Can be used to compute the quantity loss.
    """
    B, S, C = input.size()
    assert tuple(alpha.size()) == (B, S), f"{alpha.size()} != {(B, S)}"
    prob_check(alpha)

    dtype = alpha.dtype
    alpha = alpha.float()
    if padding_mask is not None:
        padding_mask = padding_mask.bool()
        alpha = alpha.masked_fill(padding_mask, 0)

    if target_lengths is not None:
        feat_lengths = target_lengths.long()
        desired_sum = beta * target_lengths.type_as(input)
        alpha_sum = alpha.sum(1)
        alpha = alpha * (desired_sum / alpha_sum).unsqueeze(1)
        T = feat_lengths.max()
    else:
        alpha_sum = alpha.sum(1)
        # make sure the output lengths are valid
        desired_sum = beta * alpha_sum.clip(min=1, max=max_output_length)
        alpha = alpha * (desired_sum / alpha_sum).unsqueeze(1)
        alpha_sum = alpha.sum(1)
        feat_lengths = (alpha_sum / beta + eps).floor().long()
        T = feat_lengths.max()

    # aggregate and integrate
    csum = alpha.cumsum(-1)
    with torch.no_grad():
        # indices used for scattering
        right_idx = (csum / beta + eps).floor().long()
        left_idx = right_idx.roll(1, dims=1)
        left_idx[:, 0] = 0

        # count # of fires from each source
        fire_num = right_idx - left_idx
        extra_weights = (fire_num - 1).clip(min=0)

        if right_idx.gt(T).any():
            import pdb
            pdb.set_trace()

        assert right_idx.le(T).all(), f"{right_idx} <= {T}"
        assert left_idx.le(T).all(), f"{right_idx} <= {T}"
        assert extra_weights.ge(0).all()

    # The extra entry in last dim is for
    output = input.new_zeros((B, T + 1, C))

    # right scatter
    fire_mask = fire_num > 0
    right_weight = torch.where(
        fire_mask,
        csum - right_idx.type_as(alpha) * beta,
        alpha
    ).type_as(input)
    output.scatter_add_(
        1,
        right_idx.unsqueeze(-1).expand(-1, -1, C),
        right_weight.unsqueeze(-1) * input
    )

    # left scatter
    left_weight = (
        alpha - right_weight - extra_weights.type_as(alpha) * beta
    ).type_as(input)
    output.scatter_add_(
        1,
        left_idx.unsqueeze(-1).expand(-1, -1, C),
        left_weight.unsqueeze(-1) * input
    )

    # extra scatters
    if extra_weights.ge(0).any():
        extra_steps = extra_weights.max().item()
        tgt_idx = left_idx
        src_feats = input * beta
        for _ in range(extra_steps):
            tgt_idx = (tgt_idx + 1).clip(max=T)
            # (B, S, 1)
            src_mask = (extra_weights > 0).unsqueeze(2)
            output.scatter_add_(
                1,
                tgt_idx.unsqueeze(-1).expand(-1, -1, C),
                src_feats * src_mask
            )
            extra_weights -= 1

    # tail handling
    if target_lengths is not None:
        # training time -> ignore tail
        output = output[:, :T, :]
    else:
        # find out contribution to output tail
        # note: w/o scaling, extra weight is all 0
        zero = right_weight.new_zeros((1,))
        r_mask = right_idx == feat_lengths.unsqueeze(1)
        tail_weights = torch.where(r_mask, right_weight, zero).sum(-1)
        l_mask = left_idx == feat_lengths.unsqueeze(1)
        tail_weights += torch.where(l_mask, left_weight, zero).sum(-1)

        # a size (B,) mask that removes non-firing position
        tail_mask = tail_weights < (beta / (2 + eps))

        # extend 1 fire
        feat_lengths[~tail_mask].add_(1)
        T = feat_lengths.max()
        output = output[:, :T, :]

        # a size (B, T) mask to erase weights
        tail_mask = torch.arange(T, device=output.device).unsqueeze(0) >= feat_lengths.unsqueeze(1)
        output[tail_mask] = 0

    return output, feat_lengths, alpha_sum.to(dtype)
