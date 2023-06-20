import math
import numpy as np
from keras_cv_attention_models import backend
from keras_cv_attention_models.backend import layers, functional, models, image_data_format
from keras_cv_attention_models.models import register_model
from keras_cv_attention_models.attention_layers import (
    ChannelAffine,
    MlpPairwisePositionalEmbedding,
    conv2d_no_bias,
    depthwise_conv2d_no_bias,
    batchnorm_with_activation,
    add_with_layer_scale_and_drop_block,
    pad_to_divisible_by_window_size,
    reverse_padded_for_window_size,
    window_partition,
    window_reverse,
    multi_head_self_attention,
    layer_norm,
    mlp_block,
    add_pre_post_process,
)
from keras_cv_attention_models.download_and_load import reload_model_weights

BATCH_NORM_EPSILON = 1e-4
LAYER_NORM_EPSILON = 1e-6

PRETRAINED_DICT = {
    "fastervit_0": {"imagenet": {224: "afafa8c70005e14f24f7b544fd4f0caf"}},
    "fastervit_1": {"imagenet": {224: "3b309f8c894a8f822e3a0cc374381b93"}},
    "fastervit_2": {"imagenet": {224: "b1f7ff0d70859199c24af59716adec2a"}},
    "fastervit_3": {"imagenet": {224: "dea02b352abbb2e76ceb241853b55c56"}},
}


def res_conv_bn_block(inputs, layer_scale=0, drop_rate=0, activation="gelu", name=""):
    input_channel = inputs.shape[-1 if image_data_format() == "channels_last" else 1]

    nn = conv2d_no_bias(inputs, input_channel, kernel_size=3, use_bias=True, padding="SAME", name=name + "1_")  # epsilon=1e-5
    nn = batchnorm_with_activation(nn, activation=activation, name=name + "1_")

    nn = conv2d_no_bias(nn, input_channel, kernel_size=3, use_bias=True, padding="SAME", name=name + "2_")
    nn = batchnorm_with_activation(nn, activation=None, name=name + "2_")  # epsilon=1e-5
    nn = add_with_layer_scale_and_drop_block(inputs, nn, layer_scale=layer_scale, drop_rate=drop_rate, name=name)
    return nn


def attention_mlp_block(inputs, carrier_tokens=None, num_heads=4, mlp_ratio=4, pos_scale=-1, layer_scale=0, drop_rate=0, activation="gelu", name=""):
    input_channel = inputs.shape[-1]

    if carrier_tokens is not None:
        attn_height = attn_width = int(inputs.shape[1] ** 0.5)
        carrier_tokens = functional.reshape(carrier_tokens, [-1, np.prod(carrier_tokens.shape[1:-1]), carrier_tokens.shape[-1]])
        inputs = functional.concat([carrier_tokens, inputs], axis=1)
    else:
        attn_height, attn_width = inputs.shape[1:-1]

    """ attention """
    attn = layer_norm(inputs, epsilon=LAYER_NORM_EPSILON, axis=-1, name=name + "attn_")
    pos_emb = MlpPairwisePositionalEmbedding(attn_height=attn_height, attn_width=attn_width, pos_scale=pos_scale, name=name + "attn_pos")
    attn = multi_head_self_attention(attn, num_heads=num_heads, pos_emb=pos_emb, qkv_bias=True, out_bias=True, name=name + "attn_")
    if layer_scale > 0:
        # Requries for `do_propagation=True`
        ct_gamma_layer = ChannelAffine(use_bias=False, weight_init_value=layer_scale, axis=-1, name=name + "ct_gamma")
        attn = ct_gamma_layer(attn)
    else:
        ct_gamma_layer = None
    attn_out = add_with_layer_scale_and_drop_block(inputs, attn, layer_scale=0, drop_rate=drop_rate, axis=-1, name=name + "attn_out_")

    """ MLP """
    nn = layer_norm(attn_out, epsilon=LAYER_NORM_EPSILON, axis=-1, name=name + "mlp_")
    nn = mlp_block(nn, input_channel * mlp_ratio, use_bias=True, activation=activation, name=name + "mlp_")
    nn = add_with_layer_scale_and_drop_block(attn_out, nn, layer_scale=layer_scale, drop_rate=drop_rate, axis=-1, name=name + "mlp_")
    # print(f"{nn.shape = }, {attn_height = }, {attn_width = }")
    if carrier_tokens is not None:
        carrier_tokens, nn = functional.split(nn, [-1, attn_height * attn_width], axis=1)
    return nn, carrier_tokens, ct_gamma_layer


def hierarchical_attention(
    inputs, carrier_tokens=None, num_heads=4, mlp_ratio=4, pos_scale=-1, use_propagation=False, layer_scale=0, drop_rate=0, activation="gelu", name=""
):
    attn_kwargs = {"num_heads": num_heads, "mlp_ratio": mlp_ratio, "pos_scale": pos_scale, "layer_scale": layer_scale, "drop_rate": drop_rate}
    # print(f"{inputs.shape = }, {attn_kwargs = }")
    if carrier_tokens is not None:
        # print(f"{carrier_tokens.shape = }")
        carrier_tokens = MlpPairwisePositionalEmbedding(pos_scale=pos_scale, use_absolute_pos=True, name=name + "hat_pos")(carrier_tokens)
        carrier_tokens, _, ct_gamma_layer = attention_mlp_block(carrier_tokens, **attn_kwargs, activation=activation, name=name + "hat_")
        ct_patch_height, ct_patch_width = carrier_tokens.shape[1] // 2, carrier_tokens.shape[2] // 2
        carrier_tokens = window_partition(carrier_tokens, window_height=2, window_width=2)
    else:
        ct_gamma_layer = None

    # Take carrier_tokens in, for haddling shape there
    pre = MlpPairwisePositionalEmbedding(pos_scale=pos_scale, use_absolute_pos=True, name=name + "pre_attn_pos")(inputs)
    nn, carrier_tokens, _ = attention_mlp_block(pre, carrier_tokens, **attn_kwargs, activation=activation, name=name)

    if use_propagation and carrier_tokens is not None:
        nn = do_propagation(nn, carrier_tokens, ct_gamma_layer)
    elif carrier_tokens is not None:
        carrier_tokens = window_reverse(carrier_tokens, patch_height=ct_patch_height, patch_width=ct_patch_width, window_height=2, window_width=2)
    return nn, carrier_tokens


def do_propagation(inputs, carrier_tokens, ct_gamma_layer=None):
    height = width = int(float(inputs.shape[1]) ** 0.5)
    carrier_tokens = functional.reshape(carrier_tokens, [-1, 2, 2, carrier_tokens.shape[-1]])
    carrier_tokens = carrier_tokens if image_data_format() == "channels_last" else layers.Permute([3, 1, 2])(carrier_tokens)
    carrier_tokens = functional.resize(carrier_tokens, size=(height, width), method="nearest")
    carrier_tokens = carrier_tokens if image_data_format() == "channels_last" else layers.Permute([2, 3, 1])(carrier_tokens)
    carrier_tokens = functional.reshape(carrier_tokens, [-1, np.prod(carrier_tokens.shape[1:-1]), carrier_tokens.shape[-1]])
    carrier_tokens = ct_gamma_layer(carrier_tokens) if ct_gamma_layer is not None else carrier_tokens
    return inputs + carrier_tokens


def global_carrier_tokens(inputs, window_size=7, token_size=2, name=""):
    # return inputs
    nn = depthwise_conv2d_no_bias(inputs, kernel_size=3, padding="SAME", use_bias=True, name=name)
    nn = layers.AvgPool2D(pool_size=5, strides=3)(nn)  # [TODO] calculate pool_size, strides
    # nn = window_partition(nn, token_size, token_size)
    return nn


def FasterViT(
    num_blocks=[2, 3, 6, 5],
    num_heads=[2, 4, 8, 16],
    window_sizes=[8, 8, 7, 7],
    # block_types=["conv", "conv", "transform", "transform"],
    stem_hidden_dim=64,
    embed_dim=64,
    mlp_ratio=4,
    ct_size=2,
    pos_scale=-1,  # If pretrained weights are from different input_shape or window_size, pos_scale is previous actually using window_size
    use_propagation=False,
    # use_layernorm_output=False,
    input_shape=(224, 224, 3),
    num_classes=1000,
    activation="gelu",
    drop_connect_rate=0,
    layer_scale=0,
    classifier_activation="softmax",
    dropout=0,
    pretrained=None,
    model_name="fastervit",
    kwargs=None,
):
    """Patch stem"""
    # Regard input_shape as force using original shape if len(input_shape) == 4,
    # else assume channel dimension is the one with min value in input_shape, and put it first or last regarding image_data_format
    input_shape = backend.align_input_shape_by_image_data_format(input_shape)
    inputs = layers.Input(input_shape)
    nn = conv2d_no_bias(inputs, stem_hidden_dim, 3, strides=2, padding="same", name="stem_1_")
    nn = batchnorm_with_activation(nn, epsilon=BATCH_NORM_EPSILON, activation="relu", name="stem_1_")
    nn = conv2d_no_bias(nn, embed_dim, 3, strides=2, padding="same", name="stem_2_")
    nn = batchnorm_with_activation(nn, epsilon=BATCH_NORM_EPSILON, activation="relu", name="stem_2_")

    block_types = ["conv", "conv", "transform", "transform"]

    """ stage [1, 2, 3, 4] """
    total_blocks = sum(num_blocks)
    global_block_id = 0
    for stack_id, (num_block, block_type, window_size, num_head) in enumerate(zip(num_blocks, block_types, window_sizes, num_heads)):
        stack_name = "stack{}_".format(stack_id + 1)
        out_channels = embed_dim * (2**stack_id)
        is_conv_block = True if block_type[0].lower() == "c" else False
        nn = nn if is_conv_block or image_data_format() == "channels_last" else layers.Permute([2, 3, 1])(nn)  # channels_first -> channels_last

        if stack_id > 0:
            layer_norm_axis = -1 if image_data_format() == "channels_last" or not is_conv_block else 1
            nn = layer_norm(nn, epsilon=LAYER_NORM_EPSILON, axis=layer_norm_axis, name=stack_name + "downsample_")
            nn = nn if is_conv_block or image_data_format() == "channels_last" else layers.Permute([3, 1, 2], name=stack_name + "permute_pre")(nn)
            nn = conv2d_no_bias(nn, out_channels, 3, strides=2, padding="same", name=stack_name + "downsample_")
            nn = nn if is_conv_block or image_data_format() == "channels_last" else layers.Permute([2, 3, 1], name=stack_name + "permute_post")(nn)

        if not is_conv_block:
            use_carrier_tokens = nn.shape[1] > window_size or nn.shape[2] > window_size
            carrier_tokens = global_carrier_tokens(nn, window_size, token_size=2, name=stack_name + "token_") if use_carrier_tokens else None

            nn, window_height, window_width, padding_height, padding_width = pad_to_divisible_by_window_size(nn, window_size)
            patch_height, patch_width = nn.shape[1] // window_height, nn.shape[2] // window_width
            nn = window_partition(nn, window_height, window_width)
            # sr_ratio = max(patch_height, patch_width)
            nn = functional.reshape(nn, [-1, nn.shape[1] * nn.shape[2], nn.shape[-1]]) if use_carrier_tokens else nn
        else:
            use_carrier_tokens = False

        for block_id in range(num_block):
            name = "stack{}_block{}_".format(stack_id + 1, block_id + 1)
            block_drop_rate = drop_connect_rate * global_block_id / total_blocks
            cur_use_propagation = use_propagation if block_id == num_block - 1 else False
            global_block_id += 1
            if is_conv_block:
                nn = res_conv_bn_block(nn, layer_scale=0, drop_rate=block_drop_rate, activation=activation, name=name)
            else:
                nn, carrier_tokens = hierarchical_attention(
                    nn, carrier_tokens, num_head, mlp_ratio, pos_scale, cur_use_propagation, layer_scale, block_drop_rate, activation=activation, name=name
                )

        if not is_conv_block:
            nn = window_reverse(nn, patch_height, patch_width, window_height, window_width)
            nn = reverse_padded_for_window_size(nn, padding_height, padding_width)
    nn = nn if image_data_format() == "channels_last" else layers.Permute([3, 1, 2], name="permute_post")(nn)

    if num_classes > 0:
        nn = batchnorm_with_activation(nn, epsilon=BATCH_NORM_EPSILON, activation=None, name="pre_out_")
        nn = layers.GlobalAveragePooling2D(name="avg_pool")(nn)
        # nn = layers.LayerNormalization(axis=-1, epsilon=LAYER_NORM_EPSILON, name="post_ln")(nn) if use_layernorm_output else nn
        if dropout > 0:
            nn = layers.Dropout(dropout, name="head_drop")(nn)
        nn = layers.Dense(num_classes, dtype="float32", activation=classifier_activation, name="predictions")(nn)
    model = models.Model(inputs, nn, name=model_name)
    reload_model_weights(model, PRETRAINED_DICT, "fastervit", pretrained)

    add_pre_post_process(model, rescale_mode="torch")
    model.switch_to_deploy = lambda: switch_to_deploy(model)
    return model


def switch_to_deploy(model):
    from keras_cv_attention_models.model_surgery.model_surgery import convert_to_deploy

    return convert_to_deploy(model)


@register_model
def FasterViT0(input_shape=(224, 224, 3), num_classes=1000, classifier_activation="softmax", pretrained="imagenet", **kwargs):
    return FasterViT(**locals(), model_name="fastervit_0", **kwargs)


@register_model
def FasterViT1(input_shape=(224, 224, 3), num_classes=1000, classifier_activation="softmax", pretrained="imagenet", **kwargs):
    num_blocks = [1, 3, 8, 5]
    embed_dim = 80
    stem_hidden_dim = 32
    return FasterViT(**locals(), model_name="fastervit_1", **kwargs)


@register_model
def FasterViT2(input_shape=(224, 224, 3), num_classes=1000, classifier_activation="softmax", pretrained="imagenet", **kwargs):
    num_blocks = [3, 3, 8, 5]
    embed_dim = 96
    return FasterViT(**locals(), model_name="fastervit_2", **kwargs)


@register_model
def FasterViT3(input_shape=(224, 224, 3), num_classes=1000, classifier_activation="softmax", pretrained="imagenet", **kwargs):
    num_blocks = [3, 3, 12, 5]
    embed_dim = 128
    layer_scale = 1e-5
    use_propagation = True
    return FasterViT(**locals(), model_name="fastervit_3", **kwargs)


@register_model
def FasterViT4(input_shape=(224, 224, 3), num_classes=1000, classifier_activation="softmax", pretrained="imagenet", **kwargs):
    num_blocks = [3, 3, 12, 5]
    num_heads = [4, 8, 16, 32]
    embed_dim = 192
    layer_scale = 1e-5
    use_propagation = True
    return FasterViT(**locals(), model_name="fastervit_4", **kwargs)


@register_model
def FasterViT5(input_shape=(224, 224, 3), num_classes=1000, classifier_activation="softmax", pretrained="imagenet", **kwargs):
    num_blocks = [3, 3, 12, 5]
    num_heads = [4, 8, 16, 32]
    embed_dim = 320
    layer_scale = 1e-5
    use_propagation = True
    return FasterViT(**locals(), model_name="fastervit_5", **kwargs)


@register_model
def FasterViT6(input_shape=(224, 224, 3), num_classes=1000, classifier_activation="softmax", pretrained="imagenet", **kwargs):
    num_blocks = [3, 3, 16, 8]
    num_heads = [4, 8, 16, 32]
    embed_dim = 320
    layer_scale = 1e-5
    use_propagation = True
    return FasterViT(**locals(), model_name="fastervit_6", **kwargs)
