from keras_cv_attention_models import backend
from keras_cv_attention_models.backend import layers, models, functional, image_data_format
from keras_cv_attention_models.models import register_model
from keras_cv_attention_models.attention_layers import activation_by_name, add_pre_post_process
from keras_cv_attention_models.download_and_load import reload_model_weights

BATCH_NORM_EPSILON = 1e-5

PRETRAINED_DICT = {
    "mlp_mixer_b16": {
        "imagenet21k": "6353dffc590a2a7348a44cee2c784724",
        "imagenet": "abd04090063ba9ab0d49e2131cef9d64",
        "imagenet_sam": "d953ef41ffdb0ab9c3fa21493bf0982f",
    },
    "mlp_mixer_l16": {"imagenet": "fa91a74f1aa11ed610299d06d643ed45", "imagenet21k": "8dca5de1817112d9e717db6b2e9a7b0b"},
    "mlp_mixer_b32": {"imagenet_sam": "a6285750e55579fc68e7ba68a683c77d"},
}


def layer_norm(inputs, axis="auto", name=None):
    """Typical LayerNormalization with epsilon=1e-5"""
    norm_axis = (-1 if backend.image_data_format() == "channels_last" else 1) if axis == "auto" else axis
    return layers.LayerNormalization(axis=norm_axis, epsilon=BATCH_NORM_EPSILON, name=name)(inputs)


def mlp_block(inputs, hidden_dim, output_channel=-1, drop_rate=0, use_conv=False, use_bias=True, activation="gelu", name=None):
    channnel_axis = -1 if image_data_format() == "channels_last" else (1 if use_conv else -1)
    output_channel = output_channel if output_channel > 0 else inputs.shape[channnel_axis]
    if use_conv:
        nn = layers.Conv2D(hidden_dim, kernel_size=1, use_bias=use_bias, name=name and name + "Conv_0")(inputs)
    else:
        nn = layers.Dense(hidden_dim, use_bias=use_bias, name=name and name + "Dense_0")(inputs)
    nn = activation_by_name(nn, activation, name=name)
    nn = layers.Dropout(drop_rate)(nn) if drop_rate > 0 else nn
    if use_conv:
        nn = layers.Conv2D(output_channel, kernel_size=1, use_bias=use_bias, name=name and name + "Conv_1")(nn)
    else:
        nn = layers.Dense(output_channel, use_bias=use_bias, name=name and name + "Dense_1")(nn)
    nn = layers.Dropout(drop_rate)(nn) if drop_rate > 0 else nn
    return nn


def mlp_mixer_block(inputs, tokens_mlp_dim, channels_mlp_dim, use_bias=True, drop_rate=0, activation="gelu", data_format=None, name=None):
    data_format = backend.image_data_format() if data_format is None else data_format
    norm_axis = -1 if data_format == "channels_last" else 1

    nn = layer_norm(inputs, axis=norm_axis, name=name and name + "LayerNorm_0")
    nn = layers.Permute((2, 1), name=name and name + "permute_0")(nn) if data_format == "channels_last" else nn
    nn = mlp_block(nn, tokens_mlp_dim, use_bias=use_bias, activation=activation, name=name and name + "token_mixing/")
    nn = layers.Permute((2, 1), name=name and name + "permute_1")(nn) if data_format == "channels_last" else nn
    if drop_rate > 0:
        nn = layers.Dropout(drop_rate, noise_shape=(None, 1, 1), name=name and name + "token_drop")(nn)
    token_out = layers.Add(name=name and name + "add_0")([nn, inputs])

    nn = layer_norm(token_out, axis=norm_axis, name=name and name + "LayerNorm_1")
    nn = nn if data_format == "channels_last" else layers.Permute((2, 1), name=name and name + "permute_2")(nn)
    nn = mlp_block(nn, channels_mlp_dim, use_bias=use_bias, activation=activation, name=name and name + "channel_mixing/")
    channel_out = nn if data_format == "channels_last" else layers.Permute((2, 1), name=name and name + "permute_3")(nn)
    if drop_rate > 0:
        channel_out = layers.Dropout(drop_rate, noise_shape=(None, 1, 1), name=name and name + "channel_drop")(channel_out)
    return layers.Add(name=name and name + "output")([channel_out, token_out])


def MLPMixer(
    num_blocks,
    patch_size,
    stem_width,
    tokens_mlp_dim,
    channels_mlp_dim,
    input_shape=(224, 224, 3),
    num_classes=0,
    activation="gelu",
    sam_rho=0,
    dropout=0,
    drop_connect_rate=0,
    classifier_activation="softmax",
    pretrained="imagenet",
    model_name="mlp_mixer",
    kwargs=None,
):
    # Regard input_shape as force using original shape if len(input_shape) == 4,
    # else assume channel dimension is the one with min value in input_shape, and put it first or last regarding image_data_format
    input_shape = backend.align_input_shape_by_image_data_format(input_shape)
    inputs = layers.Input(input_shape)

    nn = layers.Conv2D(stem_width, kernel_size=patch_size, strides=patch_size, padding="VALID", name="stem")(inputs)
    new_shape = [nn.shape[1] * nn.shape[2], stem_width] if backend.image_data_format() == "channels_last" else [stem_width, nn.shape[2] * nn.shape[3]]
    nn = layers.Reshape(new_shape)(nn)

    drop_connect_s, drop_connect_e = drop_connect_rate if isinstance(drop_connect_rate, (list, tuple)) else [drop_connect_rate, drop_connect_rate]
    for ii in range(num_blocks):
        name = "{}_{}/".format("MixerBlock", str(ii))
        block_drop_rate = drop_connect_s + (drop_connect_e - drop_connect_s) * ii / num_blocks
        nn = mlp_mixer_block(nn, tokens_mlp_dim, channels_mlp_dim, drop_rate=block_drop_rate, activation=activation, name=name)
    nn = layer_norm(nn, name="pre_head_layer_norm")

    if num_classes > 0:
        nn = layers.GlobalAveragePooling1D()(nn)  # tf.reduce_mean(nn, axis=1)
        if dropout > 0 and dropout < 1:
            nn = layers.Dropout(dropout)(nn)
        nn = layers.Dense(num_classes, dtype="float32", activation=classifier_activation, name="head")(nn)

    if sam_rho != 0:
        from keras_cv_attention_models.model_surgery import SAMModel

        model = SAMModel(inputs, nn, name=model_name)
    else:
        model = models.Model(inputs, nn, name=model_name)
    add_pre_post_process(model, rescale_mode="tf")
    reload_model_weights(model, pretrained_dict=PRETRAINED_DICT, sub_release="mlp_family", pretrained=pretrained)
    return model


BLOCK_CONFIGS = {
    "s32": {
        "num_blocks": 8,
        "patch_size": 32,
        "stem_width": 512,
        "tokens_mlp_dim": 256,
        "channels_mlp_dim": 2048,
    },
    "s16": {
        "num_blocks": 8,
        "patch_size": 16,
        "stem_width": 512,
        "tokens_mlp_dim": 256,
        "channels_mlp_dim": 2048,
    },
    "b32": {
        "num_blocks": 12,
        "patch_size": 32,
        "stem_width": 768,
        "tokens_mlp_dim": 384,
        "channels_mlp_dim": 3072,
    },
    "b16": {
        "num_blocks": 12,
        "patch_size": 16,
        "stem_width": 768,
        "tokens_mlp_dim": 384,
        "channels_mlp_dim": 3072,
    },
    "l32": {
        "num_blocks": 24,
        "patch_size": 32,
        "stem_width": 1024,
        "tokens_mlp_dim": 512,
        "channels_mlp_dim": 4096,
    },
    "l16": {
        "num_blocks": 24,
        "patch_size": 16,
        "stem_width": 1024,
        "tokens_mlp_dim": 512,
        "channels_mlp_dim": 4096,
    },
    "h14": {
        "num_blocks": 32,
        "patch_size": 14,
        "stem_width": 1280,
        "tokens_mlp_dim": 640,
        "channels_mlp_dim": 5120,
    },
}


@register_model
def MLPMixerS32(input_shape=(224, 224, 3), num_classes=1000, activation="gelu", classifier_activation="softmax", pretrained="imagenet", **kwargs):
    return MLPMixer(**BLOCK_CONFIGS["s32"], **locals(), model_name="mlp_mixer_s32", **kwargs)


@register_model
def MLPMixerS16(input_shape=(224, 224, 3), num_classes=1000, activation="gelu", classifier_activation="softmax", pretrained="imagenet", **kwargs):
    return MLPMixer(**BLOCK_CONFIGS["s16"], **locals(), model_name="mlp_mixer_s16", **kwargs)


@register_model
def MLPMixerB32(input_shape=(224, 224, 3), num_classes=1000, activation="gelu", classifier_activation="softmax", pretrained="imagenet", **kwargs):
    return MLPMixer(**BLOCK_CONFIGS["b32"], **locals(), model_name="mlp_mixer_b32", **kwargs)


@register_model
def MLPMixerB16(input_shape=(224, 224, 3), num_classes=1000, activation="gelu", classifier_activation="softmax", pretrained="imagenet", **kwargs):
    return MLPMixer(**BLOCK_CONFIGS["b16"], **locals(), model_name="mlp_mixer_b16", **kwargs)


@register_model
def MLPMixerL32(input_shape=(224, 224, 3), num_classes=1000, activation="gelu", classifier_activation="softmax", pretrained="imagenet", **kwargs):
    return MLPMixer(**BLOCK_CONFIGS["l32"], **locals(), model_name="mlp_mixer_l32", **kwargs)


@register_model
def MLPMixerL16(input_shape=(224, 224, 3), num_classes=1000, activation="gelu", classifier_activation="softmax", pretrained="imagenet", **kwargs):
    return MLPMixer(**BLOCK_CONFIGS["l16"], **locals(), model_name="mlp_mixer_l16", **kwargs)


@register_model
def MLPMixerH14(input_shape=(224, 224, 3), num_classes=1000, activation="gelu", classifier_activation="softmax", pretrained="imagenet", **kwargs):
    return MLPMixer(**BLOCK_CONFIGS["h14"], **locals(), model_name="mlp_mixer_h14", **kwargs)


if __name__ == "__convert__":
    aa = np.load("../models/imagenet1k_Mixer-B_16.npz")
    bb = {kk: vv for kk, vv in aa.items()}
    # cc = {kk: vv.shape for kk, vv in bb.items()}

    import mlp_mixer

    mm = mlp_mixer.MLPMixerB16(num_classes=1000, pretrained=None)
    # dd = {ii.name: ii.shape for ii in mm.weights}

    target_weights_dict = {"kernel": 0, "bias": 1, "scale": 0, "running_var": 3}
    for kk, vv in bb.items():
        split_name = kk.split("/")
        source_name = "/".join(split_name[:-1])
        source_weight_type = split_name[-1]
        target_layer = mm.get_layer(source_name)

        target_weights = target_layer.get_weights()
        target_weight_pos = target_weights_dict[source_weight_type]
        print("[{}] source: {}, target: {}".format(kk, vv.shape, target_weights[target_weight_pos].shape))

        target_weights[target_weight_pos] = vv
        target_layer.set_weights(target_weights)
