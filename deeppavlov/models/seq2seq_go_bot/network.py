# Copyright 2017 Neural Networks and Deep Learning lab, MIPT
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

import json
import tensorflow as tf
import numpy as np
from typing import List
import math

from deeppavlov.core.common.registry import register
from deeppavlov.core.common.errors import ConfigError
from deeppavlov.core.models.lr_scheduled_tf_model import LRScheduledTFModel
from deeppavlov.core.common.log import get_logger
from deeppavlov.models.seq2seq_go_bot.kb_attn_layer import KBAttention


log = get_logger(__name__)


@register("seq2seq_go_bot_nn")
class Seq2SeqGoalOrientedBotNetwork(LRScheduledTFModel):
    """
    The :class:`~deeppavlov.models.seq2seq_go_bot.bot.GoalOrientedBotNetwork`
    is a recurrent network that encodes user utterance and generates response
    in a sequence-to-sequence manner.

    For network architecture is similar to https://arxiv.org/abs/1705.05414 .

    Parameters:
        hidden_size: RNN hidden layer size.
        source_vocab_size: size of a vocabulary of encoder tokens.
        target_vocab_size: size of a vocabulary of decoder tokens.
        target_start_of_sequence_index: index of a start of sequence token during
            decoding.
        target_end_of_sequence_index: index of an end of sequence token during decoding.
        knowledge_base_size: number of knowledge base entries.
        kb_attention_hidden_sizes: list of sizes for attention hidden units.
        decoder_embeddings: matrix with embeddings for decoder output tokens, size is
            (`targer_vocab_size` + number of knowledge base entries, embedding size).
        beam_width: width of beam search decoding.
        dropout_rate: probability of weights' dropout.
        state_dropout_rate: probability of rnn state dropout.
        optimizer: one of tf.train.Optimizer subclasses as a string.
        **kwargs: parameters passed to a parent
            :class:`~deeppavlov.core.models.tf_model.TFModel` class.
    """

    GRAPH_PARAMS = ['knowledge_base_size', 'source_vocab_size',
                    'target_vocab_size', 'hidden_size', 'embedding_size',
                    'kb_embedding_control_sum', 'kb_attention_hidden_sizes']

    def __init__(self,
                 hidden_size: int,
                 source_vocab_size: int,
                 target_vocab_size: int,
                 target_start_of_sequence_index: int,
                 target_end_of_sequence_index: int,
                 decoder_embeddings: np.ndarray,
                 knowledge_base_entry_embeddings: np.ndarray = [[]],
                 kb_attention_hidden_sizes: List[int] = [],
                 beam_width: int = 1,
                 dropout_rate: float = 0.0,
                 state_dropout_rate: float = 0.0,
                 optimizer: str = 'AdamOptimizer',
                 **kwargs) -> None:

        # initialize knowledge base embeddings
        self.kb_embedding = np.array(knowledge_base_entry_embeddings)
        if self.kb_embedding.shape[1] > 0:
            self.kb_size = self.kb_embedding.shape[0]
            log.debug("recieved knowledge_base_entry_embeddings with shape = {}"
                      .format(self.kb_embedding.shape))
        else:
            self.kb_size = 0
        # initialize decoder embeddings
        self.decoder_embedding = np.array(decoder_embeddings)
        if self.kb_size > 0:
            if self.kb_embedding.shape[1] != self.decoder_embedding.shape[1]:
                raise ValueError("decoder embeddings should have the same dimension"
                                 " as knowledge base entries' embeddings")
        super().__init__(**kwargs)

        # specify model options
        self.opt = {
            'hidden_size': hidden_size,
            'source_vocab_size': source_vocab_size,
            'target_vocab_size': target_vocab_size,
            'target_start_of_sequence_index': target_start_of_sequence_index,
            'target_end_of_sequence_index': target_end_of_sequence_index,
            'kb_attention_hidden_sizes': kb_attention_hidden_sizes,
            'kb_embedding_control_sum': float(np.sum(self.kb_embedding)),
            'knowledge_base_size': self.kb_size,
            'embedding_size': self.decoder_embedding.shape[1],
            'beam_width': beam_width,
            'dropout_rate': dropout_rate,
            'state_dropout_rate': state_dropout_rate,
            'optimizer': optimizer
        }

        # initialize other parameters
        self._init_params()
        # build computational graph
        self._build_graph()
        # initialize session
        self.sess = tf.Session()
        # from tensorflow.python import debug as tf_debug
        # self.sess = tf_debug.TensorBoardDebugWrapperSession(self.sess, "vimary-pc:7019")

        self.sess.run(tf.global_variables_initializer())

        if tf.train.checkpoint_exists(str(self.load_path.resolve())):
            log.info("[initializing `{}` from saved]".format(self.__class__.__name__))
            self.load()
        else:
            log.info("[initializing `{}` from scratch]".format(self.__class__.__name__))

    def _init_params(self):
        self.hidden_size = self.opt['hidden_size']
        self.src_vocab_size = self.opt['source_vocab_size']
        self.tgt_vocab_size = self.opt['target_vocab_size']
        self.tgt_sos_id = self.opt['target_start_of_sequence_index']
        self.tgt_eos_id = self.opt['target_end_of_sequence_index']
        self.kb_attn_hidden_sizes = self.opt['kb_attention_hidden_sizes']
        self.embedding_size = self.opt['embedding_size']
        self.beam_width = self.opt['beam_width']
        self.dropout_rate = self.opt['dropout_rate']
        self.state_dropout_rate = self.opt['state_dropout_rate']

        self._optimizer = None
        if hasattr(tf.train, self.opt['optimizer']):
            self._optimizer = getattr(tf.train, self.opt['optimizer'])
        if not issubclass(self._optimizer, tf.train.Optimizer):
            raise ConfigError("`optimizer` parameter should be a name of"
                              " tf.train.Optimizer subclass")

    def _build_graph(self):

        self._add_placeholders()

        _logits, self._predictions = self._build_body()

        _weights = tf.expand_dims(self._tgt_mask, -1)
        _loss_tensor = \
            tf.losses.sparse_softmax_cross_entropy(logits=_logits,
                                                   labels=self._decoder_outputs,
                                                   weights=_weights,
                                                   reduction=tf.losses.Reduction.NONE)
        # normalize loss by batch_size
        _loss_tensor = \
            tf.verify_tensor_all_finite(_loss_tensor, "Non finite values in loss tensor.")
        self._loss = tf.reduce_sum(_loss_tensor) / tf.cast(self._batch_size, tf.float32)
        # self._loss = tf.reduce_mean(_loss_tensor, name='loss')
# TODO: tune clip_norm
        self._train_op = self.get_train_op(self._loss, optimizer=self._optimizer)
        log.info("Trainable variables")
        for v in tf.trainable_variables():
            log.info(v)
        self.print_number_of_parameters()

    def _add_placeholders(self):
        self._dropout_keep_prob = tf.placeholder_with_default(
            1.0, shape=[], name='dropout_keep_prob')
        self._state_dropout_keep_prob = tf.placeholder_with_default(
            1.0, shape=[], name='state_dropout_keep_prob')
        # _encoder_inputs: [batch_size, max_input_time]
        # _encoder_inputs: [batch_size, max_input_time, embedding_size]
        self._encoder_inputs = tf.placeholder(tf.float32,
                                              [None, None, self.embedding_size],
                                              name='encoder_inputs')
        self._batch_size = tf.shape(self._encoder_inputs)[0]
        # _decoder_inputs: [batch_size, max_output_time]
        self._decoder_inputs = tf.placeholder(tf.int32,
                                              [None, None],
                                              name='decoder_inputs')
        # _decoder_embedding: [tgt_vocab_size + kb_size, embedding_size]
        self._decoder_embedding = \
            tf.get_variable("decoder_embedding",
                            shape=(self.tgt_vocab_size + self.kb_size,
                                   self.embedding_size),
                            dtype=tf.float32,
                            initializer=tf.constant_initializer(self.decoder_embedding),
                            trainable=False)
        # _decoder_outputs: [batch_size, max_output_time]
        self._decoder_outputs = tf.placeholder(tf.int32,
                                               [None, None],
                                               name='decoder_outputs')
        # _kb_embedding: [kb_size, embedding_size]
# TODO: try training embeddings
        kb_W = np.array(self.kb_embedding)[:, :self.embedding_size]
        self._kb_embedding = tf.get_variable("kb_embedding",
                                             shape=(kb_W.shape[0], kb_W.shape[1]),
                                             dtype=tf.float32,
                                             initializer=tf.constant_initializer(kb_W),
                                             trainable=True)
        # _kb_mask: [batch_size, kb_size]
        self._kb_mask = tf.placeholder(tf.float32, [None, None], name='kb_mask')
        # _tgt_mask: [batch_size, max_output_time]
        self._tgt_mask = tf.placeholder(tf.int32, [None, None], name='target_mask')
        # _src_sequence_lengths, _tgt_sequence_lengths: [batch_size]
        self._src_sequence_lengths = tf.placeholder(tf.int32,
                                                    [None],
                                                    name='input_sequence_lengths')
        self._tgt_sequence_lengths = tf.to_int32(tf.reduce_sum(self._tgt_mask, axis=1))

    def _build_body(self):
        self._build_encoder()
        self._build_decoder()
        return self._logits, self._predictions

    def _build_encoder(self):
        with tf.variable_scope("Encoder"):
            # Encoder embedding
            # _encoder_embedding = tf.get_variable(
            #   "encoder_embedding", [self.src_vocab_size, self.embedding_size])
            # _encoder_emb_inp = tf.nn.embedding_lookup(_encoder_embedding,
            #                                          self._encoder_inputs)
            # _encoder_emb_inp = tf.one_hot(self._encoder_inputs, self.src_vocab_size)
            _encoder_emb_inp = self._encoder_inputs

            _encoder_cell = tf.nn.rnn_cell.LSTMCell(self.hidden_size,
                                                    name='basic_lstm_cell')
            _encoder_cell = tf.contrib.rnn.DropoutWrapper(
                _encoder_cell,
                input_size=self.embedding_size,
                dtype=tf.float32,
                input_keep_prob=self._dropout_keep_prob,
                output_keep_prob=self._dropout_keep_prob,
                state_keep_prob=self._state_dropout_keep_prob,
                variational_recurrent=True)
            # Run Dynamic RNN
            #   _encoder_outputs: [max_time, batch_size, hidden_size]
            #   _encoder_state: [batch_size, hidden_size]
# input_states?
            _encoder_outputs, _encoder_state = tf.nn.dynamic_rnn(
                _encoder_cell, _encoder_emb_inp, dtype=tf.float32,
                sequence_length=self._src_sequence_lengths, time_major=False)

        self._encoder_outputs = _encoder_outputs
        self._encoder_state = _encoder_state

    def _build_decoder(self):
        with tf.variable_scope("Decoder"):
            # Decoder embedding
            # _decoder_embedding = tf.get_variable(
            #    "decoder_embedding", [self.tgt_vocab_size + self.kb_size,
            #                          self.embedding_size])
            # _decoder_emb_inp = tf.one_hot(self._decoder_inputs,
            #                              self.tgt_vocab_size + self.kb_size)
            _decoder_emb_inp = tf.nn.embedding_lookup(self._decoder_embedding,
                                                      self._decoder_inputs)

            # Tiling outputs, states, sequence lengths
            _tiled_encoder_outputs = tf.contrib.seq2seq.tile_batch(
                self._encoder_outputs, multiplier=self.beam_width)
            _tiled_encoder_state = tf.contrib.seq2seq.tile_batch(
                self._encoder_state, multiplier=self.beam_width)
            _tiled_src_sequence_lengths = tf.contrib.seq2seq.tile_batch(
                self._src_sequence_lengths, multiplier=self.beam_width)

            if self.kb_size > 0:
                with tf.variable_scope("AttentionOverKB"):
                    _projection_layer = KBAttention(self.tgt_vocab_size,
                                                    self.kb_attn_hidden_sizes + [1],
                                                    self._kb_embedding,
                                                    self._kb_mask,
                                                    activation=tf.nn.relu,
                                                    use_bias=False)
            else:
                with tf.variable_scope("OutputDense"):
                    _projection_layer = tf.layers.Dense(self.tgt_vocab_size,
                                                        use_bias=False)

            # Decoder Cell
            _decoder_cell = tf.nn.rnn_cell.LSTMCell(self.hidden_size,
                                                    name='basic_lstm_cell')
            _decoder_cell = tf.contrib.rnn.DropoutWrapper(
                _decoder_cell,
                input_size=self.embedding_size + self.hidden_size,
                dtype=tf.float32,
                input_keep_prob=self._dropout_keep_prob,
                output_keep_prob=self._dropout_keep_prob,
                state_keep_prob=self._state_dropout_keep_prob,
                variational_recurrent=True)

            def build_dec_cell(enc_out, enc_seq_len, reuse=None):
                with tf.variable_scope("dec_cell_attn", reuse=reuse):
                    # Create an attention mechanism
                    # _attention_mechanism = tf.contrib.seq2seq.BahdanauAttention(
                    _attention_mechanism = tf.contrib.seq2seq.LuongAttention(
                        self.hidden_size,
                        memory=enc_out,
                        memory_sequence_length=enc_seq_len)
                    _cell = tf.contrib.seq2seq.AttentionWrapper(
                        _decoder_cell,
                        _attention_mechanism,
                        attention_layer_size=self.hidden_size)
                    return _cell

            # TRAIN MODE
            _decoder_cell_tr = build_dec_cell(self._encoder_outputs,
                                              self._src_sequence_lengths)
            self._decoder_cell_tr = _decoder_cell_tr
            # Train Helper to feed inputs for training:
            # read inputs from dense ground truth vectors
            _helper_tr = tf.contrib.seq2seq.TrainingHelper(
                _decoder_emb_inp, self._tgt_sequence_lengths, time_major=False)
            # Copy encoder hidden state to decoder inital state
            _decoder_init_state = \
                _decoder_cell_tr.zero_state(self._batch_size, dtype=tf.float32)\
                .clone(cell_state=self._encoder_state)
            _decoder_tr = \
                tf.contrib.seq2seq.BasicDecoder(_decoder_cell_tr, _helper_tr,
                                                initial_state=_decoder_init_state,
                                                output_layer=_projection_layer)
            # Wrap into variable scope to share attention parameters
            # Required!
            with tf.variable_scope('decode_with_shared_attention'):
                _outputs_inf, _, _ = \
                    tf.contrib.seq2seq.dynamic_decode(_decoder_tr,
                                                      impute_finished=False,
                                                      output_time_major=False)
            # _logits = decode(_helper, "decode").beam_search_decoder_output.scores
            _logits = _outputs_inf.rnn_output

            # INFER MODE
            _decoder_cell_inf = build_dec_cell(_tiled_encoder_outputs,
                                               _tiled_src_sequence_lengths,
                                               reuse=True)
            self._decoder_cell_inf = _decoder_cell_inf
            # Infer Helper
            _max_iters = tf.round(tf.reduce_max(self._src_sequence_lengths) * 2)
            # NOTE: helper is not needed?
            # _helper_inf = tf.contrib.seq2seq.GreedyEmbeddingHelper(
            #    self._decoder_embedding,
            #    tf.fill([self._batch_size], self.tgt_sos_id), self.tgt_eos_id)
            #    lambda d: tf.one_hot(d, self.tgt_vocab_size + self.kb_size),
            # Decoder Init State
            _decoder_init_state = \
                _decoder_cell_inf.zero_state(tf.shape(_tiled_encoder_outputs)[0],
                                             dtype=tf.float32)\
                .clone(cell_state=_tiled_encoder_state)
            # Define a beam-search decoder
            _start_tokens = tf.tile(tf.constant([self.tgt_sos_id], tf.int32),
                                    [self._batch_size])
            # _start_tokens = tf.fill([self._batch_size], self.tgt_sos_id)
            _decoder_inf = tf.contrib.seq2seq.BeamSearchDecoder(
                    cell=_decoder_cell_inf,
                    embedding=self._decoder_embedding,
                    start_tokens=_start_tokens,
                    end_token=self.tgt_eos_id,
                    initial_state=_decoder_init_state,
                    beam_width=self.beam_width,
                    output_layer=_projection_layer,
                    length_penalty_weight=0.0)

            # Wrap into variable scope to share attention parameters
            # Required!
            with tf.variable_scope("decode_with_shared_attention", reuse=True):
                # TODO: try impute_finished = True,
                _outputs_inf, _, _ = \
                    tf.contrib.seq2seq.dynamic_decode(_decoder_inf,
                                                      impute_finished=False,
                                                      maximum_iterations=_max_iters,
                                                      output_time_major=False)
            _predictions = _outputs_inf.predicted_ids[:, :, 0]
            # TODO: rm indexing
            # _predictions = \
            #    decode(_helper_infer, "decode", _max_iters, reuse=True).sample_id
        self._logits = _logits
        self._predictions = _predictions

    def __call__(self, enc_inputs, src_seq_lengths, kb_masks, prob=False):
        predictions = self.sess.run(
            self._predictions,
            feed_dict={
                self._dropout_keep_prob: 1.,
                self._state_dropout_keep_prob: 1.,
                self._encoder_inputs: enc_inputs,
                self._src_sequence_lengths: src_seq_lengths,
                self._kb_mask: kb_masks
            }
        )
# TODO: implement infer probabilities
        if prob:
            raise NotImplementedError("Probs not available for now.")
        return predictions

    def train_on_batch(self, enc_inputs, dec_inputs, dec_outputs,
                       src_seq_lengths, tgt_masks, kb_masks):
        _, loss_value = self.sess.run(
            [self._train_op, self._loss],
            feed_dict={
                self._dropout_keep_prob: 1 - self.dropout_rate,
                self._state_dropout_keep_prob: 1 - self.state_dropout_rate,
                self._encoder_inputs: enc_inputs,
                self._decoder_inputs: dec_inputs,
                self._decoder_outputs: dec_outputs,
                self._src_sequence_lengths: src_seq_lengths,
                self._tgt_mask: tgt_masks,
                self._kb_mask: kb_masks
            }
        )
        return loss_value

    def load(self, *args, **kwargs):
        self.load_params()
        super().load(*args, **kwargs)

    def load_params(self):
        path = str(self.load_path.with_suffix('.json').resolve())
        log.info('[loading parameters from {}]'.format(path))
        with open(path, 'r', encoding='utf8') as fp:
            params = json.load(fp)
        for p in self.GRAPH_PARAMS:
            if self.opt.get(p) != params.get(p):
                if p in ('kb_embedding_control_sum') and\
                        (math.abs(self.opt.get(p, 0.) - params.get(p, 0.)) < 1e-3):
                        continue
                raise ConfigError("`{}` parameter must be equal to saved model"
                                  " parameter value `{}`, but is equal to `{}`"
                                  .format(p, params.get(p), self.opt.get(p)))

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.save_params()

    def save_params(self):
        path = str(self.save_path.with_suffix('.json').resolve())
        log.info('[saving parameters to {}]'.format(path))
        with open(path, 'w', encoding='utf8') as fp:
            json.dump(self.opt, fp)

