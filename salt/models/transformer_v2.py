"""Efficient Transformer implementation.

Updated transformer implementation based on
https://github.com/mistralai/mistral-src

Features:
- native SDP kernels (including flash)
- gated linear units https://arxiv.org/abs/2002.05202
- RMSNorm https://arxiv.org/abs/1910.07467
"""

from abc import ABC

import torch
from torch import BoolTensor, Size, Tensor, nn

import salt.models.layernorm as layernorms
from salt.stypes import Tensors


def merge_masks(
    q_mask: BoolTensor | None,
    kv_mask: BoolTensor | None,
    attn_mask: BoolTensor | None,
    q_shape: Size,
    k_shape: Size,
) -> BoolTensor:
    """Create a full attention mask which incorporates the padding information.

    Using pytorch transformer convention:
        False: Real node
        True:  Zero padded

    Parameters
    ----------
    q_mask : BoolTensor | None
        Mask for the queries, of shape (batch, q_len).
    kv_mask : BoolTensor | None
        Mask for the keys and values, of shape (batch, kv_len).
    attn_mask : BoolTensor | None
        Full attention mask, of shape (batch, q_len, kv_len).
    q_shape : Size
        Shape of the queries tensor, (batch, q_len, dim).
    k_shape : Size
        Shape of the keys tensor, (batch, kv_len, dim).
    """
    # Create the full mask which combines the attention and padding masks
    mask = None

    # if both masks exist, combine them
    if q_mask is not None and kv_mask is not None:
        mask = q_mask.unsqueeze(-1) | kv_mask.unsqueeze(-2)

    # if only one mask exists, expand it to the other dimension
    if q_mask is None and kv_mask is not None:
        mask = kv_mask.unsqueeze(-2).expand(-1, q_shape[-2], -1)
    if kv_mask is None and q_mask is not None:
        mask = q_mask.unsqueeze(-1).expand(-1, -1, k_shape[-2])

    # include the attention mask
    if attn_mask is not None:
        mask = attn_mask if mask is None else attn_mask | mask

    return mask


def repeat_kv(keys: Tensor, values: Tensor, repeats: int, dim: int):
    keys = torch.repeat_interleave(keys, repeats=repeats, dim=dim)
    values = torch.repeat_interleave(values, repeats=repeats, dim=dim)
    return keys, values


def torch_meff_attn(q: Tensor, k: Tensor, v: Tensor, mask: BoolTensor, dropout: float) -> Tensor:
    # masking can lead to nans, see
    # - https://github.com/pytorch/pytorch/issues/110213
    # - https://github.com/pytorch/pytorch/issues/103749
    # to get round this, can transform the mask from a bool to float
    # mask = (1.0 - mask.to(q.dtype)) * torch.finfo(q.dtype).min
    # but don't need this if add_zero_attn is True

    # TODO: change mask convention
    # https://gitlab.cern.ch/atlas-flavor-tagging-tools/algorithms/salt/-/issues/47
    if mask is not None:
        mask = ~mask.contiguous()

    return nn.functional.scaled_dot_product_attention(q, k, v, attn_mask=mask, dropout_p=dropout)


def torch_flash_attn(q: Tensor, k: Tensor, v: Tensor, mask: BoolTensor, dropout: float) -> Tensor:
    assert mask is None, "Flash attention does not support attention masks"
    with torch.backends.cuda.sdp_kernel(
        enable_flash=True, enable_math=False, enable_mem_efficient=False
    ):
        return nn.functional.scaled_dot_product_attention(
            q, k, v, attn_mask=mask, dropout_p=dropout
        )


ATTN_BACKENDS = {
    "torch-meff": torch_meff_attn,
    "torch-flash": torch_flash_attn,
}


class Attention(nn.Module, ABC):
    def __init__(
        self,
        embed_dim: int,
        num_heads: int,
        attn_type: str = "torch-meff",
        n_kv_heads: int | None = None,
        window_size: int | None = None,
        dropout: float = 0.0,
        bias: bool = True,
        add_zero_attn: bool = True,
    ):
        """Multihead attention module.

        Parameters
        ----------
        embed_dim : int
            Dimension of the input.
        num_heads : int
            Number of attention heads.
        attn_type : str, optional
            Type of backend kernel to use.
        n_kv_heads : int | None, optional
            Number of heads for the keys and values. If None, defaults to num_heads.
        window_size : int | None, optional
            Window size for flash attention kernel. If None, defaults to global attention.
        dropout : float, optional
            Dropout rate.
        bias : bool, optional
            Whether to include bias terms.
        add_zero_attn : bool, optional
            Whether to add a dummy token to attend to. This avoids nan when all tokens are padded.
        """
        super().__init__()

        self.embed_dim = embed_dim
        self.num_heads = num_heads
        self.head_dim = embed_dim // num_heads

        self.n_kv_heads = num_heads if n_kv_heads is None else n_kv_heads
        assert self.n_kv_heads is not None
        self.repeats = self.num_heads // self.n_kv_heads
        self.scale = self.head_dim**-0.5
        self.dropout = dropout
        self.bias = bias
        self.add_zero_attn = add_zero_attn

        self.attn_type = attn_type
        self.attn_func = ATTN_BACKENDS[self.attn_type]
        self.backend = self._flash_backend if self.attn_type == "flash" else self._torch_backend
        if window_size is None:
            self.window_size = (-1, -1)
        else:
            assert attn_type == "flash"
            assert window_size % 2 == 0
            self.window_size = (window_size // 2, window_size // 2)

        self.wq = nn.Linear(self.embed_dim, self.num_heads * self.head_dim, bias=self.bias)
        self.wk = nn.Linear(self.embed_dim, self.n_kv_heads * self.head_dim, bias=self.bias)
        self.wv = nn.Linear(self.embed_dim, self.n_kv_heads * self.head_dim, bias=self.bias)
        self.wo = nn.Linear(self.num_heads * self.head_dim, self.embed_dim, bias=self.bias)

    def forward(
        self,
        q: Tensor,
        k: Tensor,
        v: Tensor,
        q_mask: BoolTensor | None = None,
        kv_mask: BoolTensor | None = None,
        attn_mask: BoolTensor | None = None,
    ) -> Tensor:
        """Attention forward pass.

        Parameters
        ----------
        q : Tensor
            Queries of shape (batch, q_len, dim).
        k : Tensor
            Keys of shape (batch, kv_len, dim).
        v : Tensor
            Values of shape (batch, kv_len, dim).
        q_mask : BoolTensor, optional
            Mask for the queries, by default None.
        kv_mask : BoolTensor, optional
            Mask for the keys and values, by default None.
        attn_mask : BoolTensor, optional
            Full attention mask, by default None.

        Returns
        -------
        Tensor
            Output of shape (batch, q_len, dim).
        """
        # combine masks
        attn_mask = merge_masks(q_mask, kv_mask, attn_mask, q.shape, k.shape)

        # input projections
        q, k, v = self.wq(q), self.wk(k), self.wv(v)

        # add a dummy token to attend to - avoids nan when all tokens are padded
        if self.add_zero_attn:
            batch = q.shape[0]
            zero_attn_shape = (batch, 1, self.embed_dim)
            k = torch.cat([k, torch.zeros(zero_attn_shape, dtype=k.dtype, device=k.device)], dim=1)
            v = torch.cat([v, torch.zeros(zero_attn_shape, dtype=v.dtype, device=v.device)], dim=1)
            if attn_mask is not None:
                attn_mask = nn.functional.pad(attn_mask, (0, 1), value=False)
            if kv_mask is not None:
                kv_mask = nn.functional.pad(kv_mask, (0, 1), value=False)

        # run attention
        output = self.backend(q, k, v, attn_mask)

        # return output projection
        return self.wo(output)

    def _torch_backend(self, q: Tensor, k: Tensor, v: Tensor, attn_mask: BoolTensor | None = None):
        batch, q_len, _ = q.shape
        _, kv_len, _ = k.shape

        # transform tensors to (batch, num_heads, seq_len, head_dim)
        q = q.view(batch, q_len, self.num_heads, self.head_dim).transpose(1, 2)
        k = k.view(batch, kv_len, self.n_kv_heads, self.head_dim).transpose(1, 2)
        v = v.view(batch, kv_len, self.n_kv_heads, self.head_dim).transpose(1, 2)

        # repeat keys and values to match number of query heads
        if self.repeats > 1:
            k, v = repeat_kv(k, v, self.repeats, dim=-2)

        # expand mask to (batch, num_heads, q_len, kv_len)
        if attn_mask is not None:
            attn_mask = attn_mask.view(batch, 1, q_len, kv_len).expand(-1, self.num_heads, -1, -1)

        # run attention
        output = self.attn_func(q, k, v, mask=attn_mask, dropout=self.dropout)

        # recombine heads and return
        return output.transpose(1, 2).contiguous().view(batch, -1, self.embed_dim)


class SelfAttention(nn.Module):
    def __init__(self, embed_dim: int, **kwargs):
        """Self attention module.

        Parameters
        ----------
        embed_dim : int
            Dimension of the input.
        kwargs : dict
            Keyword arguments for
            [salt.models.transformer_v2.Attention][salt.models.transformer_v2.Attention].
        """
        super().__init__()
        self.embed_dim = embed_dim
        self.attention = Attention(embed_dim=embed_dim, **kwargs)

    def forward(self, x: Tensor, **kwargs) -> Tensor:
        return self.attention(x, x, x, **kwargs)


class CrossAttention(nn.Module):
    def __init__(self, embed_dim: int, **kwargs):
        super().__init__()
        self.embed_dim = embed_dim
        self.attention = Attention(embed_dim=embed_dim, **kwargs)

    def forward(self, q: Tensor, kv: Tensor, **kwargs) -> Tensor:
        return self.attention(q, kv, kv, **kwargs)


class GLU(nn.Module):
    def __init__(
        self,
        embed_dim: int,
        hidden_dim: int | None = None,
        activation: str = "ReLU",
        bias: bool = True,
        gated: bool = False,
    ):
        """Dense update with gated linear unit.

        See [2002.05202](https://arxiv.org/abs/2002.05202).

        Parameters
        ----------
        embed_dim : int
            Dimension of the input and output.
        hidden_dim : int | None, optional
            Dimension of the hidden layer. If None, defaults to embed_dim * 2.
        activation : str, optional
            Activation function.
        bias : bool, optional
            Whether to include bias in the linear layers.
        gated : bool, optional
            Whether to gate the output of the hidden layer.
        """
        super().__init__()

        if hidden_dim is None:
            hidden_dim = embed_dim * 2

        self.in_proj = nn.Linear(embed_dim, hidden_dim, bias=bias)
        self.out_proj = nn.Linear(hidden_dim, embed_dim, bias=bias)
        self.gate = None
        if gated:
            self.gate = nn.Linear(embed_dim, hidden_dim, bias=bias)
        self.activation = getattr(nn, activation)()

    def forward(self, x: Tensor) -> Tensor:
        out = self.activation(self.in_proj(x))
        if self.gate:
            out = out * self.gate(x)
        return self.out_proj(out)


class EncoderLayer(nn.Module):
    def __init__(
        self,
        embed_dim: int,
        norm: str = "LayerNorm",
        dense_kwargs: dict | None = None,
        attn_kwargs: dict | None = None,
    ):
        """Encoder layer consisting of a self-attention and a feed-forward layer.

        Parameters
        ----------
        embed_dim : int
            Dimension of the embeddings at each layer.
        norm : str, optional
            Normalization style, by default "LayerNorm".
        dense_kwargs : dict | None, optional
            Keyword arguments for [salt.models.transformer_v2.GLU][salt.models.transformer_v2.GLU].
        attn_kwargs : dict | None, optional
            Keyword arguments for
            [salt.models.transformer_v2.SelfAttention][salt.models.transformer_v2.SelfAttention].
        """
        super().__init__()
        if attn_kwargs is None:
            attn_kwargs = {}
        if dense_kwargs is None:
            dense_kwargs = {}
        self.embed_dim = embed_dim
        self.attn = SelfAttention(embed_dim=embed_dim, **attn_kwargs)
        self.attn_norm = getattr(layernorms, norm)(embed_dim)
        self.dense = GLU(embed_dim, **dense_kwargs)
        self.dense_norm = getattr(layernorms, norm)(embed_dim)

    def forward(self, x: Tensor, pad_mask: BoolTensor) -> Tensor:
        x = x + self.attn(self.attn_norm(x), kv_mask=pad_mask)
        return x + self.dense(self.dense_norm(x))


class DecoderLayer(nn.Module):
    def __init__(
        self,
        embed_dim: int,
        norm: str = "LayerNorm",
        dense_kwargs: dict | None = None,
        attn_kwargs: dict | None = None,
    ):
        super().__init__()
        if attn_kwargs is None:
            attn_kwargs = {}
        if dense_kwargs is None:
            dense_kwargs = {}
        self.embed_dim = embed_dim
        self.attn = CrossAttention(embed_dim=embed_dim, **attn_kwargs)
        self.q_norm = getattr(layernorms, norm)(embed_dim)
        self.kv_norm = getattr(layernorms, norm)(embed_dim)
        self.dense = GLU(embed_dim, **dense_kwargs)
        self.dense_norm = getattr(layernorms, norm)(embed_dim)

    def forward(self, x: Tensor, kv: Tensor, pad_mask: BoolTensor) -> Tensor:
        x = x + self.attn(self.q_norm(x), self.kv_norm(kv), kv_mask=pad_mask)
        return x + self.dense(self.dense_norm(x))


class TransformerV2(nn.Module):
    def __init__(
        self,
        num_layers: int,
        embed_dim: int,
        out_dim: int | None = None,
        norm: str = "LayerNorm",
        **kwargs,
    ):
        """Transformer model consisting of a series of stacked Transformer encoder layers.

        Parameters
        ----------
        num_layers : int
            Number of layers.
        embed_dim : int
            Dimension of the embeddings at each layer.
        out_dim : int | None, optional
            Optionally project the output to a different dimension.
        norm : str, optional
            Normalization style, by default "LayerNorm".
        kwargs : dict
            Keyword arguments for [salt.models.transformer_v2.EncoderLayer].
        """
        super().__init__()
        self.num_layers = num_layers
        self.embed_dim = embed_dim

        self.layers = torch.nn.ModuleList([
            EncoderLayer(embed_dim=embed_dim, norm=norm, **kwargs) for _ in range(num_layers)
        ])
        self.out_norm = getattr(layernorms, norm)(embed_dim if out_dim is None else out_dim)
        self.out_proj = None
        if out_dim is not None:
            self.out_proj = nn.Linear(self.embed_dim, out_dim)
        self.featurewise = nn.ModuleList()

    def forward(
        self,
        x: Tensor,
        pad_mask: BoolTensor,
        inputs: Tensors = None,
    ) -> Tensor:
        if isinstance(x, dict):
            x = torch.cat(list(x.values()), dim=1)
        if isinstance(pad_mask, dict):
            pad_mask = torch.cat(list(pad_mask.values()), dim=1)

        for i, layer in enumerate(self.layers):
            if len(self.featurewise) > 0:
                x = self.featurewise[i](inputs, x)
            x = layer(x, pad_mask)
        if self.out_proj is not None:
            x = self.out_proj(x)
        return self.out_norm(x)
