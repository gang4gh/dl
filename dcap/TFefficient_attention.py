# MIT License

# Copyright (c) 2020 Streack, Jayakrishna Sahit

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import sys
import tensorflow as tf
from tensorflow.keras.layers import Dense
from TFutils import sort_key_val, batched_index_select, process_inputs_chunk

def debug_print(*args, **kwargs):
  #print(*args, **kwargs)
  pass

def print_tensor(tensor, summarize=16):
  import inspect
  frame = inspect.currentframe()
  frame = inspect.getouterframes(frame)[1]
  string = inspect.getframeinfo(frame[0]).code_context[0].strip()
  args = string[string.find('(') + 1:-1].split(',')
  tensor_name = args[0] if '[' not in args[0] else ', '.join(args[:next(idx for idx, arg in enumerate(args) if ']' in arg)+1])
  tf.print(f'{tensor_name} : shape {tensor.shape} = ', tensor, **({'summarize': summarize} if summarize else {}))


class TFLSHAttention():
    def __init__( self,
                  dropout = 0.,
                  bucket_size = 64,
                  n_hashes = 8,
                  causal = False,
                  allow_duplicate_attention = True,
                  drop_for_hash_rate = 0.0,
                  random_rotations_per_head = False):
        assert dropout >= 0 and dropout <= 1.0, 'dropout rates must be in [0, 1.0]'

        self.dropout = dropout

        self.causal = causal
        self.n_hashes = n_hashes
        self.bucket_size = bucket_size

        self._allow_duplicate_attention = allow_duplicate_attention
        self._random_rotations_per_head = random_rotations_per_head

    def hash_vectors(self, n_buckets, vecs, padding_mask=None):
        batch_size = tf.shape(vecs)[0]

        # See https://arxiv.org/pdf/1509.02897.pdf
        # Sample a different random rotation for each round of hashing to
        # decrease the probability of hash misses.
        assert n_buckets % 2 == 0

        rot_size = n_buckets

        rotations_shape = (
            batch_size if self._random_rotations_per_head else 1,
            vecs.shape[-1],
            self.n_hashes,
            rot_size // 2)
        debug_print('rotations_shape: ', rotations_shape)

        random_rotations = tf.broadcast_to(tf.random.normal(rotations_shape), (batch_size, vecs.shape[-1], self.n_hashes, rot_size // 2))

        rotated_vecs = tf.einsum('btf,bfhi->bhti', vecs, random_rotations)

        rotated_vecs = tf.concat([rotated_vecs, -rotated_vecs], axis=-1)
        if padding_mask is not None:
          lastbucket = rotated_vecs[:,:,:,-1:] + tf.cast(padding_mask[:,None,:,None], tf.float32) * 1e9
          rotated_vecs = tf.concat([rotated_vecs[:,:,:,:-1], lastbucket], axis=-1)
        buckets = tf.math.argmax(rotated_vecs, axis=-1)
        # buckets is now (batch_size, self.n_hashes, seqlen). Next we add offsets so that
        # bucket numbers from different hashing rounds don't overlap.
        offsets = tf.range(self.n_hashes)
        offsets = tf.reshape(offsets * n_buckets, (1, -1, 1))
        offsets = tf.cast(offsets, tf.int64)
        debug_print('offsets: ', offsets.shape)
        buckets = tf.reshape(buckets + offsets, (-1, self.n_hashes * n_buckets * self.bucket_size))

        return buckets

    def call(self, qk, v, padding_mask, num_hashes=None):
        if num_hashes:
          self.n_hashes = num_hashes

        _, seqlen, num_dims = qk.shape

        debug_print('qk.shape/v.shape: ', qk.shape, v.shape)
        assert padding_mask is None or seqlen == padding_mask.shape[1]

        n_buckets = seqlen // self.bucket_size
        n_bins = n_buckets

        buckets = self.hash_vectors(n_buckets, qk, padding_mask)
        debug_print('buckets: ', buckets.shape)
        #buckets_0 = tf.reshape(buckets[0], (self.n_hashes, seqlen))
        #debug_print('buckets[0]: ', buckets_0[:,0:2])
        #buckets_0_argsort = tf.gather(buckets_0, full_logits_argsort[:, :32], axis=-1)
        #debug_print(buckets_0_argsort[:,11,:])
        #debug_print(buckets_0_argsort[...,0,None] == buckets_0_argsort, 0)
        #debug_print(tf.reduce_any(buckets_0_argsort[...,0,None] == buckets_0_argsort, 0)[:,1][:64])
        #debug_print(tf.reduce_sum(tf.cast(tf.reduce_any(buckets_0_argsort[...,0,None] == buckets_0_argsort, 0)[:,1], tf.float32))/seqlen)
        #sys.exit(0)
        # We use the same vector as both a query and a key.
        assert int(buckets.shape[1]) == self.n_hashes * seqlen

        ticker = tf.expand_dims(tf.range(self.n_hashes * seqlen), axis=0)
        buckets_and_t = seqlen * buckets + tf.cast((ticker % seqlen), tf.int64)
        buckets_and_t = tf.stop_gradient(buckets_and_t)
        debug_print('buckets_and_t.shape: ', buckets_and_t.shape)

        # Hash-based sort ("s" at the start of variable names means "sorted")
        ticker = tf.reshape(ticker, (-1,))
        sbuckets_and_t, sticker = sort_key_val(buckets_and_t, ticker, dim=-1)
        _, undo_sort = sort_key_val(sticker, ticker, dim=-1)
        debug_print('ticker.shape: ', ticker.shape)
        debug_print('sbuckets_and_t.shape: ', sbuckets_and_t.shape)
        debug_print('sticker.shape: ', sticker.shape)
        debug_print('undo_sort.shape: ', undo_sort.shape)
        del ticker

        sbuckets_and_t = tf.stop_gradient(sbuckets_and_t)
        sticker = tf.stop_gradient(sticker)
        undo_sort = tf.stop_gradient(undo_sort)

        st = (sticker % seqlen)
        debug_print('st.shape: ', st.shape)
        sqk = batched_index_select(qk, st)
        sv = batched_index_select(v, st)
        debug_print('sqk.shape: ', sqk.shape)
        debug_print('sv.shape: ', sv.shape)

        # Split off a "bin" axis so that attention only occurs within chunks.
        bq_t = bkv_t = tf.reshape(st, (-1, self.n_hashes * n_bins, self.bucket_size))
        bqk = tf.reshape(sqk, (-1, self.n_hashes * n_bins, self.bucket_size, num_dims))
        bv = tf.reshape(sv, (-1, self.n_hashes * n_bins, self.bucket_size, num_dims))
        debug_print('bq_t.shape: ', bq_t.shape)
        debug_print('bqk.shape: ', bqk.shape)
        debug_print('bv.shape: ', bv.shape)

        # Hashing operates on unit-length vectors. Unnormalized query vectors are
        # fine because they effectively provide a learnable temperature for the
        # attention softmax, but normalizing keys is needed so that similarity for
        # the purposes of attention correctly corresponds to hash locality.
        bq = bqk
        bk = tf.math.l2_normalize(bqk, -1)
        debug_print('bq.shape: ', bq.shape)
        debug_print('bk.shape: ', bk.shape)

        # Allow each chunk to attend within itself, and also one chunk back. Chunk
        # boundaries might occur in the middle of a sequence of items from the
        # same bucket, so this increases the chances of attending to relevant items.
        def look_one_back(x):
            # actually this is a rotation
            #x_extra = tf.concat([x[:, -1:, ...], x[:, :-1, ...]], axis=1)
            if len(x.shape) == 3:
              x1 = tf.reshape(x, [-1, self.n_hashes, n_bins, x.shape[2]])
            else:
              x1 = tf.reshape(x, [-1, self.n_hashes, n_bins, x.shape[2], x.shape[3]])

            x_extra = tf.reshape(tf.concat([x1[:, :, 1:2, ...], x1[:, :, :-1, ...]], axis=2), tf.shape(x))
            return tf.concat([x, x_extra], axis=2)

        bk = look_one_back(bk)
        bv = look_one_back(bv)
        bkv_t = look_one_back(bkv_t)
        debug_print('apply look_one_back')
        debug_print('bk.shape', bk.shape)
        debug_print('bv.shape', bv.shape)
        debug_print('bkv_t.shape', bkv_t.shape)

        # Dot-product attention.
        dots = tf.einsum('bhie,bhje->bhij', bq * (bq.shape[-1] ** -0.5), bk)
        debug_print('dots.shape', dots.shape)

        # padding masking
        if padding_mask is not None:
            # padding length based mask, Url segment (64 tokens), Hostname segment (64 tokens)
            #mask = bkv_t[:, :, None, :] >= seqlen - tf.reduce_sum(tf.cast(padding_mask[:, 128:], tf.int32), axis=-1)[:,None,None,None]
            #mask = tf.where(bkv_t[:, :, None, :] < 128
            #           , tf.where(bkv_t[:, :, None, :] < 64
            #               , bkv_t[:, :, None, :] >= 64 - tf.reduce_sum(tf.cast(padding_mask[:, :64], tf.int32), axis=-1)[:,None,None,None]
            #               , bkv_t[:, :, None, :] >= 128 - tf.reduce_sum(tf.cast(padding_mask[:, 64:128], tf.int32), axis=-1)[:,None,None,None])
            #           , bkv_t[:, :, None, :] >= seqlen - tf.reduce_sum(tf.cast(padding_mask[:, 128:], tf.int32), axis=-1)[:,None,None,None]
            #           )

            # padding position based mask
            mask = tf.cast(tf.gather(padding_mask, bkv_t, batch_dims=1)[:,:,None,:], tf.float32)
            #tf.debugging.assert_equal(mask, mask2, message='two masks are diff')

            dots = tf.math.multiply(dots, (1-mask)) + mask * (-1e9)
            #dots += mask * (-1e9)
            debug_print('********** apply padding mask, mask.shape', mask.shape)
            del mask

        # Causal masking
        if self.causal:
            mask = bq_t[:, :, :, None] >= bkv_t[:, :, None, :]
            debug_print('********** apply causal mask: causal/mask.shape', mask.shape)
            dots = tf.math.multiply(dots, tf.cast(mask, tf.float32)) + (1-tf.cast(mask, tf.float32)) * (- 1e9)
            del mask

        # Mask out attention to self except when no other targets are available.
        self_mask = tf.cast(bq_t[:, :, :, None] == bkv_t[:, :, None, :], tf.float32)
        dots = tf.math.multiply(dots, (1-self_mask)) + self_mask * (-1e5)
        #dots += self_mask * (-1e5)
        del self_mask

        # Don't double-count query-key pairs across multiple rounds of hashing.
        # Here to count how many times a query-key pair is repeated,
        # and to lower its log-prob correspondingly at each repetition.
        if not self._allow_duplicate_attention:
            # TODO: not efficient and contains bugs, re-implement it or disable this option
            locs1 = undo_sort // bq_t.shape[-1]
            locs2 = (locs1 + 1) % (self.n_hashes * n_bins)
            locs = tf.transpose(tf.concat([ tf.reshape(locs1, (-1, self.n_hashes, seqlen)), tf.reshape(locs2, (-1, self.n_hashes, seqlen)), ], 1), perm=[0, 2, 1])
            slocs = batched_index_select(locs, st)
            b_locs = tf.reshape(slocs, (-1, self.n_hashes * n_bins, self.bucket_size, 2 * self.n_hashes))
            b_locs1 = b_locs[:, :, :, None, :self.n_hashes]

            bq_locs = tf.broadcast_to(b_locs1, b_locs.shape[:3] + (2, self.n_hashes))
            bq_locs = tf.reshape(bq_locs, b_locs.shape)
            bkv_locs = look_one_back(b_locs)

            dup_counts = tf.math.reduce_sum(tf.cast(bq_locs[:, :, :, None, :] == bkv_locs[:, :, None, :, :], tf.float32), -1)
            dup_counts = tf.stop_gradient(dup_counts)

            assert dup_counts.shape == dots.shape
            #dots = dots - tf.math.log(dup_counts + 1e-9) # doesn't work as dup_counts contains 0
            dots = tf.where(dup_counts <= 1, dots, dots - tf.math.log(dup_counts))
            del dup_counts

        debug_print('dots.shape (after discount)', dots.shape)

        # Softmax.
        logits_in_buckets = dots
        dots_logsumexp = tf.math.reduce_logsumexp(dots, axis=-1, keepdims=True)
        debug_print('dots_logsumexp.shape: ', dots_logsumexp.shape)
        dots = tf.exp(dots - dots_logsumexp) # weights matrix after softmax
        if self.dropout:
          dots = tf.nn.dropout(dots, rate=dropout)
 
        bo = tf.einsum('buij,buje->buie', dots, bv)
        debug_print('bo.shape', bo.shape)
        so = tf.reshape(bo, (-1, self.n_hashes * seqlen, num_dims))
        slogits = tf.reshape(dots_logsumexp, (-1, self.n_hashes * seqlen,))
        debug_print('so.shape', so.shape)
        debug_print('slogits.shape', slogits.shape)

#        class UnsortLogits(tf.keras.layers.Layer):
#            def __init__(self):
#                super(UnsortLogits, self).__init__()
#
#            def call(self, so, slogits):
#                so, slogits = tf.stop_gradient(so), tf.stop_gradient(slogits)
#                o = batched_index_select(so, undo_sort)
#                _, logits = sort_key_val(sticker, slogits, dim=-1)
#                #logits2 = batched_index_select(slogits, undo_sort)
#                #debug_print(tf.reduce_sum(tf.cast(logits2 == logits, tf.float32)))
#                return o, logits
#        unsortlogits = UnsortLogits()
#        o, logits = unsortlogits(so, slogits)

        # TODO: undrestand the mechanism of custom_gradient
        @tf.custom_gradient
        def unsort_output(so, slogits):
          """Custom gradient for unsort_output."""
          so = tf.stop_gradient(so)
          slogits = tf.stop_gradient(slogits)

          o = batched_index_select(so, undo_sort)
          _, logits = sort_key_val(sticker, slogits, dim=-1)
          #logits2 = batched_index_select(slogits, undo_sort)
          #debug_print(tf.reduce_sum(tf.cast(logits2 == logits, tf.float32)))

          def unsort_output_grad(*grads):
            so_grad = batched_index_select(grads[0], sticker)
            _, slogits_grad = sort_key_val(buckets_and_t, grads[1], dim=-1)
            return so_grad, slogits_grad
          return (o, logits), unsort_output_grad
        o, logits = unsort_output(so, slogits)
        debug_print('o.shape', o.shape)
        debug_print('logits.shape', logits.shape)

        if self.n_hashes == 1:
            out = o
            debug_print('output o since n_hashes == 1')
        else:
            o = tf.reshape(o, (-1, self.n_hashes, seqlen, num_dims))
            logits = tf.reshape(logits, (-1, self.n_hashes, seqlen, 1))
            probs = tf.exp(logits - tf.math.reduce_logsumexp(logits, axis=1, keepdims=True))
            debug_print('o.shape (modified): ', o.shape)
            debug_print('logits.shape (modified): ', logits.shape)
            debug_print('probs.shape: ', probs.shape)
            debug_print('calc.shape ', (o*probs).shape)
            out = tf.reduce_sum(o * probs, axis=1)

        assert out.shape[1:] == v.shape[1:]
        #return out, buckets
        return out

class TFLSHSelfAttention(tf.keras.Model):
    def __init__(self, emb, heads = 8, bucket_size = 64, n_hashes = 8, causal = False, attn_chunks = None, random_rotations_per_head = False, allow_duplicate_attention = True, **kwargs):
        super(TFLSHSelfAttention, self).__init__()
        assert emb % heads == 0, 'dimensions must be divisible by number of heads'

        self.emb = emb
        self.heads = heads
        self.attn_chunks = heads if attn_chunks is None else attn_chunks

        self.toqk = Dense(emb, use_bias = False)
        self.tov = Dense(emb, use_bias = False)
        self.to_out = Dense(emb)

        self.bucket_size = bucket_size
        self.lsh_attn = TFLSHAttention(bucket_size=bucket_size, causal=causal, random_rotations_per_head=random_rotations_per_head, allow_duplicate_attention = allow_duplicate_attention, **kwargs)

    def call(self, inputs):
        b, t, e, h = *inputs.shape, self.heads
        assert t % self.bucket_size == 0, f'Sequence length needs to be divisible by target bucket size - {self.bucket_size}'

        qk = self.toqk(inputs)
        v = self.tov(inputs)

        def merge_heads(v):
            return tf.reshape(tf.transpose(tf.reshape(v, (b, t, h, -1)), perm=[0, 2, 1, 3]), (b * h, t, -1)) 

        def split_heads(v):
            return tf.transpose(tf.reshape(v, (b, t, h, -1)), perm=[0, 2, 1, 3])

        qk = merge_heads(qk)
        v = merge_heads(v)

        outputs = process_inputs_chunk(self.lsh_attn, qk, v, chunks=self.attn_chunks)
        attn_out = tf.concat([output for (output, _) in outputs], axis=0)

        out = tf.reshape(split_heads(attn_out), (b, t, e))

        return self.to_out(out)
