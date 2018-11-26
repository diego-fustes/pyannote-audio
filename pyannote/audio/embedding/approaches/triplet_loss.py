#!/usr/bin/env python
# encoding: utf-8

# The MIT License (MIT)

# Copyright (c) 2018 CNRS

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# AUTHORS
# Hervé BREDIN - http://herve.niderb.fr

import numpy as np
import torch
import torch.nn.functional as F
from pyannote.audio.embedding.generators import SpeechSegmentGenerator
from pyannote.audio.embedding.generators import SessionWiseSpeechSegmentGenerator
from pyannote.audio.embedding.generators import UnsupervisedSpeechSegmentGenerator

from pyannote.core.utils.distance import to_condensed
from pyannote.core.utils.distance import pdist
from scipy.spatial.distance import squareform
from pyannote.metrics.binary_classification import det_curve
from collections import deque
from pyannote.audio.train.trainer import Trainer


class TripletLoss(Trainer):
    """

    delta = d(anchor, positive) - d(anchor, negative)

    * with 'positive' clamping:
        loss = max(0, delta + margin x D)
    * with 'sigmoid' clamping:
        loss = sigmoid(10 * delta)

    where d(., .) varies in range [0, D] (e.g. D=2 for euclidean distance).

    Parameters
    ----------
    duration : float, optional
        Use fixed duration segments with this `duration`.
        Defaults (None) to using variable duration segments.
    min_duration : float, optional
        In case `duration` is None, set segment minimum duration.
    max_duration : float, optional
        In case `duration` is None, set segment maximum duration.
    metric : {'euclidean', 'cosine', 'angular'}, optional
        Defaults to 'cosine'.
    margin: float, optional
        Margin multiplicative factor. Defaults to 0.2.
    clamp : {'positive', 'sigmoid', 'softmargin'}, optional
        Defaults to 'positive'.
    sampling : {'all', 'hard', 'negative', 'easy'}, optional
        Triplet sampling strategy. Defaults to 'all' (i.e. use all possible
        triplets). 'hard' sampling use both hardest positive and negative for
        each anchor. 'negative' sampling use hardest negative for each
        (anchor, positive) pairs. 'easy' sampling only use easy triplets (i.e.
        those for which d(anchor, positive) < d(anchor, negative)).
    per_label : int, optional
        Number of sequences per speaker in each batch. Defaults to 3.
    label_min_duration : float, optional
        Remove speakers with less than `label_min_duration` seconds of speech.
        Defaults to 0 (i.e. keep them all).
    per_fold : int, optional
        If provided, sample triplets from groups of `per_fold` speakers at a
        time. Defaults to sample triplets from the whole speaker set.
    per_epoch : float, optional
        Number of days per epoch. Defaults to 7 (a week).
    variant : {'corpus', 'session', 'unsupervised'}, optional
        Change the way triplets are built. Default behavior ('corpus') is to
        build triplets by sampling sequences from the whole corpus (assuming
        that labels are consistent on the whole corpus). Therefore, triplets
        may contain sequences from different sessions in the corpus. When
        `variant` is 'session', we only assume labels are consistent within
        each 'session'. Therefore, triplets are always made of sequences
        extracted from the same session. When `variant` is 'unsupervised',
        anchor and positive sequences are always extracted from the same
        speech turn, and the negative sequence is sampled at random from any
        other speech turns (whatever its label).
    parallel : int, optional
        Number of prefetching background generators. Defaults to 1.
        Each generator will prefetch enough batches to cover a whole epoch.
        Set `parallel` to 0 to not use background generators.
    """

    def __init__(self, duration=None, min_duration=None, max_duration=None,
                 metric='cosine', margin=0.2, clamp='positive',
                 sampling='all', per_label=3, per_fold=None, per_epoch=7,
                 parallel=1, variant='corpus', label_min_duration=0.):

        super(TripletLoss, self).__init__()

        self.metric = metric
        self.margin = margin

        self.margin_ = self.margin * self.max_distance

        if clamp not in {'positive', 'sigmoid', 'softmargin'}:
            msg = "'clamp' must be one of {'positive', 'sigmoid', 'softmargin'}."
            raise ValueError(msg)
        self.clamp = clamp

        if sampling not in {'all', 'hard', 'negative', 'easy'}:
            msg = "'sampling' must be one of {'all', 'hard', 'negative', 'easy'}."
            raise ValueError(msg)
        self.sampling = sampling

        self.per_fold = per_fold
        self.per_label = per_label
        self.per_epoch = per_epoch
        self.label_min_duration = label_min_duration

        if variant not in {'corpus', 'session', 'unsupervised'}:
            msg = "'variant' must be one of {'corpus', 'session', 'unsupervised'}."
            raise ValueError(msg)
        self.variant = variant

        self.duration = duration
        self.min_duration = min_duration
        self.max_duration = max_duration

        self.parallel = parallel

    @property
    def max_distance(self):
        if self.metric == 'cosine':
            return 2.
        elif self.metric == 'angular':
            return np.pi
        elif self.metric == 'euclidean':
            # FIXME. incorrect if embedding are not unit-normalized
            return 2.
        else:
            msg = "'metric' must be one of {'euclidean', 'cosine', 'angular'}."
            raise ValueError(msg)

    def pdist(self, fX):
        """Compute pdist à-la scipy.spatial.distance.pdist

        Parameters
        ----------
        fX : (n, d) torch.Tensor
            Embeddings.

        Returns
        -------
        distances : (n * (n-1) / 2,) torch.Tensor
            Condensed pairwise distance matrix
        """

        n_sequences, _ = fX.size()
        distances = []

        for i in range(n_sequences - 1):

            if self.metric in ('cosine', 'angular'):
                d = 1. - F.cosine_similarity(
                    fX[i, :].expand(n_sequences - 1 - i, -1),
                    fX[i+1:, :], dim=1, eps=1e-8)

                if self.metric == 'angular':
                    d = torch.acos(torch.clamp(1. - d, -1 + 1e-6, 1 - 1e-6))

            elif self.metric == 'euclidean':
                d = F.pairwise_distance(
                    fX[i, :].expand(n_sequences - 1 - i, -1),
                    fX[i+1:, :], p=2, eps=1e-06).view(-1)

            distances.append(d)

        return torch.cat(distances)


    def batch_easy(self, y, distances):
        """Build easy triplets"""

        anchors, positives, negatives = [], [], []

        distances = squareform(self.to_numpy(distances))

        for anchor, y_anchor in enumerate(y):
            for positive, y_positive in enumerate(y):

                # if same embedding or different labels, skip
                if (anchor == positive) or (y_anchor != y_positive):
                    continue

                d = distances[anchor, positive]

                for negative, y_negative in enumerate(y):

                    if y_negative == y_anchor:
                        continue

                    if d > distances[anchor, negative]:
                        continue

                    anchors.append(anchor)
                    positives.append(positive)
                    negatives.append(negative)

        return anchors, positives, negatives


    def batch_hard(self, y, distances):
        """Build triplet with both hardest positive and hardest negative

        Parameters
        ----------
        y : list
            Sequence labels.
        distances : (n * (n-1) / 2,) torch.Tensor
            Condensed pairwise distance matrix

        Returns
        -------
        anchors, positives, negatives : list of int
            Triplets indices.
        """

        anchors, positives, negatives = [], [], []

        distances = squareform(self.to_numpy(distances))
        y = np.array(y)

        for anchor, y_anchor in enumerate(y):

            d = distances[anchor]

            # hardest positive
            pos = np.where(y == y_anchor)[0]
            pos = [p for p in pos if p != anchor]
            positive = int(pos[np.argmax(d[pos])])

            # hardest negative
            neg = np.where(y != y_anchor)[0]
            negative = int(neg[np.argmin(d[neg])])

            anchors.append(anchor)
            positives.append(positive)
            negatives.append(negative)

        return anchors, positives, negatives

    def batch_negative(self, y, distances):
        """Build triplet with hardest negative

        Parameters
        ----------
        y : list
            Sequence labels.
        distances : (n * (n-1) / 2,) torch.Tensor
            Condensed pairwise distance matrix

        Returns
        -------
        anchors, positives, negatives : list of int
            Triplets indices.
        """

        anchors, positives, negatives = [], [], []

        distances = squareform(self.to_numpy(distances))
        y = np.array(y)

        for anchor, y_anchor in enumerate(y):

            # hardest negative
            d = distances[anchor]
            neg = np.where(y != y_anchor)[0]
            negative = int(neg[np.argmin(d[neg])])

            for positive in np.where(y == y_anchor)[0]:
                if positive == anchor:
                    continue

                anchors.append(anchor)
                positives.append(positive)
                negatives.append(negative)

        return anchors, positives, negatives

    def batch_all(self, y, distances):
        """Build all possible triplet

        Parameters
        ----------
        y : list
            Sequence labels.
        distances : (n * (n-1) / 2,) torch.Tensor
            Condensed pairwise distance matrix

        Returns
        -------
        anchors, positives, negatives : list of int
            Triplets indices.
        """

        anchors, positives, negatives = [], [], []

        for anchor, y_anchor in enumerate(y):
            for positive, y_positive in enumerate(y):

                # if same embedding or different labels, skip
                if (anchor == positive) or (y_anchor != y_positive):
                    continue

                for negative, y_negative in enumerate(y):

                    if y_negative == y_anchor:
                        continue

                    anchors.append(anchor)
                    positives.append(positive)
                    negatives.append(negative)

        return anchors, positives, negatives

    def triplet_loss(self, distances, anchors, positives, negatives,
                     return_delta=False):
        """Compute triplet loss

        Parameters
        ----------
        distances : torch.Tensor
            Condensed matrix of pairwise distances.
        anchors, positives, negatives : list of int
            Triplets indices.
        return_delta : bool, optional
            Return delta before clamping.

        Returns
        -------
        loss : torch.Tensor
            Triplet loss.
        """

        # estimate total number of embeddings from pdist shape
        n = int(.5 * (1 + np.sqrt(1 + 8 * len(distances))))

        # convert indices from squared matrix
        # to condensed matrix referential
        pos = to_condensed(n, anchors, positives)
        neg = to_condensed(n, anchors, negatives)

        # compute raw triplet loss (no margin, no clamping)
        # the lower, the better
        delta = distances[pos] - distances[neg]

        # clamp triplet loss
        if self.clamp == 'positive':
            loss = torch.clamp(delta + self.margin_, min=0)

        elif self.clamp == 'softmargin':
            loss = torch.log1p(torch.exp(delta))

        elif self.clamp == 'sigmoid':
            # TODO. tune this "10" hyperparameter
            # TODO. log-sigmoid
            loss = F.sigmoid(10 * (delta + self.margin_))

        # return triplet losses
        if return_delta:
            return loss, delta.view((-1, 1)), pos, neg
        else:
            return loss

    def get_batch_generator(self, feature_extraction):
        """Get batch generator

        Parameters
        ----------
        feature_extraction : `pyannote.audio.features.FeatureExtraction`

        Returns
        -------
        generator : `pyannote.audio.embedding.generators.SpeechSegmentGenerator`

        """

        if self.variant == 'session':

            return SessionWiseSpeechSegmentGenerator(
                feature_extraction, label_min_duration=self.label_min_duration,
                per_label=self.per_label, per_fold=self.per_fold,
                per_epoch=self.per_epoch, duration=self.duration,
                min_duration=self.min_duration, max_duration=self.max_duration,
                parallel=self.parallel)

        elif self.variant == 'corpus':

            return SpeechSegmentGenerator(
                feature_extraction, label_min_duration=self.label_min_duration,
                per_label=self.per_label, per_fold=self.per_fold,
                per_epoch=self.per_epoch, duration=self.duration,
                min_duration=self.min_duration, max_duration=self.max_duration,
                parallel=self.parallel)

        elif self.variant == 'unsupervised':

            return UnsupervisedSpeechSegmentGenerator(
                feature_extraction, per_fold=self.per_fold,
                per_epoch=self.per_epoch, duration=self.duration,
                parallel=self.parallel)

    def aggregate(self, batch):
        return batch

    def on_train_start(self, model, batches_per_epoch=None, **kwargs):
        self.log_positive_ = deque([], maxlen=batches_per_epoch)
        self.log_negative_ = deque([], maxlen=batches_per_epoch)
        self.log_delta_ = deque([], maxlen=batches_per_epoch)
        self.log_norm_ = deque([], maxlen=batches_per_epoch)

    def batch_loss(self, batch, model, device, writer=None):
        """Compute loss for current `batch`

        Parameters
        ----------
        batch : `dict`
            ['X'] (`numpy.ndarray`)
            ['y'] (`numpy.ndarray`)
        model : `torch.nn.Module`
            Model currently being trained.
        device : `torch.device`
            Device used by model parameters.
        writer : `tensorboardX.SummaryWriter`, optional
            Tensorboard writer.

        Returns
        -------
        loss : `torch.Tensor`
            Triplet loss.
        """

        fX = self.forward(batch, model, device)

        # log embedding norms
        if writer is not None:
            norm_npy = np.linalg.norm(self.to_numpy(fX), axis=1)
            self.log_norm_.append(norm_npy)

        batch['fX'] = fX
        batch = self.aggregate(batch)

        fX = batch['fX']
        y = batch['y']

        # pre-compute pairwise distances
        distances = self.pdist(fX)

        # sample triplets
        triplets = getattr(self, 'batch_{0}'.format(self.sampling))
        anchors, positives, negatives = triplets(y, distances)

        # compute loss for each triplet
        losses, deltas, _, _ = self.triplet_loss(
            distances, anchors, positives, negatives,
            return_delta=True)

        if writer is not None:
            pdist_npy = self.to_numpy(distances)
            delta_npy = self.to_numpy(deltas)
            same_speaker = pdist(y.reshape((-1, 1)), metric='equal')
            self.log_positive_.append(pdist_npy[np.where(same_speaker)])
            self.log_negative_.append(pdist_npy[np.where(~same_speaker)])
            self.log_delta_.append(delta_npy)

        # average over all triplets
        return torch.mean(losses)

    def on_epoch_end(self, iteration, checkpoint, writer=None, **kwargs):
        """Log a bunch of statistics at the end of current epoch

        Parameters
        ----------
        iteration : `int`
            Current epoch.
        checkpoint : `pyannote.audio.train.checkpoint.Checkpoint`
            Checkpoint.
        writer : `tensorboardX.SummaryWriter`, optional
            Tensorboard writer.
        """

        if writer is None:
            return

        # log intra class vs. inter class distance distributions
        log_positive = np.hstack(self.log_positive_)
        log_negative = np.hstack(self.log_negative_)
        writer.add_histogram(
            'train/distance/intra_class', log_positive,
            global_step=iteration, bins='doane')
        writer.add_histogram(
            'train/distance/inter_class', log_negative,
            global_step=iteration, bins='doane')

        # log same/different experiment on training samples
        _, _, _, eer = det_curve(
            np.hstack([np.ones(len(log_positive)),
                       np.zeros(len(log_negative))]),
            np.hstack([log_positive, log_negative]),
            distances=True)
        writer.add_scalar('train/eer', eer,
                          global_step=iteration)

        # log raw triplet loss (before max(0, .))
        log_delta = np.vstack(self.log_delta_)
        writer.add_histogram(
            'train/triplet/delta', log_delta,
            global_step=iteration, bins='doane')

        # log distribution of embedding norms
        log_norm = np.hstack(self.log_norm_)
        writer.add_histogram(
            'train/embedding/norm', log_norm,
            global_step=iteration, bins='doane')
