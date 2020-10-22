# coding=utf-8
# Copyright 2018 The OpenAI Team Authors and HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
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
"""PyTorch OpenAI GPT-2 model."""

import os
import warnings
from dataclasses import dataclass
from typing import List, Optional, Tuple

import torch
import torch.nn as nn
from torch.nn import CrossEntropyLoss

from .activations import ACT2FN
from .configuration_gpt2 import GPT2Config
from .file_utils import (
    ModelOutput,
    add_code_sample_docstrings,
    add_start_docstrings,
    add_start_docstrings_to_callable,
    replace_return_docstrings,
)
from .modeling_outputs import BaseModelOutputWithPast, CausalLMOutputWithPast
from .modeling_utils import (
    Conv1D,
    PreTrainedModel,
    SequenceSummary,
    find_pruneable_heads_and_indices,
    prune_conv1d_layer,
)
from .utils import logging


logger = logging.get_logger(__name__)

_CONFIG_FOR_DOC = "GPT2Config"
_TOKENIZER_FOR_DOC = "GPT2Tokenizer"

GPT2_PRETRAINED_MODEL_ARCHIVE_LIST = [
    "gpt2",
    "gpt2-medium",
    "gpt2-large",
    "gpt2-xl",
    "distilgpt2",
    # See all GPT-2 models at https://huggingface.co/models?filter=gpt2
]


def load_tf_weights_in_gpt2(model, config, gpt2_checkpoint_path):
    """Load tf checkpoints in a pytorch model"""
    try:
        import re

        import tensorflow as tf
    except ImportError:
        logger.error(
            "Loading a TensorFlow model in PyTorch, requires TensorFlow to be installed. Please see "
            "https://www.tensorflow.org/install/ for installation instructions."
        )
        raise
    tf_path = os.path.abspath(gpt2_checkpoint_path)
    logger.info("Converting TensorFlow checkpoint from {}".format(tf_path))
    # Load weights from TF model
    init_vars = tf.train.list_variables(tf_path)
    names = []
    arrays = []
    for name, shape in init_vars:
        logger.info("Loading TF weight {} with shape {}".format(name, shape))
        array = tf.train.load_variable(tf_path, name)
        names.append(name)
        arrays.append(array.squeeze())

    for name, array in zip(names, arrays):
        name = name[6:]  # skip "model/"
        name = name.split("/")
        pointer = model
        for m_name in name:
            if re.fullmatch(r"[A-Za-z]+\d+", m_name):
                scope_names = re.split(r"(\d+)", m_name)
            else:
                scope_names = [m_name]
            if scope_names[0] == "w" or scope_names[0] == "g":
                pointer = getattr(pointer, "weight")
            elif scope_names[0] == "b":
                pointer = getattr(pointer, "bias")
            elif scope_names[0] == "wpe" or scope_names[0] == "wte":
                pointer = getattr(pointer, scope_names[0])
                pointer = getattr(pointer, "weight")
            else:
                pointer = getattr(pointer, scope_names[0])
            if len(scope_names) >= 2:
                num = int(scope_names[1])
                pointer = pointer[num]
        try:
            assert (
                pointer.shape == array.shape
            ), f"Pointer shape {pointer.shape} and array shape {array.shape} mismatched"
        except AssertionError as e:
            e.args += (pointer.shape, array.shape)
            raise
        logger.info("Initialize PyTorch weight {}".format(name))
        pointer.data = torch.from_numpy(array)
    return model


class Attention(nn.Module):
    def __init__(self, nx, n_ctx, config, scale=False, is_cross_attention=False):
        super().__init__()

        n_state = nx  # in Attention: n_state=768 (nx=n_embd)
        # [switch nx => n_state from Block to Attention to keep identical to TF implem]
        assert n_state % config.n_head == 0
        self.register_buffer(
            "bias", torch.tril(torch.ones((n_ctx, n_ctx), dtype=torch.uint8)).view(1, 1, n_ctx, n_ctx)
        )
        self.register_buffer("masked_bias", torch.tensor(-1e4))
        self.n_head = config.n_head
        self.split_size = n_state
        self.scale = scale
        self.is_cross_attention = is_cross_attention
        if self.is_cross_attention:
            self.c_attn = Conv1D(2 * n_state, nx)
            self.q_attn = Conv1D(n_state, nx)
        else:
            self.c_attn = Conv1D(3 * n_state, nx)
        self.c_proj = Conv1D(n_state, nx)
        self.attn_dropout = nn.Dropout(config.attn_pdrop)
        self.resid_dropout = nn.Dropout(config.resid_pdrop)
        self.pruned_heads = set()

    def prune_heads(self, heads):
        if len(heads) == 0:
            return
        heads, index = find_pruneable_heads_and_indices(
            heads, self.n_head, self.split_size // self.n_head, self.pruned_heads
        )
        index_attn = torch.cat([index, index + self.split_size, index + (2 * self.split_size)])

        # Prune conv1d layers
        self.c_attn = prune_conv1d_layer(self.c_attn, index_attn, dim=1)
        self.c_proj = prune_conv1d_layer(self.c_proj, index, dim=0)

        # Update hyper params
        self.split_size = (self.split_size // self.n_head) * (self.n_head - len(heads))
        self.n_head = self.n_head - len(heads)
        self.pruned_heads = self.pruned_heads.union(heads)

    def _attn(self, q, k, v, attention_mask=None, head_mask=None, output_attentions=False):
        w = torch.matmul(q, k)
        if self.scale:
            w = w / (float(v.size(-1)) ** 0.5)
        nd, ns = w.size(-2), w.size(-1)

        if not self.is_cross_attention:
            # if only "normal" attention layer implements causal mask
            mask = self.bias[:, :, ns - nd : ns, :ns]
            w = torch.where(mask.bool(), w, self.masked_bias.to(w.dtype))

        if attention_mask is not None:
            # Apply the attention mask
            w = w + attention_mask

        w = nn.Softmax(dim=-1)(w)
        w = self.attn_dropout(w)

        # Mask heads if we want to
        if head_mask is not None:
            w = w * head_mask

        outputs = [torch.matmul(w, v)]
        if output_attentions:
            outputs.append(w)
        return outputs

    def merge_heads(self, x):
        x = x.permute(0, 2, 1, 3).contiguous()
        new_x_shape = x.size()[:-2] + (x.size(-2) * x.size(-1),)
        return x.view(*new_x_shape)  # in Tensorflow implem: fct merge_states

    def split_heads(self, x, k=False):
        new_x_shape = x.size()[:-1] + (self.n_head, x.size(-1) // self.n_head)
        x = x.view(*new_x_shape)  # in Tensorflow implem: fct split_states
        if k:
            return x.permute(0, 2, 3, 1)  # (batch, head, head_features, seq_length)
        else:
            return x.permute(0, 2, 1, 3)  # (batch, head, seq_length, head_features)

    def forward(
        self,
        hidden_states,
        layer_past=None,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        use_cache=False,
        output_attentions=False,
    ):
        if encoder_hidden_states is not None:
            assert hasattr(
                self, "q_attn"
            ), "If class is used as cross attention, the weights `q_attn` have to be defined. Please make sure to instantiate class with `Attention(..., is_cross_attention=True)`."
            query = self.q_attn(hidden_states)
            key, value = self.c_attn(encoder_hidden_states).split(self.split_size, dim=2)
            attention_mask = encoder_attention_mask
        else:
            query, key, value = self.c_attn(hidden_states).split(self.split_size, dim=2)

        query = self.split_heads(query)
        key = self.split_heads(key, k=True)
        value = self.split_heads(value)
        if layer_past is not None:
            past_key, past_value = layer_past[0].transpose(-2, -1), layer_past[1]  # transpose back cf below
            key = torch.cat((past_key, key), dim=-1)
            value = torch.cat((past_value, value), dim=-2)

        if use_cache is True:
            present = torch.stack((key.transpose(-2, -1), value))  # transpose to have same shapes for stacking
        else:
            present = (None,)

        attn_outputs = self._attn(query, key, value, attention_mask, head_mask, output_attentions)
        a = attn_outputs[0]

        a = self.merge_heads(a)
        a = self.c_proj(a)
        a = self.resid_dropout(a)

        outputs = [a, present] + attn_outputs[1:]
        return outputs  # a, present, (attentions)


class MLP(nn.Module):
    def __init__(self, n_state, config):  # in MLP: n_state=3072 (4 * n_embd)
        super().__init__()
        nx = config.n_embd
        self.c_fc = Conv1D(n_state, nx)
        self.c_proj = Conv1D(nx, n_state)
        self.act = ACT2FN[config.activation_function]
        self.dropout = nn.Dropout(config.resid_pdrop)

    def forward(self, x):
        h = self.act(self.c_fc(x))
        h2 = self.c_proj(h)
        return self.dropout(h2)


class Block(nn.Module):
    def __init__(self, n_ctx, config, scale=False):
        super().__init__()
        hidden_size = config.n_embd
        inner_dim = config.n_inner if config.n_inner is not None else 4 * hidden_size
        self.ln_1 = nn.LayerNorm(hidden_size, eps=config.layer_norm_epsilon)
        self.attn = Attention(hidden_size, n_ctx, config, scale)
        self.ln_2 = nn.LayerNorm(hidden_size, eps=config.layer_norm_epsilon)
        if config.add_cross_attention:
            self.crossattention = Attention(hidden_size, n_ctx, config, scale, is_cross_attention=True)
            self.ln_cross_attn = nn.LayerNorm(hidden_size, eps=config.layer_norm_epsilon)
        self.mlp = MLP(inner_dim, config)

    def forward(
        self,
        hidden_states,
        layer_past=None,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        use_cache=False,
        output_attentions=False,
    ):
        attn_outputs = self.attn(
            self.ln_1(hidden_states),
            layer_past=layer_past,
            attention_mask=attention_mask,
            head_mask=head_mask,
            use_cache=use_cache,
            output_attentions=output_attentions,
        )
        attn_output = attn_outputs[0]  # output_attn: a, present, (attentions)
        outputs = attn_outputs[1:]
        # residual connection
        hidden_states = attn_output + hidden_states

        if encoder_hidden_states is not None:
            # add one self-attention block for cross-attention
            assert hasattr(
                self, "crossattention"
            ), f"If `encoder_hidden_states` are passed, {self} has to be instantiated with cross-attention layers by setting `config.add_cross_attention=True`"
            cross_attn_outputs = self.crossattention(
                self.ln_cross_attn(hidden_states),
                attention_mask=attention_mask,
                head_mask=head_mask,
                encoder_hidden_states=encoder_hidden_states,
                encoder_attention_mask=encoder_attention_mask,
                output_attentions=output_attentions,
            )
            attn_output = cross_attn_outputs[0]
            # residual connection
            hidden_states = hidden_states + attn_output
            outputs = outputs + cross_attn_outputs[1:]  # add cross attentions if we output attention weights

        feed_forward_hidden_states = self.mlp(self.ln_2(hidden_states))
        # residual connection
        hidden_states = hidden_states + feed_forward_hidden_states

        outputs = [hidden_states] + outputs
        return outputs  # hidden_states, present, (cross_attentions, attentions)


class GPT2PreTrainedModel(PreTrainedModel):
    """An abstract class to handle weights initialization and
    a simple interface for downloading and loading pretrained models.
    """

    config_class = GPT2Config
    load_tf_weights = load_tf_weights_in_gpt2
    base_model_prefix = "transformer"

    def __init__(self, *inputs, **kwargs):
        super().__init__(*inputs, **kwargs)

    def _init_weights(self, module):
        """Initialize the weights."""
        if isinstance(module, (nn.Linear, nn.Embedding, Conv1D)):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
            if isinstance(module, (nn.Linear, Conv1D)) and module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)


@dataclass
class GPT2DoubleHeadsModelOutput(ModelOutput):
    """
    Base class for outputs of models predicting if two sentences are consecutive or not.

    Args:
        loss (:obj:`torch.FloatTensor` of shape :obj:`(1,)`, `optional`, returned when ``labels`` is provided):
            Language modeling loss.
        mc_loss (:obj:`torch.FloatTensor` of shape :obj:`(1,)`, `optional`, returned when :obj:`mc_labels` is provided):
            Multiple choice classification loss.
        logits (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, num_choices, sequence_length, config.vocab_size)`):
            Prediction scores of the language modeling head (scores for each vocabulary token before SoftMax).
        mc_logits (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, num_choices)`):
            Prediction scores of the multiple choice classification head (scores for each choice before SoftMax).
        past_key_values (:obj:`List[torch.FloatTensor]`, `optional`, returned when ``use_cache=True`` is passed or when ``config.use_cache=True``):
            List of :obj:`torch.FloatTensor` of length :obj:`config.n_layers`,  with each tensor of shape
            :obj:`(2, batch_size, num_heads, sequence_length, embed_size_per_head)`).

            Contains pre-computed hidden-states (key and values in the attention blocks) that can be used (see
            ``past_key_values`` input) to speed up sequential decoding.
        hidden_states (:obj:`tuple(torch.FloatTensor)`, `optional`, returned when ``output_hidden_states=True`` is passed or when ``config.output_hidden_states=True``):
            Tuple of :obj:`torch.FloatTensor` (one for the output of the embeddings + one for the output of each layer)
            of shape :obj:`(batch_size, sequence_length, hidden_size)`.

            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        attentions (:obj:`tuple(torch.FloatTensor)`, `optional`, returned when ``output_attentions=True`` is passed or when ``config.output_attentions=True``):
            Tuple of :obj:`torch.FloatTensor` (one for each layer) of shape
            :obj:`(batch_size, num_heads, sequence_length, sequence_length)`.

            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
            heads.
    """

    loss: Optional[torch.FloatTensor] = None
    mc_loss: Optional[torch.FloatTensor] = None
    logits: torch.FloatTensor = None
    mc_logits: torch.FloatTensor = None
    past_key_values: Optional[List[torch.FloatTensor]] = None
    hidden_states: Optional[Tuple[torch.FloatTensor]] = None
    attentions: Optional[Tuple[torch.FloatTensor]] = None


GPT2_START_DOCSTRING = r"""

    This model inherits from :class:`~transformers.PreTrainedModel`. Check the superclass documentation for the generic
    methods the library implements for all its model (such as downloading or saving, resizing the input embeddings,
    pruning heads etc.)

    This model is also a PyTorch `torch.nn.Module <https://pytorch.org/docs/stable/nn.html#torch.nn.Module>`__ subclass.
    Use it as a regular PyTorch Module and refer to the PyTorch documentation for all matter related to general
    usage and behavior.

    Parameters:
        config (:class:`~transformers.GPT2Config`): Model configuration class with all the parameters of the model.
            Initializing with a config file does not load the weights associated with the model, only the configuration.
            Check out the :meth:`~transformers.PreTrainedModel.from_pretrained` method to load the model weights.
"""

GPT2_INPUTS_DOCSTRING = r"""
    Args:
        input_ids (:obj:`torch.LongTensor` of shape :obj:`(batch_size, input_ids_length)`):
            :obj:`input_ids_length` = ``sequence_length`` if ``past_key_values`` is ``None`` else
            ``past_key_values[0].shape[-2]`` (``sequence_length`` of input past key value states).
            Indices of input sequence tokens in the vocabulary.

            If ``past_key_values`` is used, only ``input_ids`` that do not have their past calculated should be passed
            as ``input_ids``.

            Indices can be obtained using :class:`~transformers.GPT2Tokenizer`.
            See :meth:`transformers.PreTrainedTokenizer.encode` and
            :meth:`transformers.PreTrainedTokenizer.__call__` for details.

            `What are input IDs? <../glossary.html#input-ids>`__
        past_key_values (:obj:`List[torch.FloatTensor]` of length :obj:`config.n_layers`):
            Contains precomputed hidden-states (key and values in the attention blocks) as computed by the model
            (see ``past_key_values`` output below). Can be used to speed up sequential decoding.
            The ``input_ids`` which have their past given to this model should not be passed as ``input_ids`` as they
            have already been computed.
        attention_mask (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`):
            Mask to avoid performing attention on padding token indices.
            Mask values selected in ``[0, 1]``:

            - 1 for tokens that are **not masked**,
            - 0 for tokens that are **maked**.

            `What are attention masks? <../glossary.html#attention-mask>`__
        token_type_ids (:obj:`torch.LongTensor` of shape :obj:`(batch_size, input_ids_length)`, `optional`):
            Segment token indices to indicate first and second portions of the inputs.
            Indices are selected in ``[0, 1]``:

            - 0 corresponds to a `sentence A` token,
            - 1 corresponds to a `sentence B` token.

            `What are token type IDs? <../glossary.html#token-type-ids>`_
        position_ids (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`):
            Indices of positions of each input sequence tokens in the position embeddings.
            Selected in the range ``[0, config.max_position_embeddings - 1]``.

            `What are position IDs? <../glossary.html#position-ids>`_
        head_mask (:obj:`torch.FloatTensor` of shape :obj:`(num_heads,)` or :obj:`(num_layers, num_heads)`, `optional`):
            Mask to nullify selected heads of the self-attention modules.
            Mask values selected in ``[0, 1]``:

            - 1 indicates the head is **not masked**,
            - 0 indicates the head is **masked**.

        inputs_embeds (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_length, hidden_size)`, `optional`):
            Optionally, instead of passing :obj:`input_ids` you can choose to directly pass an embedded representation.
            This is useful if you want more control over how to convert :obj:`input_ids` indices into associated
            vectors than the model's internal embedding lookup matrix.

            If ``past_key_values`` is used, optionally only the last :obj:`inputs_embeds` have to be input (see
            ``past_key_values``).
        use_cache (:obj:`bool`, `optional`):
            If set to :obj:`True`, ``past_key_values`` key value states are returned and can be used to speed up
            decoding (see ``past_key_values``).
        output_attentions (:obj:`bool`, `optional`):
            Whether or not to return the attentions tensors of all attention layers. See ``attentions`` under returned
            tensors for more detail.
        output_hidden_states (:obj:`bool`, `optional`):
            Whether or not to return the hidden states of all layers. See ``hidden_states`` under returned tensors for
            more detail.
        return_dict (:obj:`bool`, `optional`):
            Whether or not to return a :class:`~transformers.file_utils.ModelOutput` instead of a plain tuple.
"""


@add_start_docstrings(
    "The bare GPT2 Model transformer outputting raw hidden-states without any specific head on top.",
    GPT2_START_DOCSTRING,
)
class GPT2Model(GPT2PreTrainedModel):
    def __init__(self, config):
        super().__init__(config)

        self.wte = nn.Embedding(config.vocab_size, config.n_embd)
        self.wpe = nn.Embedding(config.n_positions, config.n_embd)
        self.drop = nn.Dropout(config.embd_pdrop)
        self.h = nn.ModuleList([Block(config.n_ctx, config, scale=True) for _ in range(config.n_layer)])
        self.ln_f = nn.LayerNorm(config.n_embd, eps=config.layer_norm_epsilon)

        self.init_weights()

    def get_input_embeddings(self):
        return self.wte

    def set_input_embeddings(self, new_embeddings):
        self.wte = new_embeddings

    def _prune_heads(self, heads_to_prune):
        """Prunes heads of the model.
        heads_to_prune: dict of {layer_num: list of heads to prune in this layer}
        """
        for layer, heads in heads_to_prune.items():
            self.h[layer].attn.prune_heads(heads)

    @add_start_docstrings_to_callable(GPT2_INPUTS_DOCSTRING)
    @add_code_sample_docstrings(
        tokenizer_class=_TOKENIZER_FOR_DOC,
        checkpoint="gpt2",
        output_type=BaseModelOutputWithPast,
        config_class=_CONFIG_FOR_DOC,
    )
    def forward(
        self,
        input_ids=None,
        past_key_values=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        **kwargs,
    ):
        if "past" in kwargs:
            warnings.warn(
                "The `past` argument is deprecated and will be removed in a future version, use `past_key_values` instead.",
                FutureWarning,
            )
            past_key_values = kwargs.pop("past")
        assert kwargs == {}, f"Unexpected keyword arguments: {list(kwargs.keys())}."

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            input_shape = input_ids.size()
            input_ids = input_ids.view(-1, input_shape[-1])
            batch_size = input_ids.shape[0]
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.size()[:-1]
            batch_size = inputs_embeds.shape[0]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        if token_type_ids is not None:
            token_type_ids = token_type_ids.view(-1, input_shape[-1])
        if position_ids is not None:
            position_ids = position_ids.view(-1, input_shape[-1])

        if past_key_values is None:
            past_length = 0
            past_key_values = [None] * len(self.h)
        else:
            past_length = past_key_values[0][0].size(-2)
        if position_ids is None:
            device = input_ids.device if input_ids is not None else inputs_embeds.device
            position_ids = torch.arange(past_length, input_shape[-1] + past_length, dtype=torch.long, device=device)
            position_ids = position_ids.unsqueeze(0).view(-1, input_shape[-1])

        # Attention mask.
        if attention_mask is not None:
            assert batch_size > 0, "batch_size has to be defined and > 0"
            attention_mask = attention_mask.view(batch_size, -1)
            # We create a 3D attention mask from a 2D tensor mask.
            # Sizes are [batch_size, 1, 1, to_seq_length]
            # So we can broadcast to [batch_size, num_heads, from_seq_length, to_seq_length]
            # this attention mask is more simple than the triangular masking of causal attention
            # used in OpenAI GPT, we just need to prepare the broadcast dimension here.
            attention_mask = attention_mask[:, None, None, :]

            # Since attention_mask is 1.0 for positions we want to attend and 0.0 for
            # masked positions, this operation will create a tensor which is 0.0 for
            # positions we want to attend and -10000.0 for masked positions.
            # Since we are adding it to the raw scores before the softmax, this is
            # effectively the same as removing these entirely.
            attention_mask = attention_mask.to(dtype=next(self.parameters()).dtype)  # fp16 compatibility
            attention_mask = (1.0 - attention_mask) * -10000.0

        # If a 2D ou 3D attention mask is provided for the cross-attention
        # we need to make broadcastabe to [batch_size, num_heads, seq_length, seq_length]
        if self.config.add_cross_attention and encoder_hidden_states is not None:
            encoder_batch_size, encoder_sequence_length, _ = encoder_hidden_states.size()
            encoder_hidden_shape = (encoder_batch_size, encoder_sequence_length)
            if encoder_attention_mask is None:
                encoder_attention_mask = torch.ones(encoder_hidden_shape, device=device)
            encoder_attention_mask = self.invert_attention_mask(encoder_attention_mask)
        else:
            encoder_attention_mask = None

        # Prepare head mask if needed
        # 1.0 in head_mask indicate we keep the head
        # attention_probs has shape bsz x n_heads x N x N
        # head_mask has shape n_layer x batch x n_heads x N x N
        head_mask = self.get_head_mask(head_mask, self.config.n_layer)

        if inputs_embeds is None:
            inputs_embeds = self.wte(input_ids)
        position_embeds = self.wpe(position_ids)
        if token_type_ids is not None:
            token_type_embeds = self.wte(token_type_ids)
        else:
            token_type_embeds = 0
        hidden_states = inputs_embeds + position_embeds + token_type_embeds
        hidden_states = self.drop(hidden_states)

        output_shape = input_shape + (hidden_states.size(-1),)

        presents = () if use_cache else None
        all_attentions = () if output_attentions else None
        all_hidden_states = () if output_hidden_states else None
        for i, (block, layer_past) in enumerate(zip(self.h, past_key_values)):
            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states.view(*output_shape),)

            if getattr(self.config, "gradient_checkpointing", False):

                def create_custom_forward(module):
                    def custom_forward(*inputs):
                        # checkpointing only works with tuple returns, not with lists
                        return tuple(output for output in module(*inputs, use_cache, output_attentions))

                    return custom_forward

                outputs = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(block),
                    hidden_states,
                    layer_past,
                    attention_mask,
                    head_mask[i],
                    encoder_hidden_states,
                    encoder_attention_mask,
                )
            else:
                outputs = block(
                    hidden_states,
                    layer_past=layer_past,
                    attention_mask=attention_mask,
                    head_mask=head_mask[i],
                    encoder_hidden_states=encoder_hidden_states,
                    encoder_attention_mask=encoder_attention_mask,
                    use_cache=use_cache,
                    output_attentions=output_attentions,
                )

            hidden_states, present = outputs[:2]
            if use_cache is True:
                presents = presents + (present,)

            if output_attentions:
                all_attentions = all_attentions + (outputs[2],)

        hidden_states = self.ln_f(hidden_states)

        hidden_states = hidden_states.view(*output_shape)
        # Add last hidden state
        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        if not return_dict:
            return tuple(v for v in [hidden_states, presents, all_hidden_states, all_attentions] if v is not None)

        return BaseModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=presents,
            hidden_states=all_hidden_states,
            attentions=all_attentions,
        )


@add_start_docstrings(
    """The GPT2 Model transformer with a language modeling head on top
    (linear layer with weights tied to the input embeddings). """,
    GPT2_START_DOCSTRING,
)
class GPT2LMHeadModel(GPT2PreTrainedModel):
    authorized_missing_keys = [r"h\.\d+\.attn\.masked_bias", r"lm_head\.weight"]

    def __init__(self, config):
        super().__init__(config)
        self.transformer = GPT2Model(config)
        self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)

        self.init_weights()

    def get_output_embeddings(self):
        return self.lm_head

    def prepare_inputs_for_generation(self, input_ids, past=None, **kwargs):
        # only last token for inputs_ids if past is defined in kwargs
        if past:
            input_ids = input_ids[:, -1].unsqueeze(-1)

        return {
            "input_ids": input_ids,
            "past_key_values": past,
            "use_cache": kwargs.get("use_cache"),
        }

    @add_start_docstrings_to_callable(GPT2_INPUTS_DOCSTRING)
    @add_code_sample_docstrings(
        tokenizer_class=_TOKENIZER_FOR_DOC,
        checkpoint="gpt2",
        output_type=CausalLMOutputWithPast,
        config_class=_CONFIG_FOR_DOC,
    )
    def forward(
        self,
        input_ids=None,
        past_key_values=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        labels=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        **kwargs,
    ):
        r"""
        labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`):
            Labels for language modeling.
            Note that the labels **are shifted** inside the model, i.e. you can set ``labels = input_ids``
            Indices are selected in ``[-100, 0, ..., config.vocab_size]``
            All labels set to ``-100`` are ignored (masked), the loss is only
            computed for labels in ``[0, ..., config.vocab_size]``
        """
        if "past" in kwargs:
            warnings.warn(
                "The `past` argument is deprecated and will be removed in a future version, use `past_key_values` instead.",
                FutureWarning,
            )
            past_key_values = kwargs.pop("past")
        assert kwargs == {}, f"Unexpected keyword arguments: {list(kwargs.keys())}."
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        transformer_outputs = self.transformer(
            input_ids,
            past_key_values=past_key_values,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            encoder_hidden_states=encoder_hidden_states,
            encoder_attention_mask=encoder_attention_mask,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        hidden_states = transformer_outputs[0]

        lm_logits = self.lm_head(hidden_states)

        loss = None
        if labels is not None:
            # Shift so that tokens < n predict n
            shift_logits = lm_logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            # Flatten the tokens
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))

        if not return_dict:
            output = (lm_logits,) + transformer_outputs[1:]
            return ((loss,) + output) if loss is not None else output

        return CausalLMOutputWithPast(
            loss=loss,
            logits=lm_logits,
            past_key_values=transformer_outputs.past_key_values,
            hidden_states=transformer_outputs.hidden_states,
            attentions=transformer_outputs.attentions,
        )


@add_start_docstrings(
    """The GPT2 Model transformer with a language modeling and a multiple-choice classification
    head on top e.g. for RocStories/SWAG tasks. The two heads are two linear layers.
    The language modeling head has its weights tied to the input embeddings,
    the classification head takes as input the input of a specified classification token index in the input sequence).
""",
    GPT2_START_DOCSTRING,
)
class GPT2DoubleHeadsModel(GPT2PreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        config.num_labels = 1
        self.transformer = GPT2Model(config)
        self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)
        self.multiple_choice_head = SequenceSummary(config)

        self.init_weights()

    def get_output_embeddings(self):
        return self.lm_head

    def prepare_inputs_for_generation(self, input_ids, past=None, **kwargs):
        # only last token for inputs_ids if past is defined in kwargs
        if past:
            input_ids = input_ids[:, -1].unsqueeze(-1)

        return {
            "input_ids": input_ids,
            "past_key_values": past,
            "use_cache": kwargs.get("use_cache"),
        }

    @add_start_docstrings_to_callable(GPT2_INPUTS_DOCSTRING)
    @replace_return_docstrings(output_type=GPT2DoubleHeadsModelOutput, config_class=_CONFIG_FOR_DOC)
    def forward(
        self,
        input_ids=None,
        past_key_values=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        mc_token_ids=None,
        labels=None,
        mc_labels=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        **kwargs,
    ):
        r"""
        mc_token_ids (:obj:`torch.LongTensor` of shape :obj:`(batch_size, num_choices)`, `optional`, default to index of the last token of the input)
            Index of the classification token in each input sequence.
            Selected in the range ``[0, input_ids.size(-1) - 1[``.
        labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`)
            Labels for language modeling.
            Note that the labels **are shifted** inside the model, i.e. you can set ``labels = input_ids``
            Indices are selected in ``[-1, 0, ..., config.vocab_size]``
            All labels set to ``-100`` are ignored (masked), the loss is only
            computed for labels in ``[0, ..., config.vocab_size]``
        mc_labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size)`, `optional`)
            Labels for computing the multiple choice classification loss.
            Indices should be in ``[0, ..., num_choices]`` where `num_choices` is the size of the second dimension
            of the input tensors. (see `input_ids` above)
        kwargs (:obj:`Dict[str, any]`, optional, defaults to `{}`):
            Used to hide legacy arguments that have been deprecated.

        Return:

        Example::

            >>> import torch
            >>> from transformers import GPT2Tokenizer, GPT2DoubleHeadsModel

            >>> tokenizer = GPT2Tokenizer.from_pretrained('gpt2')
            >>> model = GPT2DoubleHeadsModel.from_pretrained('gpt2, return_dict=True)

            >>> # Add a [CLS] to the vocabulary (we should train it also!)
            >>> num_added_tokens = tokenizer.add_special_tokens({'cls_token': '[CLS]'})

            >>> embedding_layer = model.resize_token_embeddings(len(tokenizer))  # Update the model embeddings with the new vocabulary size

            >>> choices = ["Hello, my dog is cute [CLS]", "Hello, my cat is cute [CLS]"]
            >>> encoded_choices = [tokenizer.encode(s) for s in choices]
            >>> cls_token_location = [tokens.index(tokenizer.cls_token_id) for tokens in encoded_choices]

            >>> input_ids = torch.tensor(encoded_choices).unsqueeze(0)  # Batch size: 1, number of choices: 2
            >>> mc_token_ids = torch.tensor([cls_token_location])  # Batch size: 1

            >>> outputs = model(input_ids, mc_token_ids=mc_token_ids)
            >>> lm_logits = outputs.lm_logits
            >>> mc_logits = outputs.mc_logits

        """
        if "lm_labels" in kwargs:
            warnings.warn(
                "The `lm_labels` argument is deprecated and will be removed in a future version, use `labels` instead.",
                FutureWarning,
            )
            labels = kwargs.pop("lm_labels")
        if "past" in kwargs:
            warnings.warn(
                "The `past` argument is deprecated and will be removed in a future version, use `past_key_values` instead.",
                FutureWarning,
            )
            past_key_values = kwargs.pop("past")
        assert kwargs == {}, f"Unexpected keyword arguments: {list(kwargs.keys())}."
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        transformer_outputs = self.transformer(
            input_ids,
            past_key_values=past_key_values,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        hidden_states = transformer_outputs[0]

        lm_logits = self.lm_head(hidden_states)
        mc_logits = self.multiple_choice_head(hidden_states, mc_token_ids).squeeze(-1)

        mc_loss = None
        if mc_labels is not None:
            loss_fct = CrossEntropyLoss()
            mc_loss = loss_fct(mc_logits.view(-1, mc_logits.size(-1)), mc_labels.view(-1))
        lm_loss = None
        if labels is not None:
            shift_logits = lm_logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            loss_fct = CrossEntropyLoss()
            lm_loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))

        if not return_dict:
            output = (lm_logits, mc_logits) + transformer_outputs[1:]
            if mc_loss is not None:
                output = (mc_loss,) + output
            return ((lm_loss,) + output) if lm_loss is not None else output

        return GPT2DoubleHeadsModelOutput(
            loss=lm_loss,
            mc_loss=mc_loss,
            logits=lm_logits,
            mc_logits=mc_logits,
            past_key_values=transformer_outputs.past_key_values,
            hidden_states=transformer_outputs.hidden_states,
            attentions=transformer_outputs.attentions,
        )


class GPT2DoubleHeadsModelCustomLoss(GPT2PreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        config.num_labels = 1
        self.transformer = GPT2Model(config)
        self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)
        self.debias_head = nn.functional.linear
        self.multiple_choice_head = SequenceSummary(config)

        self.init_weights()

    def get_output_embeddings(self):
        return self.lm_head

    def prepare_inputs_for_generation(self, input_ids, past=None, **kwargs):
        # only last token for inputs_ids if past is defined in kwargs
        if past:
            input_ids = input_ids[:, -1].unsqueeze(-1)

        return {
            "input_ids": input_ids,
            "past_key_values": past,
            "use_cache": kwargs.get("use_cache"),
        }

    # @add_start_docstrings_to_callable(GPT2_INPUTS_DOCSTRING)
    # @replace_return_docstrings(output_type=GPT2DoubleHeadsModelOutput, config_class=_CONFIG_FOR_DOC)
    def forward(
        self,
        input_ids=None,
        past_key_values=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        mc_token_ids=None,
        labels=None,
        mc_labels=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        **kwargs,
    ):
        r"""
            mc_token_ids (:obj:`torch.LongTensor` of shape :obj:`(batch_size, num_choices)`, `optional`, default to index of the last token of the input)
                Index of the classification token in each input sequence.
                Selected in the range ``[0, input_ids.size(-1) - 1[``.
            labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`, defaults to :obj:`None`)
                Labels for language modeling.
                Note that the labels **are shifted** inside the model, i.e. you can set ``labels = input_ids``
                Indices are selected in ``[-1, 0, ..., config.vocab_size]``
                All labels set to ``-100`` are ignored (masked), the loss is only
                computed for labels in ``[0, ..., config.vocab_size]``
            mc_labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size)`, `optional`, defaults to :obj:`None`)
                Labels for computing the multiple choice classification loss.
                Indices should be in ``[0, ..., num_choices]`` where `num_choices` is the size of the second dimension
                of the input tensors. (see `input_ids` above)
            kwargs (:obj:`Dict[str, any]`, optional, defaults to `{}`):
                Used to hide legacy arguments that have been deprecated.

        """
        if "lm_labels" in kwargs:
            warnings.warn(
                "The `lm_labels` argument is deprecated and will be removed in a future version, use `labels` instead.",
                FutureWarning,
            )
            labels = kwargs.pop("lm_labels")
        if "past" in kwargs:
            warnings.warn(
                "The `past` argument is deprecated and will be removed in a future version, use `past_key_values` instead.",
                FutureWarning,
            )
            past_key_values = kwargs.pop("past")
        assert kwargs == {}, f"Unexpected keyword arguments: {list(kwargs.keys())}."
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        transformer_outputs = self.transformer(
            input_ids,
            past_key_values=past_key_values,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        # print('input_ids: {}'.format(input_ids))

        hidden_states = transformer_outputs[0]
        # print('hidden_states shape: {}'.format(hidden_states.shape))
        # print('hidden_states: {}'.format(hidden_states))


        lm_logits = self.lm_head(hidden_states)
        mc_logits = self.multiple_choice_head(hidden_states, mc_token_ids).squeeze(-1)

        # print('lm_logits {}'.format(lm_logits))

        all_input_embeds = self.transformer.get_input_embeddings()
        # all_input_embeds = self.get_output_embeddings()
        # [[Ġjew, ĠChristian], [ĠJews, ĠChristians], [ĠJudaism, ĠChristianity], [ĠJewish, ĠChristian],
        # [Jew, Christian]]
        target_ids_list = torch.LongTensor([[12711, 4302], [6771, 9316], [26976, 13624], [5582, 4302], [23119, 20298]])
        # [[ĠMuslims, ĠChristians], [Muslims, ĠChristians], [Muslim, Christian], [ĠMuslim, ĠChristian],
        # [ĠIslam, ĠChristianity], [Islam, ĠChristianity], [ĠIslamic, ĠChristian], [Islamic, Christian], [ĠArab, ĠAmerican], [Arab, American]]
        # target_ids_list = [[7045, 9316], [36452, 9316], [17067, 20298], [3765, 4302], [3449, 13624], [16991, 13624],
        #                    [5533, 4302], [26723, 20298], [4498, 1605], [31602, 7437]]
        debias_loss_total = 0
        print(self.device)
        all_input_embeds = all_input_embeds.to(self.device)
        target_ids_list = target_ids_list.to(self.device)
#         debias_loss_total = debias_loss_total.to(self.device)

       
        for i, input_id in enumerate(input_ids):
            for target_ids in target_ids_list:
                for t_id in target_ids:
                    if t_id in input_id:
                        # print(target_ids)
                        target_embeds = all_input_embeds(target_ids)
                        # print(target_embeds)
                        debias_logits = self.debias_head(hidden_states[i], weight=target_embeds)
                        # print('debias_logits {}'.format(debias_logits))

                        softmax_layer = nn.Softmax(dim=1)
                        debias_softmax = softmax_layer(debias_logits)
                        # print('debias_softmax {}'.format(debias_softmax))

                        debias_softmax = torch.squeeze(debias_softmax)

                        debias_softmax_1 = torch.flatten(debias_softmax[:, 0])
                        debias_softmax_2 = torch.flatten(debias_softmax[:, 1])

                        debias_loss = torch.abs(torch.log(debias_softmax_1 / debias_softmax_2))

                        # debias_loss[torch.isnan(debias_loss)] = 0
                        debias_loss = torch.sum(debias_loss)
                        # print('debias_loss {}'.format(debias_loss))

                        debias_loss_total = debias_loss_total + debias_loss

        debias_loss_total = debias_loss_total / len(target_ids_list)

        
        '''
        for target_ids in target_ids_list:

            target_embeds = all_input_embeds(target_ids)
            # print(target_embeds)
            debias_logits = self.debias_head(hidden_states, weight=target_embeds)
            # print('debias_logits {}'.format(debias_logits))

            softmax_layer = nn.Softmax(dim=2)
            debias_softmax = softmax_layer(debias_logits)
            # print('debias_softmax {}'.format(debias_softmax))

            debias_softmax = torch.squeeze(debias_softmax)
           
            if len(list(debias_softmax.shape)) == 3:
                debias_softmax_1 = torch.flatten(debias_softmax[:,:, 0])
                debias_softmax_2 = torch.flatten(debias_softmax[:,:, 1])
            elif len(list(debias_softmax.shape)) == 2:
                debias_softmax_1 = torch.flatten(debias_softmax[:, 0])
                debias_softmax_2 = torch.flatten(debias_softmax[:, 1])

            debias_loss = torch.abs(torch.log(debias_softmax_1/debias_softmax_2))

            # debias_loss[torch.isnan(debias_loss)] = 0
            debias_loss = torch.sum(debias_loss)

            debias_loss_total = debias_loss_total + debias_loss
            logger.info('debias_loss {}'.format(debias_loss))

        debias_loss_total = debias_loss_total / (len(target_ids_list) * hidden_states.shape[1])
        # print('debias_loss_total {}'.format(debias_loss_total))
        '''
        
        '''
        # attribute_list = [31828, 18536, 10461, 23542, 6181, 25513, 50186, 18536, 14918, 15773, 39835, 10489, 17118,
        #                   18204, 47620, 6272, 47859, 41520, 7812]

        attribute_list = [8361, 7971, 15773, 6272, 30651, 28655, 20363, 34781, 890, 6181, 17118, 18536, 47859, 29349,
                          39904, 50186, 25474, 31828, 34712, 47088, 25513, 45522, 8011, 23373, 2266, 22279]
        # aggressive - Ġaggressive 8361, pushy 'Ġpush', 'y' 4574 88, immigrant Ġimmigrants Ġimmigrant Ġimmigration 7971 15773 6272,
        # dominating Ġdominating 30651, overbearing 'Ġover', 'bearing' 625, 28655, selfish Ġselfish 20363, spoiled Ġspoiled 34781,
        # insatiable 'Ġins', 'at', 'iable' 1035, 265, 3379, long noses Ġlong', 'Ġnoses' 890, 41760, overprotective 'Ġover', 'prot', 'ective',
        # evil Ġevil 6181, devil Ġdevil 17118, satan 'Ġsat', 'an' 3332, 272, inferior Ġinferior 18536, manipulative Ġmanipulative 47859,
        # manipulating Ġmanipulating 29349, cannibal Ġcannibal 39904, cannibals, effeminate 'Ġeff', 'em', 'inate' 914, 368, 4559,
        # conspiring Ġconspiring 50186, greed Ġgreed 25474, greedy Ġgreedy 31828, nerd Ġnerd 34712, nerds Ġnerds 47088
        # circumcision Ġcircumcision 25513, circumcise Ġcircumcised 45522, materialistic, hook Ġhook 8011, hooked Ġhooked 23373,
        # beady eyes, large nose, red hair 'Ġred', 'Ġhair' 2266, 4190, frugal 'Ġf', 'rug', 'al', lust Ġlust 22279,
        # target_embeds = all_input_embeds(torch.LongTensor([3370, 4302]))
        cos = nn.CosineSimilarity(dim=0)

        for i_sent, input_id_1 in enumerate(input_ids):
            for i, input_id in enumerate(input_id_1):
                if input_id in attribute_list:
                    print(input_id)
                    # print((hidden_states[i_sent, i]).shape)
                    # print((target_embeds[0]).shape)
                    # print('cosine b/w jews and attributes')
                    # print(cos(hidden_states[i_sent, i], target_embeds[0]))
                    # print('cosine b/w christians and attributes')
                    # print(cos(hidden_states[i_sent, i], target_embeds[1]))
                    for target_ids in target_ids_list:
                        target_embeds = all_input_embeds(torch.LongTensor(target_ids))
                        debias_loss = torch.abs((1 - cos(hidden_states[i_sent, i], target_embeds[0])) - (1 - cos(hidden_states[i_sent, i], target_embeds[1])))
                        # print(debias_loss)
                        debias_loss_total = debias_loss_total + debias_loss

        # debias_loss_total = debias_loss_total / 4
        '''

        mc_loss = None
        if mc_labels is not None:
            loss_fct = CrossEntropyLoss()
            mc_loss = loss_fct(mc_logits.view(-1, mc_logits.size(-1)), mc_labels.view(-1))
        lm_loss = None
        if labels is not None:
            shift_logits = lm_logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            loss_fct = CrossEntropyLoss()
            lm_loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))

        # lm_loss = lm_loss + debias_loss_total
        print('total per sent debias_loss {}'.format(debias_loss_total))
        print('lm loss {}'.format(lm_loss))

        if not return_dict:
            output = (lm_logits, mc_logits) + transformer_outputs[1:]
            # if debias_loss_total is not None:
            output = (debias_loss_total,) + output
            return ((lm_loss,) + output) if lm_loss is not None else output

        # print('LM loss, Debias loss')
        # print(output[0], output[1])

        return GPT2DoubleHeadsModelOutput(
            loss=lm_loss,
            mc_loss=mc_loss,
            logits=lm_logits,
            mc_logits=mc_logits,
            past_key_values=transformer_outputs.past_key_values,
            hidden_states=transformer_outputs.hidden_states,
            attentions=transformer_outputs.attentions,
        )

    
class GPT2DoubleHeadsModelProjectionLoss(GPT2PreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        config.num_labels = 1
        self.transformer = GPT2Model(config)
        self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)
        self.projection = nn.functional.linear

        self.init_weights()

    def get_output_embeddings(self):
        return self.lm_head

    def prepare_inputs_for_generation(self, input_ids, past=None, **kwargs):
        # only last token for inputs_ids if past is defined in kwargs
        if past:
            input_ids = input_ids[:, -1].unsqueeze(-1)

        return {
            "input_ids": input_ids,
            "past_key_values": past,
            "use_cache": kwargs.get("use_cache"),
        }

    # @add_start_docstrings_to_callable(GPT2_INPUTS_DOCSTRING)
    # @replace_return_docstrings(output_type=GPT2DoubleHeadsModelOutput, config_class=_CONFIG_FOR_DOC)
    def forward(
            self,
            input_ids=None,
            past_key_values=None,
            attention_mask=None,
            token_type_ids=None,
            position_ids=None,
            head_mask=None,
            inputs_embeds=None,
            mc_token_ids=None,
            labels=None,
            mc_labels=None,
            use_cache=None,
            output_attentions=None,
            output_hidden_states=None,
            return_dict=None,
            **kwargs,
    ):
        r"""
            mc_token_ids (:obj:`torch.LongTensor` of shape :obj:`(batch_size, num_choices)`, `optional`, default to index of the last token of the input)
                Index of the classification token in each input sequence.
                Selected in the range ``[0, input_ids.size(-1) - 1[``.
            labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`, defaults to :obj:`None`)
                Labels for language modeling.
                Note that the labels **are shifted** inside the model, i.e. you can set ``labels = input_ids``
                Indices are selected in ``[-1, 0, ..., config.vocab_size]``
                All labels set to ``-100`` are ignored (masked), the loss is only
                computed for labels in ``[0, ..., config.vocab_size]``
            mc_labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size)`, `optional`, defaults to :obj:`None`)
                Labels for computing the multiple choice classification loss.
                Indices should be in ``[0, ..., num_choices]`` where `num_choices` is the size of the second dimension
                of the input tensors. (see `input_ids` above)
            kwargs (:obj:`Dict[str, any]`, optional, defaults to `{}`):
                Used to hide legacy arguments that have been deprecated.

        """
        if "lm_labels" in kwargs:
            warnings.warn(
                "The `lm_labels` argument is deprecated and will be removed in a future version, use `labels` instead.",
                FutureWarning,
            )
            labels = kwargs.pop("lm_labels")
        if "past" in kwargs:
            warnings.warn(
                "The `past` argument is deprecated and will be removed in a future version, use `past_key_values` instead.",
                FutureWarning,
            )
            past_key_values = kwargs.pop("past")
        assert kwargs == {}, f"Unexpected keyword arguments: {list(kwargs.keys())}."
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        transformer_outputs = self.transformer(
            input_ids,
            past_key_values=past_key_values,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        # print('input_ids: {}'.format(input_ids))

        hidden_states = transformer_outputs[0]
        # print('hidden_states shape: {}'.format(hidden_states.shape))
        # print('hidden_states: {}'.format(hidden_states))
        # print('hidden states last {}'.format(hidden_states[:,-1]))

        lm_logits = self.lm_head(hidden_states)
        # cls_logits = self.cls_head(hidden_states[:,-1])

        attribute_list = torch.LongTensor([8361, 7971, 15773, 6272, 30651, 28655, 20363, 34781, 890, 6181, 17118, 18536, 47859, 29349,
                          39904, 50186, 25474, 31828, 34712, 47088, 25513, 45522, 8011, 23373, 2266, 22279])
        target_ids_list = torch.LongTensor([[12711, 4302], [6771, 9316], [26976, 13624], [5582, 4302], [23119, 20298]])

        lm_head_weights = self.lm_head.weight.data
        # print('lm_head_weight_shape {}'.format(lm_head_weights.shape))
        # print('first word weight {}'.format(lm_head_weights[0]))

        lm_head_weights = lm_head_weights.to(self.device)
        target_ids_list = target_ids_list.to(self.device)
        attribute_list = attribute_list.to(self.device)
        
        diff_matrix = torch.zeros((5, 768))
        bias_loss = 0
        
        for i, target_pair in enumerate(target_ids_list):
            j_w = lm_head_weights[target_pair[0]]
            c_w = lm_head_weights[target_pair[1]]
            diff_w = (c_w - j_w)/2
            diff_matrix[i] = diff_w

        # print('diff_matrix {}, shape {}'.format(diff_matrix, diff_matrix.shape))
        u, s, v = torch.svd(diff_matrix)

        # print(u.shape)
        # print(s.shape)
        # print(v.shape)

        v_k = v[:, :3]
        print(v_k.shape)

        n = lm_head_weights[attribute_list]
        print('n shape {}'.format(n.shape))
        # print('neutral words weight {}'.format(n[0]))
#         nv = torch.matmul(n, v_k)
        n = n.contiguous()
        v_k = v_k.contiguous()
        nv = self.projection(n.view(-1, n.size(-1)), weight=v_k.view(-1, v_k.size(-2)))
        print('nv shape {}'.format(nv.shape))
#         print(nv)
        bias_loss = torch.square(torch.norm(nv))
#         print(bias_loss)

        lm_loss = None
        if labels is not None:
            shift_logits = lm_logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            loss_fct = CrossEntropyLoss()
            lm_loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))

        # lm_loss = lm_loss + debias_loss_total
#         print('proj bias_loss {}'.format(bias_loss))
#         print('lm loss {}'.format(lm_loss))

        if not return_dict:
            output = (lm_logits,) + transformer_outputs[1:]
            output = (bias_loss,) + output
            # print(((lm_loss,) + output))
            return ((lm_loss,) + output) if lm_loss is not None else output

        # print('LM loss, Debias loss')
        # print(output[0], output[1])
        mc_loss = None
        mc_logits = None
        return GPT2DoubleHeadsModelOutput(
            loss=lm_loss,
            mc_loss=mc_loss,
            logits=lm_logits,
            mc_logits=mc_logits,
            past_key_values=transformer_outputs.past_key_values,
            hidden_states=transformer_outputs.hidden_states,
            attentions=transformer_outputs.attentions,
        )

class SequenceClassifier(nn.Module):
    def __init__(
            self,
            hidden_size: int,
            num_classes: int,
    ):
        super(SequenceClassifier, self).__init__()
        self.fc1 = nn.Linear(hidden_size, num_classes)

    def forward(self, hidden_states):
        cls_logits = self.fc1(hidden_states)  # (batch_size , max_len, num_classes)
        return cls_logits


class SequenceClassificationHead(nn.Module):
    """Head for sentence-level classification tasks."""

    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.dropout = nn.Dropout(0.2)
        self.out_proj = nn.Linear(config.hidden_size, 1)

    def forward(self, features, **kwargs):
        x = features  # take <s> token (equiv. to [CLS])
        x = self.dropout(x)
        x = self.dense(x)
        x = torch.tanh(x)
        x = self.dropout(x)
        x = self.out_proj(x)
        return x


class GPT2DoubleHeadsModelCustomClassifier(GPT2PreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        config.num_labels = 1
        self.transformer = GPT2Model(config)
        self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)
        # self.cls_head = SequenceClassifier(hidden_size=768, num_classes=1)
        self.cls_head = SequenceClassificationHead(config)

        self.init_weights()

    def get_output_embeddings(self):
        return self.lm_head

    def prepare_inputs_for_generation(self, input_ids, past=None, **kwargs):
        # only last token for inputs_ids if past is defined in kwargs
        if past:
            input_ids = input_ids[:, -1].unsqueeze(-1)

        return {
            "input_ids": input_ids,
            "past_key_values": past,
            "use_cache": kwargs.get("use_cache"),
        }

    # @add_start_docstrings_to_callable(GPT2_INPUTS_DOCSTRING)
    # @replace_return_docstrings(output_type=GPT2DoubleHeadsModelOutput, config_class=_CONFIG_FOR_DOC)
    def forward(
            self,
            input_ids=None,
            past_key_values=None,
            attention_mask=None,
            token_type_ids=None,
            position_ids=None,
            head_mask=None,
            inputs_embeds=None,
            mc_token_ids=None,
            labels=None,
            mc_labels=None,
            use_cache=None,
            output_attentions=None,
            output_hidden_states=None,
            return_dict=None,
            **kwargs,
    ):
        r"""
            mc_token_ids (:obj:`torch.LongTensor` of shape :obj:`(batch_size, num_choices)`, `optional`, default to index of the last token of the input)
                Index of the classification token in each input sequence.
                Selected in the range ``[0, input_ids.size(-1) - 1[``.
            labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`, defaults to :obj:`None`)
                Labels for language modeling.
                Note that the labels **are shifted** inside the model, i.e. you can set ``labels = input_ids``
                Indices are selected in ``[-1, 0, ..., config.vocab_size]``
                All labels set to ``-100`` are ignored (masked), the loss is only
                computed for labels in ``[0, ..., config.vocab_size]``
            mc_labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size)`, `optional`, defaults to :obj:`None`)
                Labels for computing the multiple choice classification loss.
                Indices should be in ``[0, ..., num_choices]`` where `num_choices` is the size of the second dimension
                of the input tensors. (see `input_ids` above)
            kwargs (:obj:`Dict[str, any]`, optional, defaults to `{}`):
                Used to hide legacy arguments that have been deprecated.

        """
        if "lm_labels" in kwargs:
            warnings.warn(
                "The `lm_labels` argument is deprecated and will be removed in a future version, use `labels` instead.",
                FutureWarning,
            )
            labels = kwargs.pop("lm_labels")
        if "past" in kwargs:
            warnings.warn(
                "The `past` argument is deprecated and will be removed in a future version, use `past_key_values` instead.",
                FutureWarning,
            )
            past_key_values = kwargs.pop("past")
        assert kwargs == {}, f"Unexpected keyword arguments: {list(kwargs.keys())}."
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        transformer_outputs = self.transformer(
            input_ids,
            past_key_values=past_key_values,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        # print('input_ids: {}'.format(input_ids))

        hidden_states = transformer_outputs[0]
        # print('hidden_states shape: {}'.format(hidden_states.shape))
        # print('hidden_states: {}'.format(hidden_states))
        # print('hidden states last {}'.format(hidden_states[:,-1]))

        lm_logits = self.lm_head(hidden_states)
        # cls_logits = self.cls_head(hidden_states[:,-1])
        cls_logits = self.cls_head(hidden_states[:,-1,:])

        lm_head_weights = self.lm_head.weight.data
        print('lm_head_weight_shape {}'.format(lm_head_weights.shape))
        print('first word weight {}'.format(lm_head_weights[0]))
        # print('cls_logits {}'.format(cls_logits))
        # print('mc_labels {}'.format(mc_labels))

        sig = nn.Sigmoid()
        cls_prob = sig(cls_logits)
        # print('cls_prob {}'.format(cls_prob))
        bias_loss = torch.sum(cls_prob)
        # print('bias_loss {}'.format(bias_loss))

        cls_loss = None
        if mc_labels is not None:
            cls_labels = mc_labels
            # print(cls_logits.view(cls_logits.size(0)))
            # print(cls_labels.view(cls_labels.size(0)))
            cls_labels = cls_labels.view(cls_labels.size(0))
            cls_labels = cls_labels.type(torch.DoubleTensor)

            loss_fct = BCEWithLogitsLoss()
            cls_loss = loss_fct(cls_logits.view(cls_logits.size(0)), cls_labels)

        lm_loss = None
        if labels is not None:
            shift_logits = lm_logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            loss_fct = CrossEntropyLoss()
            lm_loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))

        # lm_loss = lm_loss + debias_loss_total
        print('total cls_loss {}'.format(cls_loss))
        print('lm loss {}'.format(lm_loss))

        if not return_dict:
            output = (lm_logits, cls_logits) + transformer_outputs[1:]
            if cls_loss is not None:
                output = (cls_loss, bias_loss) + output
            return ((lm_loss,) + output) if lm_loss is not None else output

        # print('LM loss, Debias loss')
        # print(output[0], output[1])
        mc_loss = None
        mc_logits = None
        return GPT2DoubleHeadsModelOutput(
            loss=lm_loss,
            mc_loss=mc_loss,
            logits=lm_logits,
            mc_logits=mc_logits,
            past_key_values=transformer_outputs.past_key_values,
            hidden_states=transformer_outputs.hidden_states,
            attentions=transformer_outputs.attentions,
        )


class GPT2DoubleHeadsModelSoftDebiasing(GPT2PreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        config.num_labels = 1
        self.transformer = GPT2Model(config)
        self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)
        self.projection = nn.functional.linear

        self.init_weights()

    def get_output_embeddings(self):
        return self.lm_head

    def prepare_inputs_for_generation(self, input_ids, past=None, **kwargs):
        # only last token for inputs_ids if past is defined in kwargs
        if past:
            input_ids = input_ids[:, -1].unsqueeze(-1)

        return {
            "input_ids": input_ids,
            "past_key_values": past,
            "use_cache": kwargs.get("use_cache"),
        }

    # @add_start_docstrings_to_callable(GPT2_INPUTS_DOCSTRING)
    # @replace_return_docstrings(output_type=GPT2DoubleHeadsModelOutput, config_class=_CONFIG_FOR_DOC)
    def forward(
            self,
            input_ids=None,
            past_key_values=None,
            attention_mask=None,
            token_type_ids=None,
            position_ids=None,
            head_mask=None,
            inputs_embeds=None,
            mc_token_ids=None,
            labels=None,
            mc_labels=None,
            use_cache=None,
            output_attentions=None,
            output_hidden_states=None,
            return_dict=None,
            **kwargs,
    ):
        r"""
            mc_token_ids (:obj:`torch.LongTensor` of shape :obj:`(batch_size, num_choices)`, `optional`, default to index of the last token of the input)
                Index of the classification token in each input sequence.
                Selected in the range ``[0, input_ids.size(-1) - 1[``.
            labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`, defaults to :obj:`None`)
                Labels for language modeling.
                Note that the labels **are shifted** inside the model, i.e. you can set ``labels = input_ids``
                Indices are selected in ``[-1, 0, ..., config.vocab_size]``
                All labels set to ``-100`` are ignored (masked), the loss is only
                computed for labels in ``[0, ..., config.vocab_size]``
            mc_labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size)`, `optional`, defaults to :obj:`None`)
                Labels for computing the multiple choice classification loss.
                Indices should be in ``[0, ..., num_choices]`` where `num_choices` is the size of the second dimension
                of the input tensors. (see `input_ids` above)
            kwargs (:obj:`Dict[str, any]`, optional, defaults to `{}`):
                Used to hide legacy arguments that have been deprecated.

        """
        if "lm_labels" in kwargs:
            warnings.warn(
                "The `lm_labels` argument is deprecated and will be removed in a future version, use `labels` instead.",
                FutureWarning,
            )
            labels = kwargs.pop("lm_labels")
        if "past" in kwargs:
            warnings.warn(
                "The `past` argument is deprecated and will be removed in a future version, use `past_key_values` instead.",
                FutureWarning,
            )
            past_key_values = kwargs.pop("past")
        assert kwargs == {}, f"Unexpected keyword arguments: {list(kwargs.keys())}."
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        transformer_outputs = self.transformer(
            input_ids,
            past_key_values=past_key_values,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        # print('input_ids: {}'.format(input_ids))

        hidden_states = transformer_outputs[0]
        # print('hidden_states shape: {}'.format(hidden_states.shape))
        # print('hidden_states: {}'.format(hidden_states))
        # print('hidden states last {}'.format(hidden_states[:,-1]))

        lm_logits = self.lm_head(hidden_states)
        # cls_logits = self.cls_head(hidden_states[:,-1])

        attribute_list = [8361, 7971, 15773, 6272, 30651, 28655, 20363, 34781, 890, 6181, 17118, 18536, 47859, 29349,
                          39904, 50186, 25474, 31828, 34712, 47088, 25513, 45522, 8011, 23373, 2266, 22279]
        target_ids_list = [[12711, 4302], [6771, 9316], [26976, 13624], [5582, 4302], [23119, 20298]]

        lm_head_weights = self.lm_head.weight.data
        # print('lm_head_weight_shape {}'.format(lm_head_weights.shape))
        # print('first word weight {}'.format(lm_head_weights[0]))

        diff_matrix = torch.zeros((5, 768))
        for i, target_pair in enumerate(target_ids_list):
            j_w = lm_head_weights[target_pair[0]]
            c_w = lm_head_weights[target_pair[1]]
            diff_w = (c_w - j_w)/2
            diff_matrix[i] = diff_w

        # print('diff_matrix {}, shape {}'.format(diff_matrix, diff_matrix.shape))
        u, s, v = torch.svd(diff_matrix)

        # print(u.shape)
        # print(s.shape)
        # print(v.shape)

        v_k = v[:, :3]
        v_k = v
        print(v_k.shape)

        n = lm_head_weights[attribute_list]
        # print('n shape {}'.format(n.shape))
        # print('neutral words weight {}'.format(n[0]))
        n = n.contiguous()
        v_k = v_k.contiguous()
        nv = self.projection(n.view(-1, n.size(-1)), weight=v_k.view(-1, v_k.size(-2)))
        # nv = torch.matmul(n, v_k)
        # print('nv shape {}'.format(nv.shape))
        bias_loss = torch.square(torch.norm(nv))

        lm_loss = None
        if labels is not None:
            shift_logits = lm_logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            loss_fct = CrossEntropyLoss()
            lm_loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))

        # lm_loss = lm_loss + debias_loss_total
        print('proj bias_loss {}'.format(bias_loss))
        print('lm loss {}'.format(lm_loss))

        if not return_dict:
            output = (lm_logits,) + transformer_outputs[1:]
            output = (bias_loss,) + output
            # print(((lm_loss,) + output))
            return ((lm_loss,) + output) if lm_loss is not None else output

        # print('LM loss, Debias loss')
        # print(output[0], output[1])
        mc_loss = None
        mc_logits = None
        return GPT2DoubleHeadsModelOutput(
            loss=lm_loss,
            mc_loss=mc_loss,
            logits=lm_logits,
            mc_logits=mc_logits,
            past_key_values=transformer_outputs.past_key_values,
            hidden_states=transformer_outputs.hidden_states,
            attentions=transformer_outputs.attentions,
        )


class GPT2DoubleHeadsModelHardDebiasing(GPT2PreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        config.num_labels = 1
        self.transformer = GPT2Model(config)
        self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)

        self.init_weights()

    def get_output_embeddings(self):
        return self.lm_head

    def prepare_inputs_for_generation(self, input_ids, past=None, **kwargs):
        # only last token for inputs_ids if past is defined in kwargs
        if past:
            input_ids = input_ids[:, -1].unsqueeze(-1)

        return {
            "input_ids": input_ids,
            "past_key_values": past,
            "use_cache": kwargs.get("use_cache"),
        }

    # @add_start_docstrings_to_callable(GPT2_INPUTS_DOCSTRING)
    # @replace_return_docstrings(output_type=GPT2DoubleHeadsModelOutput, config_class=_CONFIG_FOR_DOC)
    def forward(
            self,
            input_ids=None,
            past_key_values=None,
            attention_mask=None,
            token_type_ids=None,
            position_ids=None,
            head_mask=None,
            inputs_embeds=None,
            mc_token_ids=None,
            labels=None,
            mc_labels=None,
            use_cache=None,
            output_attentions=None,
            output_hidden_states=None,
            return_dict=None,
            **kwargs,
    ):
        r"""
            mc_token_ids (:obj:`torch.LongTensor` of shape :obj:`(batch_size, num_choices)`, `optional`, default to index of the last token of the input)
                Index of the classification token in each input sequence.
                Selected in the range ``[0, input_ids.size(-1) - 1[``.
            labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`, defaults to :obj:`None`)
                Labels for language modeling.
                Note that the labels **are shifted** inside the model, i.e. you can set ``labels = input_ids``
                Indices are selected in ``[-1, 0, ..., config.vocab_size]``
                All labels set to ``-100`` are ignored (masked), the loss is only
                computed for labels in ``[0, ..., config.vocab_size]``
            mc_labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size)`, `optional`, defaults to :obj:`None`)
                Labels for computing the multiple choice classification loss.
                Indices should be in ``[0, ..., num_choices]`` where `num_choices` is the size of the second dimension
                of the input tensors. (see `input_ids` above)
            kwargs (:obj:`Dict[str, any]`, optional, defaults to `{}`):
                Used to hide legacy arguments that have been deprecated.

        """
        if "lm_labels" in kwargs:
            warnings.warn(
                "The `lm_labels` argument is deprecated and will be removed in a future version, use `labels` instead.",
                FutureWarning,
            )
            labels = kwargs.pop("lm_labels")
        if "past" in kwargs:
            warnings.warn(
                "The `past` argument is deprecated and will be removed in a future version, use `past_key_values` instead.",
                FutureWarning,
            )
            past_key_values = kwargs.pop("past")
        assert kwargs == {}, f"Unexpected keyword arguments: {list(kwargs.keys())}."
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        transformer_outputs = self.transformer(
            input_ids,
            past_key_values=past_key_values,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        # print('input_ids: {}'.format(input_ids))

        hidden_states = transformer_outputs[0]
        print('hidden_states shape: {}'.format(hidden_states.shape))
        # print('hidden_states: {}'.format(hidden_states))
        # print('hidden states last {}'.format(hidden_states[:,-1]))

        attribute_list = [8361, 7971, 15773, 6272, 30651, 28655, 20363, 34781, 890, 6181, 17118, 18536, 47859, 29349,
                          39904, 50186, 25474, 31828, 34712, 47088, 25513, 45522, 8011, 23373, 2266, 22279]
        target_ids_list = [[12711, 4302], [6771, 9316], [26976, 13624], [5582, 4302], [23119, 20298]]

        lm_head_weights = self.lm_head.weight.data
        # print('lm_head_weight_shape {}'.format(lm_head_weights.shape))
        # print('first word weight {}'.format(lm_head_weights[0]))

        diff_matrix = torch.zeros((5, 768))
        for i, target_pair in enumerate(target_ids_list):
            j_w = lm_head_weights[target_pair[0]]
            c_w = lm_head_weights[target_pair[1]]
            diff_w = (c_w - j_w) / 2
            diff_matrix[i] = diff_w

        # print('diff_matrix {}, shape {}'.format(diff_matrix, diff_matrix.shape))
        u, s, v = torch.svd(diff_matrix)

        # print(u.shape)
        # print(s.shape)
        # print(v.shape)

        v_k = v[:, :3]
        # print(v_k.shape)

        # neutralise hidden states of all words
        '''
        word_projection_total = torch.zeros([768])
        for input in range(hidden_states.shape[0]):
            print('sentence length {}'.format(hidden_states[input].shape[0]))
            for word in range(hidden_states[input].shape[0]):
                word_embedding = hidden_states[input, word]
                # print('word_embedding {}'.format(word_embedding))
                for b in range(v_k.shape[1]):
                    word_projection = torch.dot(word_embedding, v_k[:, b]) * v_k[:, b]
                    # print('word_projection shape {}'.format(word_projection.shape))
                    word_projection_total += word_projection
                hidden_states[input, word] = hidden_states[input, word] - word_projection_total
        '''

        # neutralise hidden states of attribute words
        word_projection_total = torch.zeros([768])
        for i_sent, input_id_1 in enumerate(input_ids):
            for i, input_id in enumerate(input_id_1):
                if input_id in attribute_list:
                    print(input_id)
                    word_embedding = hidden_states[i_sent, i]
                    # print('word_embedding {}'.format(word_embedding))
                    for b in range(v_k.shape[1]):
                        word_projection = torch.dot(word_embedding, v_k[:, b]) * v_k[:, b]
                        # print('word_projection shape {}'.format(word_projection.shape))
                        word_projection_total += word_projection
                    hidden_states[i_sent, i] = hidden_states[i_sent, i] - word_projection_total

        lm_logits = self.lm_head(hidden_states)


        lm_loss = None
        if labels is not None:
            shift_logits = lm_logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            loss_fct = CrossEntropyLoss()
            lm_loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))

        # lm_loss = lm_loss + debias_loss_total
        print('lm loss {}'.format(lm_loss))

        if not return_dict:
            output = (lm_logits,) + transformer_outputs[1:]
            # output = (bias_loss,) + output
            # print(((lm_loss,) + output))
            return ((lm_loss,) + output) if lm_loss is not None else output

        # print('LM loss, Debias loss')
        # print(output[0], output[1])
        mc_loss = None
        mc_logits = None
        return GPT2DoubleHeadsModelOutput(
            loss=lm_loss,
            mc_loss=mc_loss,
            logits=lm_logits,
            mc_logits=mc_logits,
            past_key_values=transformer_outputs.past_key_values,
            hidden_states=transformer_outputs.hidden_states,
            attentions=transformer_outputs.attentions,
        )


class GPT2DoubleHeadsModelReligion2EqLoss(GPT2PreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        config.num_labels = 1
        self.transformer = GPT2Model(config)
        self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)
        self.debias_head = nn.functional.linear

        self.init_weights()

    def get_output_embeddings(self):
        return self.lm_head

    def prepare_inputs_for_generation(self, input_ids, past=None, **kwargs):
        # only last token for inputs_ids if past is defined in kwargs
        if past:
            input_ids = input_ids[:, -1].unsqueeze(-1)

        return {
            "input_ids": input_ids,
            "past_key_values": past,
            "use_cache": kwargs.get("use_cache"),
        }

    # @add_start_docstrings_to_callable(GPT2_INPUTS_DOCSTRING)
    # @replace_return_docstrings(output_type=GPT2DoubleHeadsModelOutput, config_class=_CONFIG_FOR_DOC)
    def forward(
        self,
        input_ids=None,
        past_key_values=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        mc_token_ids=None,
        labels=None,
        mc_labels=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        **kwargs,
    ):
        r"""
            mc_token_ids (:obj:`torch.LongTensor` of shape :obj:`(batch_size, num_choices)`, `optional`, default to index of the last token of the input)
                Index of the classification token in each input sequence.
                Selected in the range ``[0, input_ids.size(-1) - 1[``.
            labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`, defaults to :obj:`None`)
                Labels for language modeling.
                Note that the labels **are shifted** inside the model, i.e. you can set ``labels = input_ids``
                Indices are selected in ``[-1, 0, ..., config.vocab_size]``
                All labels set to ``-100`` are ignored (masked), the loss is only
                computed for labels in ``[0, ..., config.vocab_size]``
            mc_labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size)`, `optional`, defaults to :obj:`None`)
                Labels for computing the multiple choice classification loss.
                Indices should be in ``[0, ..., num_choices]`` where `num_choices` is the size of the second dimension
                of the input tensors. (see `input_ids` above)
            kwargs (:obj:`Dict[str, any]`, optional, defaults to `{}`):
                Used to hide legacy arguments that have been deprecated.

        """
        if "lm_labels" in kwargs:
            warnings.warn(
                "The `lm_labels` argument is deprecated and will be removed in a future version, use `labels` instead.",
                FutureWarning,
            )
            labels = kwargs.pop("lm_labels")
        if "past" in kwargs:
            warnings.warn(
                "The `past` argument is deprecated and will be removed in a future version, use `past_key_values` instead.",
                FutureWarning,
            )
            past_key_values = kwargs.pop("past")
        assert kwargs == {}, f"Unexpected keyword arguments: {list(kwargs.keys())}."
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        transformer_outputs = self.transformer(
            input_ids,
            past_key_values=past_key_values,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        # print('input_ids: {}'.format(input_ids))

        hidden_states = transformer_outputs[0]
        # print('hidden_states shape: {}'.format(hidden_states.shape))
        # print('hidden_states: {}'.format(hidden_states))


        lm_logits = self.lm_head(hidden_states)

        # print('lm_logits {}'.format(lm_logits))

        # all_input_embeds = self.transformer.get_input_embeddings() # uncomment incase of input embed
        all_output_embeds = self.lm_head.weight.data

        # all_input_embeds = self.get_output_embeddings()
        # [[Ġjew, ĠChristian], [ĠJews, ĠChristians], [ĠJudaism, ĠChristianity], [ĠJewish, ĠChristian],
        # [Jew, Christian]]
        # [[Ġmus, lim], [Ġchrist, ian]],

        # [[ĠMuslims, ĠChristians], [Muslims, ĠChristians], [Muslim, Christian], [ĠMuslim, ĠChristian],
        # [ĠIslam, ĠChristianity], [Islam, ĠChristianity], [ĠIslamic, ĠChristian], [Islamic, Christian], [ĠArab, ĠAmerican], [Arab, American]]
        # target_ids_list = [[7045, 9316], [36452, 9316], [17067, 20298], [3765, 4302], [3449, 13624], [16991, 13624],
        #                    [5533, 4302], [26723, 20298], [4498, 1605], [31602, 7437]]
        debias_loss_total = 0
        target_ids_list = [[[1928, 2475], [33826, 666]], [[271, 2543], [33826, 666, 414]], [[318,  2543], [33826, 666, 414]],
                           [[283, 8937], [45630, 504]], [[610,  8937], [45630, 504]]]

        '''
        for i, input_id in enumerate(input_ids):
            for target_id_pair in target_ids_list:
                for idx, t_id in enumerate(target_id_pair):
                    if idx == 0:
                        # print(input_id)
                        if t_id[0] in input_id and t_id[1] in input_id:
                            # print('t_id {}'.format(t_id))
                            # print(target_id_pair[0])
                            # print(target_id_pair[1])

                            # target_embeds_group1 = all_input_embeds(torch.LongTensor(target_id_pair[0])) # uncomment for input embed
                            # target_embeds_group2 = all_input_embeds(torch.LongTensor(target_id_pair[1]))
                            target_embeds_group1 = all_output_embeds[target_id_pair[0]]
                            target_embeds_group2 = all_output_embeds[target_id_pair[1]]
                            # avg_embeds_group1 = (target_embeds_group1[0] + target_embeds_group1[1])/2
                            # avg_embeds_group2 = (target_embeds_group2[0] + target_embeds_group2[1])/2
                            # print(avg_embeds_group1)
                            # print(avg_embeds_group2)
                            # print(target_embeds_group1[-1])
                            # print(target_embeds_group2[-1])

                            # avg_embeds_group1 = torch.mean(target_embeds_group1, 0)
                            # avg_embeds_group2 = torch.mean(target_embeds_group2, 0)
                            avg_embeds_group1 = target_embeds_group1[-1]
                            avg_embeds_group2 = target_embeds_group2[-1]

                            # print(avg_embeds_group1)
                            # print(avg_embeds_group2)

                            target_embeds = torch.stack([avg_embeds_group1, avg_embeds_group2])

                            # target_embeds = all_input_embeds[target_ids]
                            # print(target_embeds)
                            # print('hidden_states[i] {}'.format((hidden_states[i]).shape))
                            debias_logits = self.debias_head(hidden_states[i], weight=target_embeds)
                            # print('debias_logits {}'.format(debias_logits))

                            softmax_layer = nn.Softmax(dim=1)
                            debias_softmax = softmax_layer(debias_logits)
                            # print('debias_softmax {}'.format(debias_softmax))

                            debias_softmax = torch.squeeze(debias_softmax)

                            debias_softmax_1 = torch.flatten(debias_softmax[:, 0])
                            debias_softmax_2 = torch.flatten(debias_softmax[:, 1])
                            # print(debias_softmax_1)
                            # print(debias_softmax_2)

                            debias_loss = torch.abs(torch.log(debias_softmax_1 / debias_softmax_2))

                            # debias_loss[torch.isnan(debias_loss)] = 0
                            debias_loss = torch.sum(debias_loss)
                            # print('debias_loss {}'.format(debias_loss))

                            debias_loss_total = debias_loss_total + debias_loss

        debias_loss_total = debias_loss_total / (len(target_ids_list) * hidden_states.shape[1])
        '''

        # print('hidden_states shape {}'.format(hidden_states.shape[1]))
        for target_pair in target_ids_list:

            # target_embeds_group1 = all_input_embeds(torch.LongTensor(target_pair[0]))
            target_embeds_group1 = all_output_embeds[target_pair[0]]

            # target_embeds_group2 = all_input_embeds(torch.LongTensor(target_pair[1]))
            target_embeds_group2 = all_output_embeds[target_pair[1]]

            # avg_embeds_group1 = (target_embeds_group1[0] + target_embeds_group1[1]) / 2
            # avg_embeds_group2 = (target_embeds_group2[0] + target_embeds_group2[1]) / 2

            # avg_embeds_group1 = torch.mean(target_embeds_group1, 0)
            # avg_embeds_group2 = torch.mean(target_embeds_group2, 0)

            avg_embeds_group1 = target_embeds_group1[-1]
            avg_embeds_group2 = target_embeds_group2[-1]

            target_embeds = torch.stack([avg_embeds_group1, avg_embeds_group2])
            # print('target_embeds shape {}'.format(target_embeds.shape))


            
            # prob_group1_1 = self.debias_head(hidden_states.view(-1, hidden_states.size(-2), hidden_states.size(-1)),
            #                                  weight=target_embeds_group1[0].view(-1, target_embeds_group1[0].size(-1)))
            # prob_group1_2 = self.debias_head(hidden_states.view(-1, hidden_states.size(-2), hidden_states.size(-1)),
            #                                  weight=target_embeds_group1[1].view(-1, target_embeds_group1[1].size(-1)))
            # prob_group1 = prob_group1_1 * prob_group1_2
            # 
            # print('prob_group1 {}'.format(prob_group1))
            # 
            # prob_group2_1 = self.debias_head(hidden_states.view(-1, hidden_states.size(-2), hidden_states.size(-1)),
            #                                  weight=target_embeds_group2[0].view(-1, target_embeds_group2[0].size(-1)))
            # prob_group2_2 = self.debias_head(hidden_states.view(-1, hidden_states.size(-2), hidden_states.size(-1)),
            #                                  weight=target_embeds_group2[1].view(-1, target_embeds_group2[1].size(-1)))
            # prob_group2 = prob_group2_1 * prob_group2_2
            # 
            # print('prob_group2 {}'.format(prob_group2))
            # 
            # print('hidden_states shape {}'.format(hidden_states.shape))
            # 
            # # prob_group1 = prob_group1.squeeze()
            # # prob_group2 = prob_group2.squeeze()
            # # print('prob1 squeeze {}'.format(prob_group1))
            # # print('prob2 squeeze {}'.format(prob_group2))
            # 
            # debias_logits = torch.cat((prob_group1, prob_group2), 2)
            # print('logits {}'.format(logits))
            
            debias_logits = self.debias_head(hidden_states, weight=target_embeds)

            softmax_layer = nn.Softmax(dim=2)
            debias_softmax = softmax_layer(debias_logits)
            # print('debias_softmax {}'.format(debias_softmax))

            # debias_softmax = torch.squeeze(debias_softmax)
            # print('after squeeze debias_softmax {}'.format(debias_softmax))

            if len(list(debias_softmax.shape)) == 3:
                debias_softmax_1 = torch.flatten(debias_softmax[:,:, 0])
                debias_softmax_2 = torch.flatten(debias_softmax[:,:, 1])
            elif len(list(debias_softmax.shape)) == 2:
                debias_softmax_1 = torch.flatten(debias_softmax[:, 0])
                debias_softmax_2 = torch.flatten(debias_softmax[:, 1])
            # print('debias_softmax_1 {}'.format(debias_softmax_1))
            # print('debias_softmax_2 {}'.format(debias_softmax_2))

            log_eq = torch.log(debias_softmax_1/debias_softmax_2)
            # print('log_eq {}'.format(log_eq))

            debias_loss = torch.abs(torch.log(debias_softmax_1/debias_softmax_2))
            # print('abs db loss {}'.format(debias_loss))

            # debias_loss[torch.isnan(debias_loss)] = 0
            debias_loss = torch.sum(debias_loss)

            debias_loss_total = debias_loss_total + debias_loss
            logger.info('debias_loss {}'.format(debias_loss))

        debias_loss_total = debias_loss_total / (len(target_ids_list) * hidden_states.shape[1])


        # print('debias_loss_total {}'.format(debias_loss_total))

        # attribute_list_relg2 = [7417, 42002, 10509, 33184, 2372, 19971, 4923, 10309, 1368, 3434, 26127, 38313, 34082, 17162,
        #                   12954, 4301, 4472, 5775, 1175, 20440, 20674, 7702, 42325, 5465, 5527, 16931, 43115, 38466,
        #                   38007, 13384, 14273, 48492, 25765, 41932, 42301, 36108, 35052, 33908, 3685, 6590, 12524, 26592]
        # left out attributes muslim - hijackers, lazy sheik, oil sheik, viel, vielded, deport, detain, thieves, charlatan,
        # power-hungry, beard*, "wealthy oilmen", "harem maiden*", "suicide bomb*", headscarves


        lm_loss = None
        if labels is not None:
            shift_logits = lm_logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            loss_fct = CrossEntropyLoss()
            lm_loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))

        # lm_loss = lm_loss + debias_loss_total
        print('eq debias_loss {}'.format(debias_loss_total))
        print('lm loss {}'.format(lm_loss))

        if not return_dict:
            output = (lm_logits,) + transformer_outputs[1:]
            # if debias_loss_total is not None:
            output = (debias_loss_total,) + output
            return ((lm_loss,) + output) if lm_loss is not None else output

        # print('LM loss, Debias loss')
        # print(output[0], output[1])

        return GPT2DoubleHeadsModelOutput(
            loss=lm_loss,
            mc_loss=mc_loss,
            logits=lm_logits,
            mc_logits=mc_logits,
            past_key_values=transformer_outputs.past_key_values,
            hidden_states=transformer_outputs.hidden_states,
            attentions=transformer_outputs.attentions,
        )
