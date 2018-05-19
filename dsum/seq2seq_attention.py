# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================

"""trains a seq2seq model.

WORK IN PROGRESS.

Implement "Abstractive Text Summarization using Sequence-to-sequence RNNS and
Beyond."

"""
import sys
import os
import time
import datetime

import tensorflow as tf
import batch_reader
import data
import seq2seq_attention_decode
import seq2seq_attention_model

FLAGS = tf.app.flags.FLAGS
tf.app.flags.DEFINE_string('data_path',
                           '', 'Path expression to tf.Example.')
tf.app.flags.DEFINE_string('vocab_path',
                           '', 'Path expression to text vocabulary file.')
tf.app.flags.DEFINE_string('log_root', '', 'Directory for model root.')
tf.app.flags.DEFINE_string('mode', 'train', 'train/eval/decode mode')
tf.app.flags.DEFINE_integer('max_run_steps', 10000000,
                            'Maximum number of run steps.')
tf.app.flags.DEFINE_integer('max_article_sentences', 2,
                            'Max number of first sentences to use from the '
                            'article')
tf.app.flags.DEFINE_integer('max_abstract_sentences', 100,
                            'Max number of first sentences to use from the '
                            'abstract')
tf.app.flags.DEFINE_integer('beam_size', 4,
                            'beam size for beam search decoding.')
tf.app.flags.DEFINE_integer('eval_interval_secs', 60, 'How often to run eval.')
tf.app.flags.DEFINE_integer('checkpoint_secs', 1200, 'How often to checkpoint.')
tf.app.flags.DEFINE_bool('use_bucketing', False,
                         'Whether bucket articles of similar length.')
tf.app.flags.DEFINE_bool('truncate_input', False,
                         'Truncate inputs that are too long. If False, '
                         'examples that are too long are discarded.')
tf.app.flags.DEFINE_integer('random_seed', 111, 'A seed value for randomness.')
tf.app.flags.DEFINE_integer('batch_size', 128, 'The mini-batch size for training.')
tf.app.flags.DEFINE_string('tf_device', None, 'default tf.device placement instruction.')
tf.app.flags.DEFINE_integer('decode_train_step', None, 'specify a train_step for the decode procedure.')


def _Train(model, data_batcher):
  """Runs model training."""
  print(datetime.datetime.now(), '- build the model graph')
  model.build_graph()

  ckpt_saver = tf.train.Saver(keep_checkpoint_every_n_hours=12, max_to_keep=3)
  ckpt_timer = tf.train.SecondOrStepTimer(every_secs=FLAGS.checkpoint_secs)

  with tf.Session() as sess:
    # initialize or restore model
    ckpt_path = tf.train.latest_checkpoint(FLAGS.log_root)
    if ckpt_path is None:
      print(datetime.datetime.now(), '- initialize variables')
      _ = sess.run(tf.global_variables_initializer())
      summary_writer = tf.summary.FileWriter(FLAGS.log_root, graph=sess.graph)
    else:
      print(datetime.datetime.now(), '- restore model from', ckpt_path)
      ckpt_saver.restore(sess, ckpt_path)
      summary_writer = tf.summary.FileWriter(FLAGS.log_root)

    global_step = sess.run(model.global_step)
    for _ in range(global_step): _ = data_batcher.NextBatch()

    # main loop
    last_timestamp = time.time()
    print(datetime.datetime.now(), '- start of training at global_step', global_step)
    while global_step < FLAGS.max_run_steps:
      (article_batch, abstract_batch, targets, article_lens, abstract_lens,
        loss_weights, _, _) = data_batcher.NextBatch()

      (_, summary, _, global_step) = model.run_train_step(
          sess, article_batch, abstract_batch, targets, article_lens,
          abstract_lens, loss_weights)

      if global_step <= 10 or global_step <= 100 and global_step % 10 == 0 or global_step % 1000 == 0:
        print(datetime.datetime.now(), '- global_step', global_step, 'is done')

      if ckpt_timer.should_trigger_for_step(global_step):
        ckpt_saver.save(sess, os.path.join(FLAGS.log_root, 'model.ckpt'), global_step=global_step)
        ckpt_timer.update_last_triggered_step(global_step)

      # write summaries
      summary_writer.add_summary(summary, global_step)
      elapsed_time = time.time() - last_timestamp
      last_timestamp += elapsed_time
      steps_per_sec = 1 / elapsed_time if elapsed_time > 0. else float('inf')
      summary_writer.add_summary(tf.Summary(value=[tf.Summary.Value(tag='global_step/sec', simple_value=steps_per_sec)]), global_step)

    ckpt_saver.save(sess, os.path.join(FLAGS.log_root, 'model.ckpt'), global_step=global_step)
    print(datetime.datetime.now(), '- end of training at global_step', global_step)


def _Eval(model, data_batcher, vocab=None):
  """Runs model eval."""
  model.build_graph()
  saver = tf.train.Saver()
  summary_writer = tf.summary.FileWriter(os.path.join(FLAGS.log_root, 'eval'))
  sess = tf.Session(config=tf.ConfigProto(allow_soft_placement=True))
  while True:
    time.sleep(FLAGS.eval_interval_secs)
    try:
      ckpt_state = tf.train.get_checkpoint_state(FLAGS.log_root)
    except tf.errors.OutOfRangeError as e:
      tf.logging.error('Cannot restore checkpoint: %s', e)
      continue

    if not (ckpt_state and ckpt_state.model_checkpoint_path):
      tf.logging.info('No model to eval yet at %s', FLAGS.log_root)
      continue

    tf.logging.info('Loading checkpoint %s', ckpt_state.model_checkpoint_path)
    saver.restore(sess, ckpt_state.model_checkpoint_path)

    (article_batch, abstract_batch, targets, article_lens, abstract_lens,
     loss_weights, _, _) = data_batcher.NextBatch()
    (summaries, _, train_step) = model.run_eval_step(
        sess, article_batch, abstract_batch, targets, article_lens,
        abstract_lens, loss_weights)
    tf.logging.info(
        'article:  %s',
        ' '.join(data.Ids2Words(article_batch[0][:].tolist(), vocab)))
    tf.logging.info(
        'abstract: %s',
        ' '.join(data.Ids2Words(abstract_batch[0][:].tolist(), vocab)))

    summary_writer.add_summary(summaries, train_step)
    if train_step % 100 == 0:
      summary_writer.flush()


def main(unused_argv):
  vocab = data.Vocab(FLAGS.vocab_path, 1000000)

  batch_size = FLAGS.batch_size
  if FLAGS.mode == 'decode':
    batch_size = FLAGS.beam_size

  hps = seq2seq_attention_model.HParams(
      mode=FLAGS.mode,  # train, eval, decode
      min_lr=0.01,  # min learning rate.
      lr=0.15,  # learning rate
      batch_size=batch_size,
      enc_layers=4,
      enc_timesteps=120,
      dec_timesteps=30,
      min_input_len=2,  # discard articles/summaries < than this
      num_hidden=256,  # for rnn cell
      emb_dim=128,  # If 0, don't use embedding
      max_grad_norm=2,
      num_softmax_samples=0)  # If 0, no sampled softmax.

  batcher = batch_reader.Batcher(
      FLAGS.data_path, vocab, hps,
      FLAGS.max_article_sentences, FLAGS.max_abstract_sentences,
      bucketing=FLAGS.use_bucketing, truncate_input=FLAGS.truncate_input)
  tf.set_random_seed(FLAGS.random_seed)

  if hps.mode == 'train':
    model = seq2seq_attention_model.Seq2SeqAttentionModel(
        hps, vocab)
    _Train(model, batcher)
  elif hps.mode == 'eval':
    model = seq2seq_attention_model.Seq2SeqAttentionModel(
        hps, vocab)
    _Eval(model, batcher, vocab=vocab)
  elif hps.mode == 'decode':
    # Only need to restore the 1st step and reuse it since
    # we keep and feed in state for each step's output.
    decode_mdl_hps = hps._replace(dec_timesteps=1)
    model = seq2seq_attention_model.Seq2SeqAttentionModel(decode_mdl_hps, vocab)
    decoder = seq2seq_attention_decode.BSDecoder(model, batcher, hps, vocab)
    decoder.DecodeLoop(FLAGS.decode_train_step)
    print('decode done.')


def main_with_device_placement(argv):
  if FLAGS.tf_device:
    print('set default tf.device to:', FLAGS.tf_device)
    with tf.device(FLAGS.tf_device):
      main(argv)
  else:
    main(argv)


if __name__ == '__main__':
  tf.app.run(main=main_with_device_placement)

