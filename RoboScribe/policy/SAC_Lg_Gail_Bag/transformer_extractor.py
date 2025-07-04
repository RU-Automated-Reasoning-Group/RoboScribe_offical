import gymnasium as gym
import numpy as np
import torch
import torch as th
import torch.nn as nn
from stable_baselines3.common.torch_layers import BaseFeaturesExtractor
from torch.distributions import Categorical
from torch.nn import functional as F

import pdb

# ==================================
#        Policy Architecture
# ==================================
def activation_factory(activation_type):
    if activation_type == "RELU":
        return F.relu
    elif activation_type == "TANH":
        return torch.tanh
    elif activation_type == "ELU":
        return nn.ELU()
    else:
        raise ValueError("Unknown activation_type: {}".format(activation_type))


class BaseModule(torch.nn.Module):
    """
    Base torch.nn.Module implementing basic features:
        - initialization factory
        - normalization parameters
    """

    def __init__(self, activation_type="RELU", reset_type="XAVIER"):
        super().__init__()
        self.activation = activation_factory(activation_type)
        self.reset_type = reset_type

    def _init_weights(self, m):
        if hasattr(m, "weight"):
            if self.reset_type == "XAVIER":
                torch.nn.init.xavier_uniform_(m.weight.data)
            elif self.reset_type == "ZEROS":
                torch.nn.init.constant_(m.weight.data, 0.0)
            else:
                raise ValueError("Unknown reset type")
        if hasattr(m, "bias") and m.bias is not None:
            torch.nn.init.constant_(m.bias.data, 0.0)

    def reset(self):
        self.apply(self._init_weights)


class MultiLayerPerceptron(BaseModule):
    def __init__(
        self,
        in_size=None,
        layer_sizes=None,
        reshape=True,
        out_size=None,
        activation="RELU",
        is_policy=False,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.reshape = reshape
        self.layer_sizes = layer_sizes or [64, 64]
        self.out_size = out_size
        self.activation = activation_factory(activation)
        self.is_policy = is_policy
        self.softmax = nn.Softmax(dim=-1)
        sizes = [in_size] + self.layer_sizes
        layers_list = [nn.Linear(sizes[i], sizes[i + 1]) for i in range(len(sizes) - 1)]
        self.layers = nn.ModuleList(layers_list)
        if out_size:
            self.predict = nn.Linear(sizes[-1], out_size)

    def forward(self, x):
        if self.reshape:
            x = x.reshape(x.shape[0], -1)  # We expect a batch of vectors
        for layer in self.layers:
            x = self.activation(layer(x.float()))
        if self.out_size:
            x = self.predict(x)
        if self.is_policy:
            action_probs = self.softmax(x)
            dist = Categorical(action_probs)
            return dist
        return x

    def action_scores(self, x):
        if self.is_policy:
            if self.reshape:
                x = x.reshape(x.shape[0], -1)  # We expect a batch of vectors
            for layer in self.layers:
                x = self.activation(layer(x.float()))
            if self.out_size:
                action_scores = self.predict(x)
            return action_scores


class BlockAttention(BaseModule):
    def __init__(self, feature_size=64, heads=4, dropout_factor=0):
        super().__init__()
        self.feature_size = feature_size
        self.heads = heads
        self.dropout_factor = dropout_factor
        self.features_per_head = int(self.feature_size / self.heads)

        self.value_all = nn.Linear(self.feature_size, self.feature_size, bias=False)
        self.key_all = nn.Linear(self.feature_size, self.feature_size, bias=False)
        self.query_ego = nn.Linear(self.feature_size, self.feature_size, bias=False)
        self.attention_combine = nn.Linear(
            self.feature_size, self.feature_size, bias=False
        )

    @classmethod
    def default_config(cls):
        return {}

    def forward(self, ego, others, mask=None):
        batch_size = others.shape[0]
        n_entities = others.shape[1] + 1
        input_all = torch.cat(
            (ego.view(batch_size, 1, self.feature_size), others), dim=1
        )
        # Dimensions: Batch, entity, head, feature_per_head
        key_all = self.key_all(input_all).view(
            batch_size, n_entities, self.heads, self.features_per_head
        )
        value_all = self.value_all(input_all).view(
            batch_size, n_entities, self.heads, self.features_per_head
        )
        query_ego = self.query_ego(ego).view(
            batch_size, 1, self.heads, self.features_per_head
        )

        # Dimensions: Batch, head, entity, feature_per_head
        key_all = key_all.permute(0, 2, 1, 3)
        value_all = value_all.permute(0, 2, 1, 3)
        query_ego = query_ego.permute(0, 2, 1, 3)
        if mask is not None:
            mask = mask.view((batch_size, 1, 1, n_entities)).repeat(
                (1, self.heads, 1, 1)
            )
        value, attention_matrix = attention(
            query_ego, key_all, value_all, mask, nn.Dropout(self.dropout_factor)
        )
        result = (
            self.attention_combine(value.reshape((batch_size, self.feature_size)))
            + ego.squeeze(1)
        ) / 2
        return result, attention_matrix


class BlockAttentionNetwork(BaseModule):
    def __init__(
        self,
        in_size=None,
        out_size=None,
        presence_feature_idx=0,
        gripper_embedding_layer_kwargs=None,
        embedding_layer_kwargs=None,
        attention_layer_kwargs=None,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.out_size = out_size
        self.presence_feature_idx = presence_feature_idx
        gripper_embedding_layer_kwargs = gripper_embedding_layer_kwargs or {}
        embedding_layer_kwargs = embedding_layer_kwargs or {}
        if not embedding_layer_kwargs.get("in_size", None):
            embedding_layer_kwargs["in_size"] = in_size
        self.gripper_embedding = MultiLayerPerceptron(**gripper_embedding_layer_kwargs)
        self.embedding = MultiLayerPerceptron(**embedding_layer_kwargs)

        attention_layer_kwargs = attention_layer_kwargs or {}
        self.attention_layer = BlockAttention(**attention_layer_kwargs)

    def forward(self, x):
        ego_embedded_att, _ = self.forward_attention(x)
        return ego_embedded_att

    # hard code for 3 block stack
    def split_input(self, x, mask=None):
        assert len(x.shape) == 2
        assert mask is None
        # init
        split_idx = x.shape[1] // 2
        block_num = (split_idx - 5) // 3
        # split
        gripper = torch.cat([x[:, :5], x[:, split_idx:split_idx+5]], 1)
        blocks = [torch.cat([x[:, 5+block_id*3: 5+block_id*3+3], x[:, split_idx+5+block_id*3: split_idx+5+block_id*3+3]], 1).unsqueeze(1) \
                  for block_id in range(block_num)]
        blocks = torch.cat(blocks, 1)
        
        return gripper, blocks, mask

    def forward_attention(self, x):
        gripper, others, mask = self.split_input(x)
        gripper = self.gripper_embedding(gripper)
        others = self.embedding(others)
        return self.attention_layer(gripper, others, mask)

    def get_attention_matrix(self, x):
        _, attention_matrix = self.forward_attention(x)
        return attention_matrix


def attention(query, key, value, mask=None, dropout=None):
    """
    Compute a Scaled Dot Product Attention.

    Parameters
    ----------
    query
        size: batch, head, 1 (ego-entity), features
    key
        size: batch, head, entities, features
    value
        size: batch, head, entities, features
    mask
        size: batch,  head, 1 (absence feature), 1 (ego-entity)
    dropout

    Returns
    -------
    The attention softmax(QK^T/sqrt(dk))V
    """
    d_k = query.size(-1)
    scores = torch.matmul(query, key.transpose(-2, -1)) / np.sqrt(d_k)
    if mask is not None:
        scores = scores.masked_fill(mask, -1e9)
    p_attn = F.softmax(scores, dim=-1)
    if dropout is not None:
        p_attn = dropout(p_attn)
    output = torch.matmul(p_attn, value)
    return output, p_attn


attention_network_kwargs = dict(
    in_size=5 * 15,
    gripper_embedding_layer_kwargs={"in_size": 10, "layer_sizes": [256, 256], "reshape": False},
    embedding_layer_kwargs={"in_size": 6, "layer_sizes": [256, 256], "reshape": False},
    attention_layer_kwargs={"feature_size": 256, "heads": 2},
)


class CustomExtractor(BaseFeaturesExtractor):
    """
    :param observation_space: (gym.Space)
    :param features_dim: (int) Number of features extracted.
        This corresponds to the number of unit for the last layer.
    """

    def __init__(self, observation_space: gym.spaces.Box, **kwargs):
        super().__init__(
            observation_space,
            features_dim=kwargs["attention_layer_kwargs"]["feature_size"],
        )
        self.extractor = BlockAttentionNetwork(**kwargs)

    def forward(self, observations: th.Tensor) -> th.Tensor:
        return self.extractor(observations)
