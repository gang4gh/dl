"""The Reformer model in TF 2.x
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from absl import flags
import tensorflow as tf

import utils as model_utils
import attention_layer
from official.nlp.modeling.ops import beam_search
from official.nlp.transformer import embedding_layer
from official.nlp.transformer import ffn_layer
import metrics


flags.DEFINE_enum('positional_encoding_strategy', 'default',
                  ['default', 'concat-4', 'concat-8', 'concat-16'],
                  help='different strategy for positional encoding')
FLAGS = flags.FLAGS


def create_model(params, mode):
  """Creates model."""
  with tf.name_scope("model"):
    input_len, output_len = params['max_input_length'], params['max_target_length']
    batch_size = params['batch_size']
    if mode == 'train' or mode == 'eval':
      inputs = tf.keras.layers.Input((input_len,), batch_size, dtype="int32", name="inputs")
      targets = tf.keras.layers.Input((output_len,), batch_size, dtype="int32", name="targets")
      internal_model = Reformer(params, name="reformer")
      logits = internal_model([inputs, targets], training=mode == 'train')
      if params["enable_metrics_in_training"]:
        vocab_size = params["vocab_size"]
        label_smoothing = params["label_smoothing"]
        logits = metrics.MetricLayer(vocab_size, label_smoothing)([logits, targets])
      logits = tf.keras.layers.Lambda(lambda x: x, name="logits",
                                      dtype=tf.float32)(logits)
      model = tf.keras.Model([inputs, targets], logits)
      return model
    else:
      inputs = tf.keras.layers.Input((input_len,), dtype="int32", name="inputs")
      targets = tf.keras.layers.Input((None,), dtype="int32", name="targets")
      internal_model = Reformer(params, name="reformer")
      ret = internal_model([inputs], training=False)
      logits = internal_model([inputs, targets], training=False)
      null_logit = tf.nn.softmax(logits[0])[1]
      outputs, scores = ret["outputs"], ret["scores"]
      return tf.keras.Model([inputs, targets], [inputs, outputs, scores, null_logit])


class Reformer(tf.keras.Model):
  """Reformer model with Keras.
  IMPORTANT: THIS IS NOT A COMPLETE AND WORKING REFORMER implementation

  The Reformer model consists of an encoder and decoder. The input is an int
  sequence (or a batch of sequences). The encoder produces a continuous
  representation, and the decoder uses the encoder output to generate
  probabilities for the output sequence.
  """

  def __init__(self, params, name=None):
    """Initialize layers to build model.

    Args:
      params: hyperparameter object defining layer sizes, dropout values, etc.
      name: name of the model.
    """
    super(Reformer, self).__init__(name=name)
    self.params = params
    self.positional_encoding_strategy = pes = FLAGS.positional_encoding_strategy
    self.positional_encoding_concat_dimension = int(pes[7:]) if pes.startswith('concat-') else 0
    self.embedding_softmax_layer = embedding_layer.EmbeddingSharedWeights(
        params["vocab_size"], params["hidden_size"] - self.positional_encoding_concat_dimension)

    self.encoder_stack = EncoderStack(params)
    self.decoder_stack = DecoderStack(params)

  def get_config(self):
    return {
        "params": self.params,
    }

  def call(self, inputs, training):
    """Calculate target logits or inferred target sequences.

    Args:
      inputs: input tensor list of size 1 or 2.
        First item, inputs: int tensor with shape [batch_size, input_length].
        Second item (optional), targets: None or int tensor with shape
          [batch_size, target_length].
      training: boolean, whether in training mode or not.

    Returns:
      If targets is defined, then return logits for each word in the target
      sequence. float tensor with shape [batch_size, target_length, vocab_size]
      If target is none, then generate output sequence one token at a time.
        returns a dictionary {
          outputs: [batch_size, decoded length]
          scores: [batch_size, float]}
      Even when float16 is used, the output tensor(s) are always float32.

    Raises:
      NotImplementedError: If try to use padded decode method on CPU/GPUs.
    """
    if len(inputs) == 2:
      inputs, targets = inputs[0], inputs[1]
    else:
      inputs, targets = inputs[0], None
      if self.params["padded_decode"]:
        raise NotImplementedError("Padded decoding is not supported.")

    # Variance scaling is used here because it seems to work in many problems.
    # Other reasonable initializers may also work just as well.
    with tf.name_scope("Reformer"):
      # Calculate attention bias for encoder self-attention and decoder
      # multi-headed attention layers.
      attention_bias = model_utils.get_padding_bias(inputs)

      # Run the inputs through the encoder layer to map the symbol
      # representations to continuous representations.
      encoder_outputs = self.encode(inputs, training)
      # Generate output sequence if targets is None, or return logits if target
      # sequence is known.
      if targets is None:
        return self.predict(encoder_outputs, attention_bias, training)
      else:
        logits = self.decode(targets, encoder_outputs, attention_bias, training)
        return logits

  def _apply_positional_encoding(self, inputs):
    with tf.name_scope("pos_encoding"):
      inputs_shape = tf.shape(inputs)
      batch_size, length = inputs_shape[0], inputs_shape[1]
      if self.positional_encoding_strategy == 'default':
        pos_encoding = model_utils.get_position_encoding(length, self.params["hidden_size"])
        pos_encoding = tf.cast(pos_encoding, self.params["dtype"])
        pos_encoded_inputs = inputs + pos_encoding
      else:  # concat-n
        pos_dim = self.positional_encoding_concat_dimension
        pos_encoding = model_utils.get_position_encoding(length, pos_dim)
        pos_encoding = tf.broadcast_to(pos_encoding, [batch_size, length, pos_dim])
        pos_encoded_inputs = tf.concat([inputs, pos_encoding], axis=-1)
    return pos_encoded_inputs

  def encode(self, inputs, training):
    """Generate continuous representation for inputs.

    Args:
      inputs: int tensor with shape [batch_size, input_length].
      training: boolean, whether in training mode or not.

    Returns:
      float tensor with shape [batch_size, input_length, hidden_size]
    """
    with tf.name_scope("encode"):
      # Prepare inputs to the layer stack by adding positional encodings and
      # applying dropout.
      embedded_inputs = self.embedding_softmax_layer(inputs)
      embedded_inputs = tf.cast(embedded_inputs, self.params["dtype"])
      padding_mask = tf.cast(tf.equal(inputs, 0), tf.bool)  # assume inputs is padded by '0'

      encoder_inputs = self._apply_positional_encoding(embedded_inputs)

      if training:
        encoder_inputs = tf.nn.dropout(
            encoder_inputs, rate=self.params["layer_postprocess_dropout"])

      return self.encoder_stack(
          encoder_inputs, padding_mask, training=training)

  def decode(self, targets, encoder_outputs, attention_bias, training):
    """Generate logits for each value in the target sequence.

    Args:
      targets: target values for the output sequence. int tensor with shape
        [batch_size, target_length]
      encoder_outputs: continuous representation of input sequence. float tensor
        with shape [batch_size, input_length, hidden_size]
      attention_bias: float tensor with shape [batch_size, 1, 1, input_length]
      training: boolean, whether in training mode or not.

    Returns:
      float32 tensor with shape [batch_size, target_length, vocab_size]
    """
    with tf.name_scope("decode"):
      # Prepare inputs to decoder layers by shifting targets, adding positional
      # encoding and applying dropout.
      decoder_inputs = self.embedding_softmax_layer(targets)
      decoder_inputs = tf.cast(decoder_inputs, self.params["dtype"])
      attention_bias = tf.cast(attention_bias, self.params["dtype"])
      with tf.name_scope("shift_targets"):
        # Shift targets to the right, and remove the last element
        decoder_inputs = tf.pad(decoder_inputs,
                                [[0, 0], [1, 0], [0, 0]])[:, :-1, :]

      decoder_inputs = self._apply_positional_encoding(decoder_inputs)

      if training:
        decoder_inputs = tf.nn.dropout(
            decoder_inputs, rate=self.params["layer_postprocess_dropout"])

      # Run values
      length = tf.shape(decoder_inputs)[1]
      decoder_self_attention_bias = model_utils.get_decoder_self_attention_bias(
          length, dtype=self.params["dtype"])
      outputs = self.decoder_stack(
          decoder_inputs,
          encoder_outputs,
          decoder_self_attention_bias,
          attention_bias,
          training=training)
      if self.positional_encoding_concat_dimension > 0:
        outputs = outputs[:, :, :-self.positional_encoding_concat_dimension]
      logits = self.embedding_softmax_layer(outputs, mode="linear")
      logits = tf.cast(logits, tf.float32)
      return logits

  def _get_symbols_to_logits_fn(self, max_decode_length, training):
    """Returns a decoding function that calculates logits of the next tokens."""

    timing_signal = model_utils.get_position_encoding(
        max_decode_length, self.params["hidden_size"])
    timing_signal = tf.cast(timing_signal, self.params["dtype"])
    decoder_self_attention_bias = model_utils.get_decoder_self_attention_bias(
        max_decode_length, dtype=self.params["dtype"])

    # TODO(b/139770046): Refactor code with better naming of i.
    def symbols_to_logits_fn(ids, i, cache):
      """Generate logits for next potential IDs.

      Args:
        ids: Current decoded sequences. int tensor with shape [batch_size *
          beam_size, i + 1].
        i: Loop index.
        cache: dictionary of values storing the encoder output, encoder-decoder
          attention bias, and previous decoder attention values.

      Returns:
        Tuple of
          (logits with shape [batch_size * beam_size, vocab_size],
           updated cache values)
      """
      # Set decoder input to the last generated IDs
      decoder_input = ids[:, -1:]

      # Preprocess decoder input by getting embeddings and adding timing signal.
      decoder_input = self.embedding_softmax_layer(decoder_input)

      if self.params["padded_decode"]:
        timing_signal_shape = timing_signal.shape.as_list()
        decoder_input += tf.slice(timing_signal, [i, 0],
                                  [1, timing_signal_shape[1]])

        bias_shape = decoder_self_attention_bias.shape.as_list()
        self_attention_bias = tf.slice(
            decoder_self_attention_bias, [0, 0, i, 0],
            [bias_shape[0], bias_shape[1], 1, bias_shape[3]])
      else:
        decoder_input += timing_signal[i:i + 1]

        self_attention_bias = decoder_self_attention_bias[:, :, i:i + 1, :i + 1]

      decoder_outputs = self.decoder_stack(
          decoder_input,
          cache.get("encoder_outputs"),
          self_attention_bias,
          cache.get("encoder_decoder_attention_bias"),
          training=training,
          cache=cache,
          decode_loop_step=i if self.params["padded_decode"] else None)
      logits = self.embedding_softmax_layer(decoder_outputs, mode="linear")
      logits = tf.squeeze(logits, axis=[1])
      return logits, cache

    return symbols_to_logits_fn

  def predict(self, encoder_outputs, encoder_decoder_attention_bias, training):
    """Return predicted sequence."""
    if self.params["padded_decode"]:
      batch_size = encoder_outputs.shape.as_list()[0]
      #input_length = encoder_outputs.shape.as_list()[1]
    else:
      batch_size = tf.shape(encoder_outputs)[0]
      #input_length = tf.shape(encoder_outputs)[1]
    max_decode_length = self.params["max_target_length"]
    encoder_decoder_attention_bias = tf.cast(encoder_decoder_attention_bias,
                                             self.params["dtype"])

    symbols_to_logits_fn = self._get_symbols_to_logits_fn(
        max_decode_length, training)

    # Create initial set of IDs that will be passed into symbols_to_logits_fn.
    initial_ids = tf.zeros([batch_size], dtype=tf.int32)

    # Create cache storing decoder attention values for each layer.
    # pylint: disable=g-complex-comprehension
    init_decode_length = (
        max_decode_length if self.params["padded_decode"] else 0)
    num_heads = self.params["num_heads"]
    dim_per_head = self.params["hidden_size"] // num_heads
    cache = {
        "layer_%d" % layer: {
            "k":
                tf.zeros([
                    batch_size, init_decode_length, num_heads, dim_per_head
                ],
                         dtype=self.params["dtype"]),
            "v":
                tf.zeros([
                    batch_size, init_decode_length, num_heads, dim_per_head
                ],
                         dtype=self.params["dtype"])
        } for layer in range(self.params["num_hidden_layers"])
    }
    # pylint: enable=g-complex-comprehension

    # Add encoder output and attention bias to the cache.
    cache["encoder_outputs"] = encoder_outputs
    cache["encoder_decoder_attention_bias"] = encoder_decoder_attention_bias

    # Use beam search to find the top beam_size sequences and scores.
    decoded_ids, scores = beam_search.sequence_beam_search(
        symbols_to_logits_fn=symbols_to_logits_fn,
        initial_ids=initial_ids,
        initial_cache=cache,
        vocab_size=self.params["vocab_size"],
        beam_size=self.params["beam_size"],
        alpha=self.params["alpha"],
        max_decode_length=max_decode_length,
        eos_id=self.params["vocab_size"] + 1,  # EOS_ID,
        padded_decode=self.params["padded_decode"],
        dtype=self.params["dtype"])

    # Get the top sequence for each batch element
    top_decoded_ids = decoded_ids[:, 0, 1:]
    top_scores = scores[:, 0]

    return {"outputs": top_decoded_ids, "scores": top_scores}


class LayerNormalization(tf.keras.layers.Layer):
  """Applies layer normalization."""

  def __init__(self, hidden_size):
    super(LayerNormalization, self).__init__()
    self.hidden_size = hidden_size

  def build(self, input_shape):
    """Builds the layer."""
    # Passing experimental_autocast=False causes these variables to not be
    # automatically casted to fp16 when mixed precision is used. Since we use
    # float32 in call() for numeric stability, we do not want variables to be
    # casted to fp16.
    self.scale = self.add_weight(
        "layer_norm_scale",
        shape=[self.hidden_size],
        dtype="float32",
        initializer=tf.ones_initializer(),
        experimental_autocast=False)
    self.bias = self.add_weight(
        "layer_norm_bias",
        shape=[self.hidden_size],
        dtype="float32",
        initializer=tf.zeros_initializer(),
        experimental_autocast=False)
    super(LayerNormalization, self).build(input_shape)

  def get_config(self):
    return {
        "hidden_size": self.hidden_size,
    }

  def call(self, x, epsilon=1e-6):
    input_dtype = x.dtype
    if input_dtype == tf.float16:
      x = tf.cast(x, tf.float32)
    mean = tf.reduce_mean(x, axis=[-1], keepdims=True)
    variance = tf.reduce_mean(tf.square(x - mean), axis=[-1], keepdims=True)
    norm_x = (x - mean) * tf.math.rsqrt(variance + epsilon)
    return tf.cast(norm_x * self.scale + self.bias, input_dtype)


class PrePostProcessingWrapper(tf.keras.layers.Layer):
  """Wrapper class that applies layer pre-processing and post-processing."""

  def __init__(self, layer, params):
    super(PrePostProcessingWrapper, self).__init__()
    self.layer = layer
    self.params = params
    self.postprocess_dropout = params["layer_postprocess_dropout"]

  def build(self, input_shape):
    # Create normalization layer
    self.layer_norm = LayerNormalization(self.params["hidden_size"])
    super(PrePostProcessingWrapper, self).build(input_shape)

  def get_config(self):
    return {
        "params": self.params,
    }

  def call(self, x, *args, **kwargs):
    """Calls wrapped layer with same parameters."""
    # Preprocessing: apply layer normalization
    training = kwargs["training"]

    y = self.layer_norm(x)

    # Get layer output
    y = self.layer(y, *args, **kwargs)

    # Postprocessing: apply dropout and residual connection
    if training:
      y = tf.nn.dropout(y, rate=self.postprocess_dropout)

    # TODO: should the layer_norm be applied after layer according to the paper?
    return x + y


class EncoderStack(tf.keras.layers.Layer):
  """Reformer encoder stack.

  The encoder stack is made up of N identical layers. Each layer is composed
  of the sublayers:
    1. Self-attention layer
    2. Feedforward network (which is 2 fully-connected layers)
  """

  def __init__(self, params):
    super(EncoderStack, self).__init__()
    self.params = params
    self.layers = []

  def build(self, input_shape):
    """Builds the encoder stack."""
    params = self.params
    for _ in range(params["num_hidden_layers"]):
      # Create sublayers for each layer.
      self_attention_layer = attention_layer.LshSelfAttention(
          params["hidden_size"], params["num_heads"],
          params["lsh_attention_dropout"], params["num_hashes"], params['test_num_hashes'], params["bucket_size"], params["use_full_attention_in_reformer"])
      feed_forward_network = ffn_layer.FeedForwardNetwork(
          params["hidden_size"], params["filter_size"], params["relu_dropout"])

      self.layers.append([
          PrePostProcessingWrapper(self_attention_layer, params),
          PrePostProcessingWrapper(feed_forward_network, params)
      ])

    # Create final layer normalization layer.
    self.output_normalization = LayerNormalization(params["hidden_size"])
    super(EncoderStack, self).build(input_shape)

  def get_config(self):
    return {
        "params": self.params,
    }

  def call(self, encoder_inputs, padding_mask, training):
    """Return the output of the encoder layer stacks.

    Args:
      encoder_inputs: tensor with shape [batch_size, input_length, hidden_size]
      padding_mask: mask for the encoder self-attention layer, with shape [batch_size, input_length]
      training: boolean, whether in training mode or not.

    Returns:
      Output of encoder layer stack.
      float32 tensor with shape [batch_size, input_length, hidden_size]
    """
    for n, layer in enumerate(self.layers):
      # Run inputs through the sublayers.
      self_attention_layer = layer[0]
      feed_forward_network = layer[1]

      with tf.name_scope("layer_%d" % n):
        with tf.name_scope("self_attention"):
          encoder_inputs = self_attention_layer(
              encoder_inputs, padding_mask, training=training)
        with tf.name_scope("ffn"):
          encoder_inputs = feed_forward_network(
              encoder_inputs, training=training)

    return self.output_normalization(encoder_inputs)


class DecoderStack(tf.keras.layers.Layer):
  """Reformer decoder stack.

  Like the encoder stack, the decoder stack is made up of N identical layers.
  Each layer is composed of the sublayers:
    1. Self-attention layer
    2. Multi-headed attention layer combining encoder outputs with results from
       the previous self-attention layer.
    3. Feedforward network (2 fully-connected layers)
  """

  def __init__(self, params):
    super(DecoderStack, self).__init__()
    self.params = params
    self.layers = []

  def build(self, input_shape):
    """Builds the decoder stack."""
    params = self.params
    for _ in range(params["num_hidden_layers"]):
      self_attention_layer = attention_layer.SelfAttention(
          params["hidden_size"], params["num_heads"],
          params["attention_dropout"])
      enc_dec_attention_layer = attention_layer.Attention(
          params["hidden_size"], params["num_heads"],
          params["attention_dropout"])
      feed_forward_network = ffn_layer.FeedForwardNetwork(
          params["hidden_size"], params["filter_size"], params["relu_dropout"])

      self.layers.append([
          PrePostProcessingWrapper(self_attention_layer, params),
          PrePostProcessingWrapper(enc_dec_attention_layer, params),
          PrePostProcessingWrapper(feed_forward_network, params)
      ])
    self.output_normalization = LayerNormalization(params["hidden_size"])
    super(DecoderStack, self).build(input_shape)

  def get_config(self):
    return {
        "params": self.params,
    }

  def call(self,
           decoder_inputs,
           encoder_outputs,
           decoder_self_attention_bias,
           attention_bias,
           training,
           cache=None,
           decode_loop_step=None):
    """Return the output of the decoder layer stacks.

    Args:
      decoder_inputs: A tensor with shape
        [batch_size, target_length, hidden_size].
      encoder_outputs: A tensor with shape
        [batch_size, input_length, hidden_size]
      decoder_self_attention_bias: A tensor with shape
        [1, 1, target_len, target_length], the bias for decoder self-attention
        layer.
      attention_bias: A tensor with shape [batch_size, 1, 1, input_length],
        the bias for encoder-decoder attention layer.
      training: A bool, whether in training mode or not.
      cache: (Used for fast decoding) A nested dictionary storing previous
        decoder self-attention values. The items are:
          {layer_n: {"k": A tensor with shape [batch_size, i, key_channels],
                     "v": A tensor with shape [batch_size, i, value_channels]},
                       ...}
      decode_loop_step: An integer, the step number of the decoding loop. Used
        only for autoregressive inference on TPU.

    Returns:
      Output of decoder layer stack.
      float32 tensor with shape [batch_size, target_length, hidden_size]
    """
    for n, layer in enumerate(self.layers):
      self_attention_layer = layer[0]
      enc_dec_attention_layer = layer[1]
      feed_forward_network = layer[2]

      # Run inputs through the sublayers.
      layer_name = "layer_%d" % n
      layer_cache = cache[layer_name] if cache is not None else None
      with tf.name_scope(layer_name):
        with tf.name_scope("self_attention"):
          decoder_inputs = self_attention_layer(
              decoder_inputs,
              decoder_self_attention_bias,
              training=training,
              cache=layer_cache,
              decode_loop_step=decode_loop_step)
        with tf.name_scope("encdec_attention"):
          decoder_inputs = enc_dec_attention_layer(
              decoder_inputs,
              encoder_outputs,
              attention_bias,
              training=training)
        with tf.name_scope("ffn"):
          decoder_inputs = feed_forward_network(
              decoder_inputs, training=training)

    return self.output_normalization(decoder_inputs)
