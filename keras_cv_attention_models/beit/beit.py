import numpy as np
from keras_cv_attention_models import backend
from keras_cv_attention_models.backend import layers, models, functional, image_data_format, initializers
from keras_cv_attention_models.models import register_model
from keras_cv_attention_models.attention_layers import (
    activation_by_name,
    BiasLayer,
    ChannelAffine,
    ClassToken,
    conv2d_no_bias,
    drop_block,
    drop_connect_rates_split,
    PositionalEmbedding,
    add_pre_post_process,
)
from keras_cv_attention_models.download_and_load import reload_model_weights

LAYER_NORM_EPSILON = 1e-6


PRETRAINED_DICT = {
    "beit_base_patch16": {"imagenet21k-ft1k": {224: "d7102337a13a3983f3b6470de77b5d5c", 384: "76353026477c60f8fdcbcc749fea17b3"}},
    "beit_v2_base_patch16": {"imagenet21k-ft1k": {224: "d001dcb67cdda16bfdbb2873ab9b13c8"}},
    "beit_large_patch16": {
        "imagenet21k-ft1k": {224: "fce2d162e7fa4dba9a1b1fc5e1dec5ce", 384: "158934d07dd8b1e1c6b96883aa00a748", 512: "64d18088e91df243960e5830aab80a6e"}
    },
    "beit_v2_large_patch16": {"imagenet21k-ft1k": {224: "b3cee12a545bfb676f9f426ee7158d27"}},
    "dinov2_vit_base14": {"imagenet": {518: "8943697691a511fa472e853c39666d97"}},
    "dinov2_vit_large14": {"imagenet": {518: "53deb035702a60948688adc858b01af6"}},
    "dinov2_vit_small14": {"imagenet": {518: "9f823ded0e64beccbd4e19a9fd2b501f"}},
    "dinov2_vit_giant14": {"imagenet": {518: "12a7cb1f167acb1036d9bfb4e25e796d"}},
    "eva_giant_patch14": {
        "imagenet21k-ft1k": {224: "5a475db6696d6e36ea896ec5dbd1c20d", 336: "fd8eeec10d6b6cb607ce033ea85b8e80", 560: "0ef0d2961523fb2047fbdb59cc347c17"}
    },
    "eva_large_patch14": {"imagenet21k-ft1k": {196: "bbeea886fbde4bd1c8c9876345273a99", 336: "4928faafd0177fe8f0d02dab4abc8e83"}},
    "eva02_base_patch14": {"mim_in22k_ft22k_ft1k": {448: "fd8cb2e335201fd1925370c78bfb68b0"}},
    "eva02_large_patch14": {"mim_m38m_ft22k_ft1k": {448: "8a5ba7c06e3f0a2e4c985982b2d93671"}},
    "eva02_small_patch14": {"mim_in22k_ft1k": {336: "87d87900e8096c3734278aab44c4b5f4"}},
    "eva02_tiny_patch14": {"mim_in22k_ft1k": {336: "8243463316967ca8f6ee0d0abcc4d236"}},
    "flexivit_small": {"imagenet": {240: "efb73a97d099a491b69ebfaf8a337df8"}},
    "flexivit_base": {"imagenet": {240: "dac627debb194928db01e1b9b7a548fd"}},
    "flexivit_large": {"imagenet": {240: "6faa953227d2ef1df6758f8eb7234490"}},
}


@backend.register_keras_serializable(package="beit")
class PositionalEncodingFourierRot(layers.Layer):
    def __init__(self, with_cls_token=True, attn_height=-1, num_heads=-1, temperature=1e4, ref_feature_shape=16, **kwargs):
        super().__init__(**kwargs)
        self.with_cls_token, self.attn_height, self.num_heads = with_cls_token, attn_height, num_heads
        self.temperature, self.ref_feature_shape = float(temperature), ref_feature_shape
        self.cls_token_len = 1 if with_cls_token else 0

    def build(self, input_shape):
        # input (with_cls_token=True): `[batch, ..., attn_height * attn_width + class_token, channels]`.
        # input (with_cls_token=False): `[batch, ..., attn_height * attn_width, channels]`.
        # print(input_shape)
        if self.attn_height == -1:
            height = width = int(float(input_shape[-2] - self.cls_token_len) ** 0.5)  # hh == ww, e.g. 14
        else:
            height = self.attn_height
            width = int(float(input_shape[-2] - self.cls_token_len) / height)
        self.channels = input_shape[-1]
        self.blocks_shape = [*input_shape[1:-2], input_shape[-2] - self.cls_token_len]

        hh, ww = np.arange(height, dtype="float32"), np.arange(width, dtype="float32")
        if self.ref_feature_shape is not None and self.ref_feature_shape > 0:
            # eva's scheme for resizing rope embeddings (ref shape = pretrain)
            hh = hh / height * self.ref_feature_shape
            ww = ww / height * self.ref_feature_shape

        pos_fileters = (self.channels // self.num_heads // 4) if self.num_heads > 0 else (self.channels // 4)
        grid = np.stack(np.meshgrid(hh, ww, indexing="ij"), axis=-1)
        dim_t = self.temperature ** (np.arange(pos_fileters, dtype="float32") / pos_fileters)  # (filters,)
        grid = np.expand_dims(grid, -1) / dim_t
        grid = np.reshape(grid, [height, width, -1])
        pos_sin, pos_cos = np.sin(grid), np.cos(grid)
        pos_sin, pos_cos = np.repeat(pos_sin, 2, axis=-1), np.repeat(pos_cos, 2, axis=-1)
        # print(f"{pos_sin.shape = }, {pos_cos.shape = }, {height = }, {width = }, {self.channels = }")
        if self.num_heads > 0:
            pos_sin = np.repeat(np.expand_dims(pos_sin, axis=-2), self.num_heads, axis=-2).reshape([height * width, self.num_heads * pos_fileters * 4])
            pos_cos = np.repeat(np.expand_dims(pos_cos, axis=-2), self.num_heads, axis=-2).reshape([height * width, self.num_heads * pos_fileters * 4])
        else:
            pos_sin, pos_cos = np.reshape(pos_sin, [height * width, pos_fileters * 4]), np.reshape(pos_cos, [height * width, pos_fileters * 4])

        if hasattr(self, "register_buffer"):  # PyTorch
            self.register_buffer("pos_sin", functional.convert_to_tensor(pos_sin, dtype=self.compute_dtype), persistent=False)
            self.register_buffer("pos_cos", functional.convert_to_tensor(pos_cos, dtype=self.compute_dtype), persistent=False)
        else:
            self.pos_sin = functional.convert_to_tensor(pos_sin, dtype=self.compute_dtype)
            self.pos_cos = functional.convert_to_tensor(pos_cos, dtype=self.compute_dtype)
        super().build(input_shape)

    def call(self, inputs, **kwargs):
        if self.with_cls_token:
            cls_token, inputs = functional.split(inputs, [1, -1], axis=-2)  # `[batch, num_heads, class_token + attn_height * attn_width, channels]`
        # def rot(x): return torch.stack([-x[..., 1::2], x[..., ::2]], -1).reshape(x.shape)
        left, right = functional.split(functional.reshape(inputs, [-1, *self.blocks_shape, self.channels // 2, 2]), 2, axis=-1)
        rot = functional.reshape(functional.concat([-right, left], axis=-1), (-1, *self.blocks_shape, self.channels))
        # def apply_rot_embed_cat(x: torch.Tensor, emb): return x * cos_emb + rot(x) * sin_emb
        out = inputs * self.pos_cos + rot * self.pos_sin
        if self.with_cls_token:
            out = functional.concat([cls_token, out], axis=-2)
        return out

    def get_config(self):
        base_config = super().get_config()
        base_config.update({"with_cls_token": self.with_cls_token, "attn_height": self.attn_height, "num_heads": self.num_heads})
        base_config.update({"temperature": self.temperature, "ref_feature_shape": self.ref_feature_shape})
        return base_config


@backend.register_keras_serializable(package="beit")
class MultiHeadRelativePositionalEmbedding(layers.Layer):
    def __init__(self, with_cls_token=True, attn_height=-1, num_heads=-1, **kwargs):
        super().__init__(**kwargs)
        self.with_cls_token, self.attn_height, self.num_heads = with_cls_token, attn_height, num_heads
        if with_cls_token:
            self.cls_token_len = 1
            self.cls_token_pos_len = 3
        else:
            self.cls_token_len = 0
            self.cls_token_pos_len = 0

    def build(self, attn_shape):
        # input (with_cls_token=True): `[batch, num_heads, attn_blocks, attn_blocks]`. where `attn_blocks = attn_height * attn_width + class_token`
        # input (with_cls_token=False): `[batch, num_heads, attn_blocks, attn_blocks]`. where `attn_blocks = attn_height * attn_width`
        # print(attn_shape)
        if self.attn_height == -1:
            height = width = int(float(attn_shape[2] - self.cls_token_len) ** 0.5)  # hh == ww, e.g. 14
        else:
            height = self.attn_height
            width = int(float(attn_shape[2] - self.cls_token_len) / height)
        num_heads = attn_shape[1] if self.num_heads == -1 else self.num_heads
        num_relative_distance = (2 * height - 1) * (2 * width - 1) + self.cls_token_pos_len
        # pos_shape = (num_relative_distance, num_heads)
        pos_shape = (num_heads, num_relative_distance)
        # initializer = tf.random_normal_initializer(stddev=0.02)
        self.relative_position_bias_table = self.add_weight(name="positional_embedding", shape=pos_shape, initializer="zeros", trainable=True)

        hh, ww = np.meshgrid(range(height), range(width))  # tf.meshgrid is same with np.meshgrid 'xy' mode, while torch.meshgrid 'ij' mode
        coords = np.stack([hh, ww], axis=-1)  # [14, 14, 2]
        coords_flatten = np.reshape(coords, [-1, 2])  # [196, 2]
        relative_coords = coords_flatten[:, None, :] - coords_flatten[None, :, :]  # [196, 196, 2]
        relative_coords_hh = relative_coords[:, :, 0] + height - 1
        relative_coords_ww = (relative_coords[:, :, 1] + width - 1) * (2 * height - 1)
        relative_coords = np.stack([relative_coords_hh, relative_coords_ww], axis=-1)

        relative_position_index = np.sum(relative_coords, axis=-1).astype("float32")  # [196, 196]
        if attn_shape[3] != attn_shape[2]:
            # Choose the small values if value_block != query_block
            relative_position_index = relative_position_index[:, -(attn_shape[3] - self.cls_token_len) :]

        if self.with_cls_token:
            top = np.ones((1, relative_position_index.shape[1]), dtype=relative_position_index.dtype) * (num_relative_distance - 3)
            left = np.ones((relative_position_index.shape[0], 1), dtype=relative_position_index.dtype) * (num_relative_distance - 2)
            corner = np.ones((1, 1), dtype=relative_position_index.dtype) * (num_relative_distance - 1)
            # print(f">>>> {top.shape = }, {left.shape = }, {corner.shape = }")
            # >>>> top.shape = TensorShape([1, 196]), left.shape = TensorShape([196, 1]), corner.shape = TensorShape([1, 1])
            left_corner = np.concatenate([corner, left], axis=0)
            relative_position_index = np.concatenate([top, relative_position_index], axis=0)
            relative_position_index = np.concatenate([left_corner, relative_position_index], axis=1)  # [197, 197]
        if hasattr(self, "register_buffer"):  # PyTorch
            self.register_buffer("relative_position_index", functional.convert_to_tensor(relative_position_index, dtype="int64"), persistent=False)
        else:
            self.relative_position_index = functional.convert_to_tensor(relative_position_index, dtype="int64")
        super().build(attn_shape)

    def call(self, attention_scores, **kwargs):
        pos_emb = functional.gather(self.relative_position_bias_table, self.relative_position_index, axis=1)
        # tf.print(pos_emb.shape, attention_scores.shape)
        return attention_scores + pos_emb

    def get_config(self):
        base_config = super().get_config()
        base_config.update({"with_cls_token": self.with_cls_token, "attn_height": self.attn_height, "num_heads": self.num_heads})
        return base_config

    def load_resized_weights(self, source_layer, method="bilinear"):
        if isinstance(source_layer, dict):
            source_tt = list(source_layer.values())[0]  # weights
            # source_tt = source_layer["pos_emb:0"]  # weights
        else:
            source_tt = source_layer.get_weights()[0]  # layer
        source_tt = np.array(source_tt.detach() if hasattr(source_tt, "detach") else source_tt).astype("float32")
        # self.relative_position_bias_table.assign(tf.transpose(source_tt))
        hh = ww = int(float(source_tt.shape[1] - self.cls_token_pos_len) ** 0.5)  # assume source weights are all square shape
        num_heads = source_tt.shape[0]
        ss = source_tt[:, : hh * ww].reshape((num_heads, hh, ww))  # [num_heads, hh, ww]

        if self.attn_height == -1:
            target_hh = target_ww = int(float(self.relative_position_bias_table.shape[1] - self.cls_token_pos_len) ** 0.5)
        else:
            target_hh = 2 * self.attn_height - 1
            target_ww = int(float(self.relative_position_bias_table.shape[1] - self.cls_token_pos_len) / target_hh)

        tt = backend.numpy_image_resize(ss, target_shape=[target_hh, target_ww], method=method, is_source_channels_last=False)
        tt = tt.reshape((num_heads, tt.shape[1] * tt.shape[2]))  # [num_heads, target_hh * target_ww]
        if self.with_cls_token:
            tt = np.concatenate([tt, source_tt[:, -self.cls_token_pos_len :]], axis=1)
        self.set_weights([tt])

    def show_pos_emb(self, rows=1, base_size=2):
        import math
        import matplotlib.pyplot as plt

        num_heads = self.relative_position_bias_table.shape[0]
        # pos_emb = tf.gather(self.relative_position_bias_table, self.relative_position_index, axis=1).numpy()
        hh = ww = int(float(self.relative_position_bias_table.shape[1] - self.cls_token_pos_len) ** 0.5)
        pos_emb = self.relative_position_bias_table[:, : hh * ww]
        pos_emb = pos_emb.detach().numpy() if hasattr(pos_emb, "detach") else pos_emb.numpy()
        pos_emb = pos_emb.reshape((num_heads, hh, ww))
        cols = int(math.ceil(num_heads / rows))
        fig, axes = plt.subplots(rows, cols, figsize=(base_size * cols, base_size * rows))
        for id, ax in enumerate(axes.flatten()):
            if id >= num_heads:
                break
            ax.imshow(pos_emb[id])
            ax.set_axis_off()
        fig.tight_layout()
        return fig


def scaled_dot_product_attention(query, key, value, output_shape, pos_emb=None, out_weight=True, out_bias=False, qk_scale=-1, dropout=0, name=None):
    output_dim = output_shape[-1]
    blocks = output_shape[1:-1] if output_shape[0] is None or output_shape[0] < 1 else output_shape[:-1]
    # query, value: [batch, num_heads, blocks, key_dim], key: [batch, num_heads, key_dim, blocks]
    qk_scale = qk_scale if qk_scale > 0 else (1.0 / (float(query.shape[-1]) ** 0.5))
    # print(f"{query.shape = }, {key.shape = }")
    # attention_scores = layers.Lambda(lambda xx: functional.matmul(xx[0], xx[1]))([query, key]) * qk_scale  # [batch, num_heads, q_blocks, k_blocks]
    attention_scores = query @ key
    if qk_scale != 1:
        attention_scores = attention_scores * qk_scale
    # print(f"{attention_scores.shape = }")
    if pos_emb is not None:
        # attention_scores = MultiHeadPositionalEmbedding(query_height=height, name=name and name + "attn_pos")(attention_scores)
        attention_scores = pos_emb(attention_scores)
    attention_scores = layers.Softmax(axis=-1, name=name and name + "attention_scores")(attention_scores)
    if dropout > 0:
        attention_scores = layers.Dropout(dropout, name=name and name + "attn_drop")(attention_scores)

    # output = functional.matmul(attention_scores, value)    # [batch, num_heads, q_blocks, key_dim * attn_ratio]
    # output = layers.Lambda(lambda xx: functional.matmul(xx[0], xx[1]))([attention_scores, value])
    attention_output = attention_scores @ value
    output = functional.transpose(attention_output, perm=[0, 2, 1, 3])  # [batch, q_blocks, num_heads, key_dim * attn_ratio]
    output = functional.reshape(output, [-1, *blocks, np.prod(output.shape[1:]) // np.prod(blocks)])  # [batch, q_blocks, channel * attn_ratio]

    if out_weight:
        # [batch, hh, ww, num_heads * key_dim] * [num_heads * key_dim, out] --> [batch, hh, ww, out]
        output_dim = output_dim if output_dim > 0 else value.shape[-1]
        output = layers.Dense(output_dim, use_bias=out_bias, name=name and name + "output")(output)
    return output


def qkv_to_multi_head_channels_last_format(query, key, value, num_heads, data_format=None):
    data_format = image_data_format() if data_format is None else data_format
    # print(f">>>> {query.shape = }, {key.shape = }, {value.shape = }, {num_heads = }, {data_format = }")
    if data_format == "channels_last":
        if query is not None:
            # query [batch, hh, ww, channel] -> [batch, num_heads, hh * ww, key_dim]
            query = functional.reshape(query, [-1, np.prod(query.shape[1:-1]), num_heads, query.shape[-1] // num_heads])
            query = functional.transpose(query, [0, 2, 1, 3])
        if key is not None:
            # key [batch, hh, ww, channel] -> [batch, num_heads, key_dim, hh * ww]
            key = functional.reshape(key, [-1, np.prod(key.shape[1:-1]), num_heads, key.shape[-1] // num_heads])
            key = functional.transpose(key, [0, 2, 3, 1])
        if value is not None:
            # value [batch, hh, ww, channel] -> [batch, num_heads, hh * ww, vv_dim]
            value = functional.reshape(value, [-1, np.prod(value.shape[1:-1]), num_heads, value.shape[-1] // num_heads])
            value = functional.transpose(value, [0, 2, 1, 3])
    else:
        if query is not None:
            # query [batch, channel, hh, ww] -> [batch, num_heads, hh * ww, key_dim]
            query = functional.reshape(query, [-1, num_heads, query.shape[1] // num_heads, np.prod(query.shape[2:])])
            query = functional.transpose(query, [0, 1, 3, 2])
        if key is not None:
            # key [batch, channel, hh, ww] -> [batch, num_heads, key_dim, hh * ww]
            key = functional.reshape(key, [-1, num_heads, key.shape[1] // num_heads, np.prod(key.shape[2:])])
        if value is not None:
            # value [batch, channel, hh, ww] -> [batch, num_heads, hh * ww, vv_dim]
            value = functional.reshape(value, [-1, num_heads, value.shape[1] // num_heads, np.prod(value.shape[2:])])
            value = functional.transpose(value, [0, 1, 3, 2])
    # print(f">>>> {query.shape = }, {key.shape = }, {value.shape = }, {num_heads = }, {data_format = }")
    return query, key, value


def attention_block(
    inputs,
    num_heads=4,
    key_dim=0,
    qv_bias=True,
    qkv_bias=False,
    out_weight=True,
    out_bias=False,
    use_pos_emb=False,
    use_rot_pos_emb=False,
    qk_rope=None,
    attn_height=-1,
    attn_dropout=0,
    name=None,
):
    _, bb, cc = inputs.shape
    key_dim = key_dim if key_dim > 0 else cc // num_heads
    emded_dim = num_heads * key_dim
    # print(f">>>> {bb = }, {cc = }, {emded_dim = }")

    # if qkv_bias, just use bias in qkv_dense, and set qv_bias False
    qkv_bias, qv_bias = (True, False) if qkv_bias else (False, qv_bias)

    # qv_bias = False
    # query = layers.Dense(emded_dim, use_bias=True, name=name and name + "query")(inputs)  # For loading eva02 base and large
    # key = layers.Dense(emded_dim, use_bias=False, name=name and name + "key")(inputs)  # For loading eva02 base and large
    # value = layers.Dense(emded_dim, use_bias=True, name=name and name + "value")(inputs)  # For loading eva02 base and large
    qkv = layers.Dense(emded_dim * 3, use_bias=qkv_bias, name=name and name + "qkv")(inputs)
    # qkv = functional.reshape(qkv, [-1, bb, qkv.shape[-1]])
    query, key, value = functional.split(qkv, 3, axis=-1)
    # query = [batch, num_heads, cls_token + hh * ww, key_dim]
    # print(f">>>> {query.shape = }, {key.shape = }, {value.shape = }")
    if qv_bias:
        query = BiasLayer(name=name + "query_bias")(query)
        value = BiasLayer(name=name + "value_bias")(value)

    if use_rot_pos_emb:
        # Create a new one every time, as there's no weights for this layer
        rope = PositionalEncodingFourierRot(with_cls_token=True, attn_height=attn_height, num_heads=num_heads)
        query, key = rope(query), rope(key)
    query, key, value = qkv_to_multi_head_channels_last_format(query, key, value, num_heads, data_format="channels_last")

    pos_emb = MultiHeadRelativePositionalEmbedding(attn_height=attn_height, name=name and name + "pos_emb") if use_pos_emb else None
    output_shape = [-1, bb, emded_dim]
    return scaled_dot_product_attention(query, key, value, output_shape, pos_emb, out_weight, out_bias, dropout=attn_dropout, name=name)


def mlp_block(inputs, mlp_ratio=4, is_gated=False, use_norm=False, activation="gelu", name=""):
    input_channels = inputs.shape[-1]
    if is_gated:
        nn = layers.Dense(int(input_channels * mlp_ratio) * 2, name=name + "dense_gate")(inputs)
        gate, nn = functional.split(nn, 2, axis=-1)
        # nn = layers.Dense(int(input_channels * mlp_ratio), name=name + "dense_1")(inputs)  # For loading eva02 base and large
        # gate = layers.Dense(int(input_channels * mlp_ratio), name=name + "dense_gate")(inputs)  # For loading eva02 base and large
        gate = activation_by_name(gate, activation=activation, name=name + "gate_")
        nn = gate * nn
    else:
        nn = layers.Dense(int(input_channels * mlp_ratio), name=name + "dense_1")(inputs)
        nn = activation_by_name(nn, activation, name=name)
    if use_norm:
        nn = layers.LayerNormalization(axis=-1, epsilon=LAYER_NORM_EPSILON, name=name + "scale_ln")(nn)
    nn = layers.Dense(input_channels, name=name + "dense_2")(nn)
    return nn


def attention_mlp_block(inputs, layer_scale=0.1, mlp_ratio=4, use_gated_mlp=False, use_norm_mlp=False, drop_rate=0, activation="gelu", attn_params={}, name=""):
    # print(f">>>> {inputs.shape = }, {drop_rate = }")
    nn = layers.LayerNormalization(axis=-1, epsilon=LAYER_NORM_EPSILON, name=name + "attn_ln")(inputs)  # "channels_first" also using axis=-1
    nn = attention_block(nn, **attn_params, name=name + "attn_")
    nn = ChannelAffine(use_bias=False, weight_init_value=layer_scale, name=name + "attn_gamma")(nn) if layer_scale > 0 else nn
    nn = drop_block(nn, drop_rate)
    attn_out = layers.Add(name=name + "attn_out")([inputs, nn])

    """ MLP """
    nn = layers.LayerNormalization(axis=-1, epsilon=LAYER_NORM_EPSILON, name=name + "mlp_ln")(attn_out)  # "channels_first" also using axis=-1
    nn = mlp_block(nn, mlp_ratio=mlp_ratio, is_gated=use_gated_mlp, use_norm=use_norm_mlp, activation=activation, name=name + "mlp_")
    nn = ChannelAffine(use_bias=False, weight_init_value=layer_scale, name=name + "mlp_gamma")(nn) if layer_scale > 0 else nn
    nn = drop_block(nn, drop_rate)
    # print(f">>>> {attn_out.shape = }, {nn.shape = }")
    nn = layers.Add(name=name + "mlp_output")([attn_out, nn])
    return nn


@backend.register_keras_serializable(package="beit")
class HeadInitializer(initializers.Initializer):
    def __init__(self, stddev=0.02, scale=0.001, **kwargs):
        super().__init__(**kwargs)
        self.stddev, self.scale = stddev, scale

    def __call__(self, shape, dtype="float32"):
        return initializers.TruncatedNormal(stddev=self.stddev)(shape, dtype=dtype) * self.scale

    def get_config(self):
        base_config = super().get_config()
        base_config.update({"stddev": self.stddev, "scale": self.scale})
        return base_config


@backend.register_keras_serializable(package="beit")
class PatchConv2DWithResampleWeights(layers.Conv2D):
    def load_resized_weights(self, source_layer, method="bilinear"):
        import numpy as np

        # print("source_layer:", {kk: vv.shape for kk, vv in source_layer.items()})
        if isinstance(source_layer, dict):
            source_kernel, source_bias = list(source_layer.values())  # weights
        else:
            source_kernel, source_bias = source_layer.get_weights()  # layer

        # From FlexiViT https://github.com/google-research/big_vision/blob/main/big_vision/models/proj/flexi/vit.py#L30
        # Paper [PDF 2212.08013 FlexiViT: One Model for All Patch Sizes](https://arxiv.org/pdf/2212.08013.pdf)

        # channels_last source_kernel shape `[patch_size, patch_size, in_channel, out_channel]`
        # channels_first source_kernel shape `[out_channel, in_channel, patch_size, patch_size]`
        source_kernel, source_bias = np.array(source_kernel).astype("float32"), np.array(source_bias).astype("float32")
        source_shape, target_shape = source_kernel.shape[:2], self.kernel_size  # source_kernel is from h5, must be channels_last format
        # print(f"{source_shape = }, {target_shape = }")

        # get_resize_mat(old_shape, target_shape)
        # NOTE: we are using tf.image.resize here to match the resize operations in
        # the data preprocessing pipeline.
        mat = []
        for idx in range(source_shape[0] * source_shape[1]):
            basis_vec = np.zeros(source_shape).astype("float32")
            basis_vec[np.unravel_index(idx, source_shape)] = 1.0
            vec = np.expand_dims(basis_vec, -1 if image_data_format() == "channels_last" else 0)
            vec = functional.resize(vec, target_shape, method=method)
            vec = vec.detach().numpy() if hasattr(vec, "detach") else vec.numpy()
            mat.append(vec.reshape(-1))
        resize_mat_pinv = np.linalg.pinv(np.stack(mat))

        # v_resample_kernel = jax.vmap(jax.vmap(lambda kernel: (resize_mat_pinv @ kernel.reshape(-1)).reshape(new_hw), 2, 2), 3, 3)
        # cc = v_resample_kernel(old)
        # As it's only one weight, just using two loop here, instead of `jax.vmap`
        target_weights = np.stack([[(resize_mat_pinv @ jj.reshape(-1)).reshape(target_shape) for jj in ii] for ii in source_kernel.transpose([3, 2, 0, 1])])
        if image_data_format() == "channels_last":
            target_weights = target_weights.transpose([2, 3, 1, 0])
        self.set_weights([target_weights, source_bias])


def Beit(
    depth=12,
    embed_dim=768,
    num_heads=12,
    patch_size=16,
    attn_key_dim=0,  # [Attention args]
    attn_qv_bias=True,  # Default False for Vit, True for Beit, if True and attn_qkv_bias being False, will add BiasLayer for query and key.
    attn_qkv_bias=False,  # True for Vit, False for Beit, if True, will just use bias in qkv_dense, and set qv_bias False.
    attn_out_weight=True,
    attn_out_bias=True,
    attn_dropout=0,
    use_abs_pos_emb=False,  # [Pos emb args] True for Vit, False for Beit, whether use abcolute positional embedding or relative one in attention blocks
    use_abs_pos_emb_on_cls_token=True,  # [Pos emb args] False for FlexiViT, no_embed_class in timm. If use_abs_pos_emb is True, whether apply pos_emb on cls_token.
    use_rot_pos_emb=False,  # [Pos emb args] True for EVA02, False for others
    mlp_ratio=4,  # [MLP args]
    use_gated_mlp=False,  # [MLP args] True for DINOv2 and EVA02
    use_norm_mlp=False,  # [MLP args] True for EVA02 base and large, False for others.
    use_mean_pooling_head=True,  # [Head args] False for Vit, True for Beit, whether use use mean output or `class_token` output
    use_cat_head=False,  # [Head args] True for DINOv2
    input_shape=(224, 224, 3),  # [Common args]
    num_classes=1000,
    activation="gelu",
    layer_scale=0.1,  # 0 for Vit, 0.1 for Beit, if > 0 will use `layer_scale` on block output
    drop_connect_rate=0,
    classifier_activation="softmax",
    pretrained=None,
    force_reload_mismatch=False,  # set True if patch_size changed, will force reloading pos_emb and stem_conv weights
    model_name="beit",
    kwargs=None,
):
    # Regard input_shape as force using original shape if len(input_shape) == 4,
    # else assume channel dimension is the one with min value in input_shape, and put it first or last regarding image_data_format
    input_shape = backend.align_input_shape_by_image_data_format(input_shape)
    inputs = layers.Input(input_shape)

    """ forward_embeddings """
    # nn = conv2d_no_bias(inputs, embed_dim, patch_size, strides=patch_size, padding="valid", use_bias=True, name="stem_")
    nn = PatchConv2DWithResampleWeights(embed_dim, patch_size, strides=patch_size, padding="valid", use_bias=True, name="stem_conv")(inputs)
    nn = nn if image_data_format() == "channels_last" else layers.Permute([2, 3, 1])(nn)  # channels_first -> channels_last
    patch_height = nn.shape[1]
    nn = layers.Reshape([-1, nn.shape[-1]])(nn)

    """ Positional embedding """
    if use_abs_pos_emb and use_abs_pos_emb_on_cls_token:  # ViT / EvaLarge / EvaGiant / DINOv2
        nn = ClassToken(name="cls_token")(nn)
        nn = PositionalEmbedding(input_height=patch_height, name="positional_embedding")(nn)
    elif use_abs_pos_emb:  # FlexiViT models
        nn = PositionalEmbedding(input_height=patch_height, name="positional_embedding")(nn)
        nn = ClassToken(name="cls_token")(nn)
    else:  # Beit and BeitV2
        nn = ClassToken(name="cls_token")(nn)

    """ Attention MLP body """
    attn_params = {
        "num_heads": num_heads,
        "key_dim": attn_key_dim,
        "qv_bias": attn_qv_bias,
        "qkv_bias": attn_qkv_bias,
        "out_weight": attn_out_weight,
        "out_bias": attn_out_bias,
        "attn_height": patch_height,
        "use_pos_emb": not use_abs_pos_emb,
        "use_rot_pos_emb": use_rot_pos_emb,
        "attn_dropout": attn_dropout,
    }

    drop_connect_rates = drop_connect_rates_split([depth], 0.0, drop_connect_rate)[0]
    for id in range(depth):
        name = "block{}_".format(id)
        block_drop_rate = drop_connect_rates[id]
        nn = attention_mlp_block(nn, layer_scale, mlp_ratio, use_gated_mlp, use_norm_mlp, block_drop_rate, activation, attn_params, name=name)

    """ Head """
    if use_cat_head:  # DINOv2
        nn = layers.LayerNormalization(axis=-1, epsilon=LAYER_NORM_EPSILON, name="out_ln")(nn)  # "channels_first" also using axis=-1
        nn = functional.concat([nn[:, 0], functional.reduce_mean(nn[:, 1:, :], axis=1)], axis=-1)
    elif use_mean_pooling_head:
        nn = functional.reduce_mean(nn[:, 1:, :], axis=1)
        nn = layers.LayerNormalization(axis=-1, epsilon=LAYER_NORM_EPSILON, name="out_ln")(nn)  # "channels_first" also using axis=-1
    else:  # FlexiViT
        nn = layers.LayerNormalization(axis=-1, epsilon=LAYER_NORM_EPSILON, name="out_ln")(nn)  # "channels_first" also using axis=-1
        nn = nn[:, 0]

    """ Output """
    if num_classes > 0:
        head_init = HeadInitializer()
        nn = layers.Dense(
            num_classes, dtype="float32", activation=classifier_activation, kernel_initializer=head_init, bias_initializer=head_init, name="predictions"
        )(nn)
    model = models.Model(inputs, nn, name=model_name)
    add_pre_post_process(model, rescale_mode="tf")
    mismatch_class = [PatchConv2DWithResampleWeights, PositionalEmbedding if use_abs_pos_emb else MultiHeadRelativePositionalEmbedding]
    reload_model_weights(model, PRETRAINED_DICT, "beit", pretrained, mismatch_class, force_reload_mismatch)
    return model


@register_model
def BeitBasePatch16(input_shape=(224, 224, 3), num_classes=1000, activation="gelu", classifier_activation="softmax", pretrained="imagenet21k-ft1k", **kwargs):
    embed_dim = 768
    depth = 12
    num_heads = 12
    layer_scale = kwargs.get("layer_scale", 0.1)
    force_reload_mismatch = kwargs.get("patch_size", 16) != 16  # If patch_size not 16, force reload pos_emb and stem_conv weights
    kwargs.pop("kwargs", None)  # From BeitV2BasePatch16
    return Beit(**locals(), model_name=kwargs.pop("model_name", "beit_base_patch16"), **kwargs)


@register_model
def BeitV2BasePatch16(input_shape=(224, 224, 3), num_classes=1000, activation="gelu", classifier_activation="softmax", pretrained="imagenet21k-ft1k", **kwargs):
    return BeitBasePatch16(**locals(), **kwargs, model_name="beit_v2_base_patch16")


@register_model
def BeitLargePatch16(input_shape=(224, 224, 3), num_classes=1000, activation="gelu", classifier_activation="softmax", pretrained="imagenet21k-ft1k", **kwargs):
    embed_dim = 1024
    depth = 24
    num_heads = 16
    layer_scale = kwargs.get("layer_scale", 1e-5)
    force_reload_mismatch = kwargs.get("patch_size", 16) != 16  # If patch_size not 16, force reload pos_emb and stem_conv weights
    kwargs.pop("kwargs", None)  # From BeitV2LargePatch16
    return Beit(**locals(), model_name=kwargs.pop("model_name", "beit_large_patch16"), **kwargs)


@register_model
def BeitV2LargePatch16(
    input_shape=(224, 224, 3), num_classes=1000, activation="gelu", classifier_activation="softmax", pretrained="imagenet21k-ft1k", **kwargs
):
    return BeitLargePatch16(**locals(), **kwargs, model_name="beit_v2_large_patch16")


""" Typical ViT """


def ViT(attn_qv_bias=False, attn_qkv_bias=True, use_abs_pos_emb=True, layer_scale=0, use_mean_pooling_head=False, model_name="vit", **kwargs):
    kwargs.pop("kwargs", None)
    return Beit(**locals(), **kwargs)


""" keras_model_load_weights_from_pytorch_model """


def keras_model_load_weights_from_pytorch_model(keras_model, timm_vit_model, save_name=None):
    from keras_cv_attention_models import download_and_load, attention_layers

    skip_weights = ["relative_position_index", "mask_token"]
    unstack_weights = ["cls_token", "gamma_1", "gamma_2", "relative_position_bias_table", "q_bias", "v_bias", "pos_embed"]
    if "dinov2" in keras_model.name:
        tail_align_dict = {}
    else:
        tail_align_dict = {"attn_gamma": -6, "mlp_gamma": -9, "attn_query_bias": -1, "attn_value_bias": -1, "attn_pos_emb": -1}
    if "flexivit" in keras_model.name:
        full_name_align_dict = {"cls_token": -2, "positional_embedding": -1}
    else:
        full_name_align_dict = {"cls_token": -1, "positional_embedding": -1}
    additional_transfer = {attention_layers.MultiHeadRelativePositionalEmbedding: lambda ww: [ww[0].T]}

    download_and_load.keras_reload_from_torch_model(
        torch_model=timm_vit_model,
        keras_model=keras_model,
        input_shape=keras_model.input_shape[1:-1],
        skip_weights=skip_weights,
        unstack_weights=unstack_weights,
        tail_align_dict=tail_align_dict,
        full_name_align_dict=full_name_align_dict,
        tail_split_position=1,
        additional_transfer=additional_transfer,
        save_name=save_name if save_name is not None else (keras_model.name + "_{}.h5".format(keras_model.input_shape[1])),
        do_convert=True,
        # do_predict=False if "eva_giant" in keras_model.name else True,
    )
