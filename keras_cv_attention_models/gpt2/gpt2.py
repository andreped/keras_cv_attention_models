import numpy as np
from keras_cv_attention_models import backend
from keras_cv_attention_models.backend import layers, models, functional
from keras_cv_attention_models.models import register_model
from keras_cv_attention_models.download_and_load import reload_model_weights


PRETRAINED_DICT = {
    "gpt2_base": {"webtext": "0426da02ebc343d8523f5c80bb5f2eab"},
    "gpt2_large": {"webtext": "fcbca87c1d32ff4cdd70c8b9f47c7824"},
    "gpt2_medium": {"webtext": "cd2284a909af2894617df5fb6f8b91e5"},
    "gpt2_xlarge": {"webtext": ["18f8e5c068a09fdde70e5716e1b62e3f", "c8c0c2e39732d9f8e482e860cb6f8058"]},
}


@backend.register_keras_serializable(package="kecam/gpt2")
class PositionalIndex(layers.Layer):
    def __init__(self, block_size=1024, **kwargs):
        super().__init__(**kwargs)
        self.block_size = block_size
        self.use_layer_as_module = True

    def build(self, input_shape):
        pos_idx = np.arange(0, self.block_size, dtype="int64")[None]
        if hasattr(self, "register_buffer"):  # PyTorch
            self.register_buffer("pos_idx", functional.convert_to_tensor(pos_idx, dtype="int64"), persistent=False)
        else:
            self.pos_idx = functional.convert_to_tensor(pos_idx, dtype="int64")
        super().build(input_shape)

    def call(self, inputs):
        # print(inputs.shape)
        return self.pos_idx[:, : inputs.shape[-1]]

    def get_config(self):
        base_config = super().get_config()
        base_config.update({"block_size": self.block_size})
        return base_config


@backend.register_keras_serializable(package="kecam/gpt2")
class CausalMask(layers.Layer):
    def __init__(self, block_size, **kwargs):
        super().__init__(**kwargs)
        self.block_size = block_size
        self.use_layer_as_module = True

    def build(self, input_shape):
        causal_mask = (1 - np.tri(self.block_size).astype("float32")[None, None]) * -1e10
        if hasattr(self, "register_buffer"):  # PyTorch
            self.register_buffer("causal_mask", functional.convert_to_tensor(causal_mask, dtype=self.compute_dtype), persistent=False)
        else:
            self.causal_mask = functional.convert_to_tensor(causal_mask, dtype=self.compute_dtype)
        super().build(input_shape)

    def call(self, inputs):
        return inputs + self.causal_mask[:, :, : inputs.shape[2], : inputs.shape[3]]

    def get_config(self):
        base_config = super().get_config()
        base_config.update({"block_size": self.block_size})
        return base_config


def causal_self_attention(inputs, block_size, num_heads, use_bias, dropout, name=""):
    input_channels = inputs.shape[-1]
    key_dim = input_channels // num_heads
    qq_scale = 1.0 / (float(key_dim) ** 0.5)

    qkv = layers.Dense(3 * input_channels, use_bias=use_bias, name=name + "qkv")(inputs)
    query, key, value = functional.split(qkv, 3, axis=-1)
    query = functional.transpose(layers.Reshape([-1, num_heads, key_dim])(query), [0, 2, 1, 3])
    key = functional.transpose(layers.Reshape([-1, num_heads, key_dim])(key), [0, 2, 3, 1])
    value = functional.transpose(layers.Reshape([-1, num_heads, key_dim])(value), [0, 2, 1, 3])

    attn = (query @ key) * qq_scale
    attn = CausalMask(block_size=block_size)(attn)
    attn = layers.Softmax(axis=-1, name=name + "attention_scores")(attn)
    attn_out = attn @ value

    output = functional.transpose(attn_out, perm=[0, 2, 1, 3])
    output = layers.Reshape([-1, input_channels])(output)
    output = layers.Dense(input_channels, use_bias=use_bias, name=name + "attn_out")(output)
    output = layers.Dropout(dropout)(output)
    return output


def attention_mlp_block(inputs, block_size, num_heads, use_bias, dropout, activation="gelu/app", name=""):
    input_channels = inputs.shape[-1]
    attn = layers.LayerNormalization(axis=-1, name=name + "attn_ln")(inputs)
    attn = causal_self_attention(attn, block_size, num_heads, use_bias, dropout, name=name + "attn.")
    attn_out = inputs + attn

    mlp = layers.LayerNormalization(axis=-1, name=name + "mlp_ln")(attn_out)
    mlp = layers.Dense(4 * input_channels, use_bias=use_bias, name=name + "mlp.0")(mlp)
    mlp = functional.gelu(mlp, approximate=True, name=name + "mlp.1")
    mlp = layers.Dense(input_channels, use_bias=use_bias, name=name + "mlp.2")(mlp)
    mlp = layers.Dropout(dropout)(mlp)

    return layers.Add(name=name + "output")([attn_out, mlp])


def GPT2(
    num_blocks=12,
    embedding_size=768,
    num_heads=12,
    block_use_bias=True,
    vocab_size=50304,
    max_block_size=1024,
    include_top=True,
    dropout=0.0,
    activation="gelu/app",
    pretrained=None,
    model_name="gpt2",
    kwargs=None,
):
    inputs = layers.Input([None], dtype="int64")
    pos_idx = PositionalIndex(block_size=max_block_size, name="pos_idx")(inputs)

    tok_emb = layers.Embedding(vocab_size, embedding_size, name="wte")(inputs)
    pos_emb = layers.Embedding(max_block_size, embedding_size, name="wpe")(pos_idx)
    nn = layers.Dropout(dropout)(tok_emb + pos_emb)

    for block_id in range(num_blocks):
        nn = attention_mlp_block(nn, max_block_size, num_heads, block_use_bias, dropout, name="blocks.{}.".format(block_id))
    nn = layers.LayerNormalization(axis=-1, name="ln_f")(nn)

    if include_top:
        nn = layers.Dense(vocab_size, use_bias=False, name="lm_head")(nn)

    model = models.Model(inputs, nn, name=model_name)
    model.max_block_size = max_block_size  # or model.get_layer('pos_idx').block_size
    model.run_prediction = RunPrediction(model)
    if pretrained == "huggingface":
        load_weights_from_huggingface(model, save_path="~/.keras/models")
    else:
        reload_model_weights(model, PRETRAINED_DICT, "gpt2", pretrained)
    return model


@register_model
def GPT2_Base(max_block_size=1024, vocab_size=50257, include_top=True, activation="gelu/app", pretrained="webtext", **kwargs):
    return GPT2(**locals(), **kwargs, model_name="gpt2_base")


@register_model
def GPT2_Medium(max_block_size=1024, vocab_size=50257, include_top=True, activation="gelu/app", pretrained="webtext", **kwargs):
    num_blocks = 24
    embedding_size = 1024
    num_heads = 16
    return GPT2(**locals(), **kwargs, model_name="gpt2_medium")


@register_model
def GPT2_Large(max_block_size=1024, vocab_size=50257, include_top=True, activation="gelu/app", pretrained="webtext", **kwargs):
    num_blocks = 36
    embedding_size = 1280
    num_heads = 20
    return GPT2(**locals(), **kwargs, model_name="gpt2_large")


@register_model
def GPT2_XLarge(max_block_size=1024, vocab_size=50257, include_top=True, activation="gelu/app", pretrained="webtext", **kwargs):
    num_blocks = 48
    embedding_size = 1600
    num_heads = 25
    return GPT2(**locals(), **kwargs, model_name="gpt2_xlarge")


""" Load weights and run prediction functions """


class RunPrediction:
    def __init__(self, model):
        self.model = model

    @staticmethod
    def softmax_numpy(inputs, axis=-1):
        exp_inputs = np.exp(inputs - np.max(inputs, axis=axis))
        return exp_inputs / np.sum(exp_inputs, keepdims=True, axis=axis)

    def __call__(self, inputs, num_samples=1, max_new_tokens=100, temperature=0.8, top_k=200):
        """
        Take a conditioning sequence of indices idx (LongTensor of shape (b,t)) and complete
        the sequence max_new_tokens times, feeding the predictions back into the model each time.
        Most likely you'll want to make sure to be in model.eval() mode of operation for this.

        Args:
          num_samples = 1  # number of samples to draw
          max_new_tokens = 100  # number of tokens generated in each sample
          temperature = 0.8  # 1.0 = no change, < 1.0 = less random, > 1.0 = more random, in predictions
          top_k = 200  # retain only the top_k most likely tokens, clamp others to have 0 probability
        """
        import tiktoken

        enc = tiktoken.get_encoding("gpt2")
        start_ids = np.array(enc.encode(inputs))

        max_block_size = self.model.get_layer("pos_idx").block_size
        vocab_size = self.model.output_shape[-1]
        vocab_indexes = np.arange(vocab_size)
        for k in range(num_samples):
            inputs_idxes = start_ids
            for _ in range(max_new_tokens):
                # if the sequence context is growing too long we must crop it at block_size
                idx_cond = inputs_idxes if inputs_idxes.shape[-1] <= max_block_size else inputs_idxes[-max_block_size:]
                # forward the model to get the logits for the index in the sequence
                logits = self.model(functional.convert_to_tensor(idx_cond, dtype="int64")[None])
                # pluck the logits at the final step and scale by desired temperature
                logits = logits[:, -1, :] / temperature
                logits = logits.detach().cpu().numpy() if hasattr(logits, "detach") else logits.numpy()

                if top_k is not None:
                    # optionally crop the logits to only the top k options
                    threshold_pos = min(top_k, vocab_size)
                    logits_threshold = np.sort(logits)[:, -threshold_pos]
                    logits[logits < logits_threshold[:, None]] = -float("Inf")

                # sample from the distribution
                probs = self.softmax_numpy(logits, axis=-1)
                multinomial_pick = np.array([np.random.choice(vocab_indexes, p=prob) for prob in probs])
                inputs_idxes = np.concatenate([inputs_idxes, multinomial_pick], axis=-1)
            print(enc.decode(inputs_idxes.tolist()))
            print("---------------")


def load_weights_from_huggingface(model, save_name=None, save_path=".", force=False):
    import os

    model_type_map = {"gpt2_base": "gpt2", "gpt2_medium": "gpt2-medium", "gpt2_large": "gpt2-large", "gpt2_xlarge": "gpt2-xl"}
    if model.name not in model_type_map:
        print("No pretrained available, model will be randomly initialized.")
        return

    pretrained = "huggingface"
    save_name = save_name if save_name is not None else "{}_{}.h5".format(model.name, pretrained)
    save_path = os.path.join(os.path.expanduser(save_path), save_name)
    if not force and os.path.exists(save_path):
        print("Load previously saved model:", save_path)
        model.load_weights(save_path)
        return
    else:
        print("Convert and load weights from huggingface")

    from transformers import GPT2LMHeadModel

    model_type = model_type_map[model.name]
    source_state_dict = GPT2LMHeadModel.from_pretrained(model_type).state_dict()

    """ state_dict_stack_by_layer """
    stacked_state_dict = {}
    for kk, vv in source_state_dict.items():
        if kk.endswith(".attn.bias") or kk.endswith(".attn.masked_bias") or kk.endswith(".num_batches_tracked"):
            continue

        split_kk = kk.split(".")
        vv = vv.numpy() if hasattr(vv, "numpy") else vv

        # split_kk[-1] in ["weight", "bias", "running_mean", "running_var", "gain"]
        layer_name = ".".join(split_kk[:-1])
        stacked_state_dict.setdefault(layer_name, []).append(vv)
    stacked_state_dict["lm_head"] = [ii.T for ii in stacked_state_dict["lm_head"]]

    """ keras_reload_stacked_state_dict """
    target_names = [ii.name for ii in model.layers if len(ii.weights) != 0]
    for target_name, source_name in zip(target_names, stacked_state_dict.keys()):
        print(">>>> Load {} weights from {}".format(target_name, source_name))
        target_layer = model.get_layer(target_name)
        source_weights = stacked_state_dict[source_name]
        print("    Target: {}, Source: {}".format([ii.shape for ii in source_weights], [ii.shape for ii in target_layer.get_weights()]))

        if hasattr(target_layer, "set_weights_channels_last"):
            target_layer.set_weights_channels_last(source_weights)  # Kecam PyTorch backend
        else:
            target_layer.set_weights(source_weights)

    print(">>>> Save to:", save_path)
    if hasattr(model, "save"):
        model.save(save_path)
    else:
        model.save_weights(save_path)  # Kecam PyTorch backend


if __name__ == "__test__":
    from keras_cv_attention_models import gpt2

    mm = gpt2.GPT2_Base()
    mm.run_prediction("hello world")
