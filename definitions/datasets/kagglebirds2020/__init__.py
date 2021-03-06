# -*- coding: utf-8 -*-

"""
Kaggle birdcall recognition dataset.

Author: Jan Schlüter
"""

import os
import re
import glob
import itertools

import numpy as np
import pandas as pd
import tqdm

from ... import config
from .. import Dataset, ClassWeightedRandomSampler
from .. import audio
from .. import splitting


def common_shape(arrays):
    """
    Infers the common shape of an iterable of array-likes (assuming all are of
    the same dimensionality). Inconsistent dimensions are replaced with `None`.
    """
    arrays = iter(arrays)
    shape = next(arrays).shape
    for array in arrays:
        shape = tuple(a if a == b else None
                      for a, b in zip(shape, array.shape))
    return shape


class BirdcallDataset(Dataset):
    def __init__(self, itemids, wavs, labelset, annotations=None):
        shapes = dict(input=common_shape(wavs), itemid=())
        dtypes = dict(input=wavs[0].dtype, itemid=str)
        num_classes = len(labelset)
        if annotations is not None:
            if 'label_fg' in annotations:
                shapes['label_fg'] = ()
                dtypes['label_fg'] = np.uint8
            if 'label_bg' in annotations:
                shapes['label_bg'] = (num_classes,)
                dtypes['label_bg'] = np.uint8
            if 'label_all' in annotations:
                shapes['label_all'] = (num_classes,)
                dtypes['label_all'] = np.float32
            if 'rating' in annotations:
                shapes['rating'] = ()
                dtypes['rating'] = np.float32
        super(BirdcallDataset, self).__init__(
            shapes=shapes,
            dtypes=dtypes,
            num_classes=num_classes,
            num_items=len(itemids),
        )
        self.itemids = itemids
        self.wavs = wavs
        self.labelset = labelset
        self.annotations = annotations

    def __getitem__(self, idx):
        # get audio
        item = dict(itemid=self.itemids[idx], input=self.wavs[idx])
        # get targets, if any
        for key in self.shapes:
            if key not in item:
                item[key] = self.annotations[key][idx]
        # return
        return item


class NoiseDataset(Dataset):
    """
    Dataset of noise segments given as a list of `wavs` and an optional
    list of `(start, length)` segments for each wave file.
    """
    def __init__(self, wavs, segments_per_wav=None, min_length=0):
        shapes = dict(input=common_shape(wavs))
        dtypes = dict(input=wavs[0].dtype)
        if segments_per_wav is None:
            segments = [(wav, 0, len(wav)) for wav in wavs]
        else:
            segments = [(wav, start, length)
                        for wav, segments in zip(wavs, segments_per_wav)
                        for start, length in segments
                        if length >= min_length]
        super(NoiseDataset, self).__init__(shapes=shapes, dtypes=dtypes,
                                           num_classes=0,
                                           num_items=len(segments))
        self.segments = segments

    def __getitem__(self, idx):
        wav, start, length = self.segments[idx]
        return dict(input=wav[start:start + length])


def loop(array, length):
    """
    Loops a given `array` along its first axis to reach a length of `length`.
    """
    if len(array) < length:
        array = np.asanyarray(array)
        if len(array) == 0:
            return np.zeros((length,) + array.shape[1:], dtype=array.dtype)
        factor = length // len(array)
        if factor > 1:
            array = np.tile(array, (factor,) + (1,) * (array.ndim - 1))
        missing = length - len(array)
        if missing:
            array = np.concatenate((array, array[:missing:]))
    return array


def crop(array, length, deterministic=False):
    """
    Crops a random excerpt of `length` along the first axis of `array`. If
    `deterministic`, perform a center crop instead.
    """
    if len(array) > length:
        if not deterministic:
            pos = np.random.randint(len(array) - length + 1)
            array = array[pos:pos + length:]
        else:
            l = len(array)
            array = array[(l - length) // 2:(l + length) // 2]
    return array


class FixedSizeExcerpts(Dataset):
    """
    Dataset wrapper that returns batches of random excerpts of the same length,
    cropping or looping inputs along the first axis as needed. If
    `deterministic`, will always do a center crop for too long inputs.
    """
    def __init__(self, dataset, length, deterministic=False, key='input'):
        shapes = dict(dataset.shapes)
        shapes[key] = (length,) + shapes[key][1:]
        super(FixedSizeExcerpts, self).__init__(
                shapes=shapes, dtypes=dataset.dtypes,
                num_classes=dataset.num_classes, num_items=len(dataset))
        self.dataset = dataset
        self.length = length
        self.deterministic = deterministic
        self.key = key

    def __getattr__(self, attr):
        return getattr(self.dataset, attr)

    def __getitem__(self, idx):
        item = dict(self.dataset[idx])
        data = item[self.key]
        if len(data) < self.length:
            data = loop(data, self.length)
        elif len(data) > self.length:
            data = crop(data, self.length, deterministic=self.deterministic)
        item[self.key] = data
        return item


class Floatify(Dataset):
    """
    Dataset wrapper that converts audio samples to float32 with proper scaling,
    possibly transposing the data on the way to swap time and channels.
    """
    def __init__(self, dataset, transpose=False, key='input'):
        dtypes = dict(dataset.dtypes)
        dtypes[key] = np.float32
        shapes = dict(dataset.shapes)
        if transpose:
            shapes[key] = shapes[key][::-1]
        super(Floatify, self).__init__(
                shapes=shapes, dtypes=dtypes,
                num_classes=dataset.num_classes, num_items=len(dataset))
        self.dataset = dataset
        self.transpose = transpose
        self.key = key

    def __getattr__(self, attr):
        return getattr(self.dataset, attr)

    def __getitem__(self, idx):
        item = dict(self.dataset[idx])
        data = item[self.key]
        if self.transpose:
            data = np.asanyarray(data).T
        item[self.key] = audio.to_float(data)
        return item


class MixBackgroundNoise(Dataset):
    """
    Dataset wrapper that mixes in background noise from another dataset, with
    a given `probability`, and with a uniformly random amount between
    `min_factor` and `max_factor`. Optionally the noise is normalized and
    scaled with a uniformly random value between `min_amp` and `max_amp`.
    Keeping `max_amp=0` disables normalization and scaling. With a given
    `noise_only_probability`, replaces the input with only noise, and replaces
    the labels in `label_keys` with zeros.
    """
    def __init__(self, dataset, noisedataset, key='input', probability=1,
                 min_factor=0, max_factor=0.5, min_amp=0, max_amp=0,
                 noise_only_probability=0, label_keys=()):
        super(MixBackgroundNoise, self).__init__(shapes=dataset.shapes,
                                                 dtypes=dataset.dtypes,
                                                 num_classes=dataset.num_classes,
                                                 num_items=len(dataset))
        self.dataset = dataset
        self.noisedataset = noisedataset
        self.key = key
        self.probability = probability
        self.min_factor = min_factor
        self.max_factor = max_factor
        self.min_amp = min_amp
        self.max_amp = max_amp
        self.noise_only_probability = noise_only_probability
        self.label_keys = set(label_keys)

    def __getattr__(self, attr):
        return getattr(self.dataset, attr)

    def get_noise(self):
        # sample noise dataset
        noise = self.noisedataset[np.random.randint(len(self.noisedataset))]
        # get out the sound
        wav = noise[self.key]
        # normalize and scale, if needed
        if self.max_amp != 0:
            if self.min_amp != self.max_amp:
                factor = (self.min_amp + np.random.rand() *
                          (self.max_amp - self.min_amp))
            else:
                factor = self.min_amp
            wav = np.asanyarray(wav)
            max_amplitude = abs(wav).max() or 1
            wav = wav * (factor / max_amplitude)
        return wav

    def __getitem__(self, idx):
        item = dict(self.dataset[idx])
        if (self.noise_only_probability > 0 and
                np.random.rand() < self.noise_only_probability):
            item[self.key] = self.get_noise()
            for k in self.label_keys:
                item[k] = np.zeros_like(item[k])
        elif self.probability > 0 and (self.probability == 1 or
                                     np.random.rand() < self.probability):
            noise_factor = (self.min_factor + np.random.rand() *
                            (self.max_factor - self.min_factor))
            item[self.key] = ((1 - noise_factor) * item[self.key] +
                         noise_factor * self.get_noise())
        return item


class DownmixChannels(Dataset):
    """
    Dataset wrapper that downmixes multichannel audio to mono, either
    deterministically (method='average') or randomly (method='random_uniform').
    """
    def __init__(self, dataset, key='input', axis=0, method='average'):
        shapes = dict(dataset.shapes)
        shape = list(shapes[key])
        shape[axis] = 1
        shapes[key] = tuple(shape)
        super(DownmixChannels, self).__init__(
                shapes=shapes, dtypes=dataset.dtypes,
                num_classes=dataset.num_classes, num_items=len(dataset))
        self.dataset = dataset
        self.key = key
        self.axis = axis
        self.method = method

    def __getattr__(self, attr):
        return getattr(self.dataset, attr)

    def __getitem__(self, idx):
        item = dict(self.dataset[idx])
        wav = item[self.key]
        num_channels = wav.shape[self.axis]
        if num_channels > 1:
            if self.method == 'average':
                wav = np.mean(wav, axis=self.axis, keepdims=True)
            elif self.method == 'random_uniform':
                weights = np.random.dirichlet(np.ones(num_channels))
                weights = weights.astype(wav.dtype)
                if self.axis == -1 or self.axis == len(wav.shape) - 1:
                    wav = np.dot(wav, weights)[..., np.newaxis]
                else:
                    weights = weights.reshape(weights.shape +
                                              (1,) *
                                              (len(wav.shape[self.axis:]) - 1))
                    wav = (wav * weights).sum(self.axis, keepdims=True)
        item[self.key] = wav
        return item


class Mixup(Dataset):
    """
    Applies Mixup to a dataset, by drawing a second item uniformly at random
    and mixing the data of all given `keys` between the two with a weight drawn
    from a Beta distribution with concentration parameter `alpha` (the smaller,
    the more probable the weight will be near 0.0 or 1.0). Optionally binarizes
    the data in `binarize_keys` afterwards by setting positive values to 1.
    """
    def __init__(self, dataset, keys, binarize_keys=(), alpha=0.3):
        super(Mixup, self).__init__(
                shapes=dataset.shapes, dtypes=dataset.dtypes,
                num_classes=dataset.num_classes, num_items=len(dataset))
        self.dataset = dataset
        self.keys = set(keys)
        self.binarize_keys = set(binarize_keys)
        self.alpha = alpha

    def __getattr__(self, attr):
        return getattr(self.dataset, attr)

    def __getitem__(self, idx):
        item1 = self.dataset[idx]
        item2 = self.dataset[np.random.randint(len(self.dataset))]
        w1 = np.random.beta(self.alpha, self.alpha)
        w2 = 1 - w1
        # use the more highly weighted item as the basis
        item = dict(item1 if w1 >= w2 else item2)
        # mix data from the two items
        for k in self.keys:
            item[k] = w1 * item1[k] + w2 * item2[k]
        for k in self.binarize_keys:
            item[k] = (item[k] > 0.0).astype(item[k].dtype)
        return item


def get_itemid(filename):
    """
    Returns the file name without path and without file extension.
    """
    return os.path.splitext(os.path.basename(filename))[0]


def find_files(basedir, regexp):
    """
    Finds all files below `basedir` that match `regexp`, sorted alphabetically.
    """
    regexp = re.compile(regexp)
    return sorted(fn for fn in glob.glob(os.path.join(basedir, '**'),
                                         recursive=True)
                  if regexp.match(fn))


def derive_labelset(train_csv):
    """
    Returns the set of used ebird codes, sorted by latin names.
    """
    labelset_latin = sorted(set(train_csv.primary_label))
    latin_to_ebird = dict(zip(train_csv.primary_label, train_csv.ebird_code))
    labelset_ebird = [latin_to_ebird[latin] for latin in labelset_latin]
    if len(set(labelset_ebird)) != len(labelset_ebird):
        raise RuntimeError("Inconsistent latin names in train.csv!")
    return labelset_ebird


def make_multilabel_target(num_classes, classes):
    """
    Creates a k-hot vector of length `num_classes` with 1.0 at every index in
    `classes`.
    """
    target = np.zeros(num_classes, dtype=np.uint8)
    target[classes] = 1
    return target


def read_noise_csv(fn, sample_rate, length):
    """
    Figure out bird-free portions in a background noise .csv file.
    """
    df = pd.read_csv(fn, '\t')
    if len(df.columns) < 2:
        df = pd.read_csv(fn, ',')
    if 'Time (s)' in df and 'Freq (Hz)' in df:
        # only time points given, assume each call lasts 3s
        beginnings = df['Time (s)'].values - 1.5
        endings = beginnings + 1.5
    elif 'Begin Time (s)' in df and 'End Time (s)' in df:
        beginnings = df['Begin Time (s)'].values
        endings = df['End Time (s)'].values
    else:
        raise ValueError("Unsupported csv format: %s" % fn)
    beginnings = (beginnings * sample_rate).astype(np.int)
    endings = (endings * sample_rate).astype(np.int)
    # now we know the bird segments. they may overlap or include each other.
    # we will compute the number of active birds at each change point: each
    # beginning adds an active bird, each ending subtracts one.
    beginnings = np.stack((beginnings, np.full_like(beginnings, +1)))
    endings = np.stack((endings, np.full_like(endings, -1)))
    changepoints = sorted(map(tuple, itertools.chain(beginnings.T, endings.T)))
    times, changes = np.asarray([(0, 0)] + changepoints + [(length, 0)]).T
    num_sources = np.cumsum(changes[:-1])
    # now we just need to take the segments with zero active birds
    inactive = np.where(num_sources == 0)[0]
    beginnings = times[inactive]
    endings = times[inactive + 1]
    lengths = endings - beginnings
    return list(zip(beginnings, lengths))


def create_noise_dataset(cfg):
    """
    Creates a NoiseDataset instance of sounds with bird-free segments.
    """
    here = os.path.dirname(__file__)
    basedir = os.path.join(here, cfg['data.mix_background_noise.audio_dir'])
    audio_files = find_files(basedir,
                             cfg['data.mix_background_noise.audio_regexp'])
    sample_rate = cfg['data.sample_rate']
    audios = [audio.WavFile(fn, sample_rate=sample_rate)
              for fn in tqdm.tqdm(audio_files, 'Reading noise',
                                  ascii=bool(cfg['tqdm.ascii']))]
    segment_files = [os.path.splitext(fn)[0] + '.csv' for fn in audio_files]
    segments = [read_noise_csv(fn, sample_rate, len(wav))
                if os.path.exists(fn)
                else [(0, len(wav))]
                for fn, wav in zip(segment_files, audios)]
    return NoiseDataset(audios, segments,
                        min_length=sample_rate * cfg['data.len_min'])


def create_synthetic_noise_dataset(cfg):
    """
    Creates a NoiseDataset instance of sounds with synthetic noise.
    """
    from colorednoise import powerlaw_psd_gaussian

    betas = np.linspace(cfg['data.mix_synthetic_noise.min_beta'],
                        cfg['data.mix_synthetic_noise.max_beta'],
                        num=cfg['data.mix_synthetic_noise.num_samples'])
    sample_rate = cfg['data.sample_rate']
    segment_length = 2 * cfg['data.len_min']
    wavs = [powerlaw_psd_gaussian(beta, sample_rate * segment_length)
            for beta in betas]
    wavs = [audio.normalize(wav, low=-1, high=1) for wav in wavs]
    return NoiseDataset(wavs)


def create(cfg, designation):
    config.add_defaults(cfg, pyfile=__file__)
    here = os.path.dirname(__file__)

    # browse for audio files
    basedir = os.path.join(here, cfg['data.audio_dir'])
    audio_files = find_files(basedir, cfg['data.audio_regexp'])
    if cfg['debug']:
        print("Found %d audio files in %s matching %s." %
            (len(audio_files), basedir, cfg['data.audio_regexp']))
    if not audio_files:
        raise RuntimeError("Did not find any audio files in %s matching %s." %
                           (basedir, cfg['data.audio_regexp']))

    # read official train.csv file
    train_csv = pd.read_csv(os.path.join(here, cfg['data.train_csv']),
                                         index_col='filename')

    # derive set of labels, ordered by latin names
    labelset_ebird = derive_labelset(train_csv)
    ebird_to_idx = {ebird: idx for idx, ebird in enumerate(labelset_ebird)}
    num_classes = len(labelset_ebird)

    # for training and validation, read and convert all required labels
    if designation in ('train', 'valid'):
        # combine with additional .csv files
        for fn in cfg['data.extra_csvs'].split(':'):
            train_csv = train_csv.append(pd.read_csv(os.path.join(here, fn),
                                                     index_col='filename'))
        if cfg['debug']:
            print("Found %d entries in .csv files." % len(train_csv))

        # remove file extensions from .csv index column
        train_csv.rename(index=lambda fn: os.path.splitext(fn)[0],
                         inplace=True)

        # add additional ebird codes for inconsistent latin names
        latin_to_ebird = dict(zip(train_csv.primary_label,
                                  train_csv.ebird_code))

        # constrain .csv rows to selected audio files and vice versa
        csv_ids = set(train_csv.index)
        audio_ids = {get_itemid(fn): fn for fn in audio_files}
        audio_ids = {k: fn for k, fn in audio_ids.items() if k in csv_ids}
        train_csv = train_csv.loc[[i in audio_ids for i in train_csv.index]]
        train_csv['audiofile'] = [audio_ids[i] for i in train_csv.index]
        if cfg['debug']:
            print("Found %d entries matching the audio files." %
                len(train_csv))

        # convert foreground and background labels to numbers
        latin_to_idx = {latin: ebird_to_idx[ebird]
                        for latin, ebird in latin_to_ebird.items()}
        train_csv['label_fg'] = [latin_to_idx[latin]
                                 for latin in train_csv.primary_label]
        train_csv['label_bg'] = [
                make_multilabel_target(num_classes,
                                       [latin_to_idx[latin]
                                        for latin in eval(labels)
                                        if latin_to_idx.get(latin, fg) != fg])
                for labels, fg in zip(train_csv.secondary_labels,
                                      train_csv.label_fg)]
        weight_fg = cfg['data.label_fg_weight']
        weight_bg = cfg['data.label_bg_weight']
        label_fg_onehot = np.eye(num_classes,
                                 dtype=np.float32)[train_csv.label_fg]
        label_bg = np.stack(train_csv.label_bg.values)
        train_csv['label_all'] = list(weight_fg * label_fg_onehot +
                                      weight_bg * label_bg)

        # train/valid split
        if cfg['data.split_mode'] == 'byrecordist':
            train_idxs, valid_idxs = splitting.grouped_split(
                    train_csv.index,
                    groups=pd.factorize(train_csv.recordist, sort=True)[0],
                    test_size=(cfg['data.valid_size'] / len(train_csv)
                               if cfg['data.valid_size'] >= 1 else
                               cfg['data.valid_size']),
                    seed=cfg['data.split_seed'])
        elif cfg['data.split_mode'] == 'stratified':
            train_idxs, valid_idxs = splitting.stratified_split(
                    train_csv.index,
                    y=train_csv.label_fg,
                    test_size=cfg['data.valid_size'],
                    seed=cfg['data.split_seed'])
        else:
            raise ValueError("Unknown data.split_mode=%s" % cfg['data.split_mode'])
        if designation == 'train':
            train_csv = train_csv.iloc[train_idxs]
        elif designation == 'valid':
            train_csv = train_csv.iloc[valid_idxs]
        if cfg['debug']:
            print("Kept %d items for this split." % len(train_csv))

        # filter by quality rating
        if cfg['data.min_rating_%s' % designation]:
            train_csv = train_csv.loc[
                    train_csv.rating >= cfg['data.min_rating_%s' % designation]]
            if cfg['debug']:
                print("Have %d remaining with rating >= %f." %
                      len(train_csv, cfg['data.min_rating_%s' % designation]))

        # update audio_files list to match train_csv
        audio_files = train_csv.audiofile
        itemids = train_csv.index
    elif designation == 'test':
        itemids = audio_files

    # prepare the audio files, assume a consistent sample rate
    if not cfg.get('data.sample_rate'):
        cfg['data.sample_rate'] = audio.get_sample_rate(audio_files[0])
    sample_rate = cfg['data.sample_rate']
    # TODO: support .mp3?
    audios = [audio.WavFile(fn, sample_rate=sample_rate)
              for fn in tqdm.tqdm(audio_files, 'Reading audio',
                                  ascii=bool(cfg['tqdm.ascii']))]

    # prepare annotations
    train_csv.rating = train_csv.rating.astype(np.float32)

    # create the dataset
    dataset = BirdcallDataset(itemids, audios, labelset_ebird,
                              annotations=train_csv)

    # unified length, if needed
    if cfg['data.len_min'] < cfg['data.len_max']:
        raise NotImplementedError("data.len_min < data.len_max not allowed yet")
    elif cfg['data.len_max'] > 0:
        dataset = FixedSizeExcerpts(dataset,
                                    int(sample_rate * cfg['data.len_min']),
                                    deterministic=designation != 'train')

    # convert to float and move channel dimension to the front
    dataset = Floatify(dataset, transpose=True)

    # mix in background noise, if needed
    if (designation == 'train' and
            (cfg['data.mix_background_noise.probability'] or
             cfg['data.mix_background_noise.noise_only_probability'])):
        noisedataset = create_noise_dataset(cfg)
        noisedataset = FixedSizeExcerpts(noisedataset,
                                         int(sample_rate * cfg['data.len_min']),
                                         deterministic=False)
        noisedataset = Floatify(noisedataset, transpose=True)
        dataset = MixBackgroundNoise(
                dataset, noisedataset,
                probability=cfg['data.mix_background_noise.probability'],
                min_factor=cfg['data.mix_background_noise.min_factor'],
                max_factor=cfg['data.mix_background_noise.max_factor'],
                min_amp=cfg['data.mix_background_noise.min_amp'],
                max_amp=cfg['data.mix_background_noise.max_amp'],
                noise_only_probability=cfg['data.mix_background_noise.noise_only_probability'],
                label_keys=cfg['data.mix_background_noise.noise_only_zero_labels'].split(','))

    # downmixing, if needed
    if cfg['data.downmix'] != 'none':
        dataset = DownmixChannels(dataset,
                                  method=(cfg['data.downmix']
                                          if designation == 'train'
                                          else 'average'))

    # mix in synthetic (colored) noise, if needed
    if designation == 'train' and cfg['data.mix_synthetic_noise.probability']:
        noisedataset = create_synthetic_noise_dataset(cfg)
        noisedataset = FixedSizeExcerpts(noisedataset,
                                         int(sample_rate * cfg['data.len_min']),
                                         deterministic=False)
        noisedataset = Floatify(noisedataset, transpose=True)
        dataset = MixBackgroundNoise(
                dataset, noisedataset,
                probability=cfg['data.mix_synthetic_noise.probability'],
                min_factor=cfg['data.mix_synthetic_noise.min_factor'],
                max_factor=cfg['data.mix_synthetic_noise.max_factor'])

    # apply mixup, if needed
    if designation == 'train' and cfg['data.mixup.apply_to']:
        dataset = Mixup(
                dataset, keys=cfg['data.mixup.apply_to'].split(','),
                binarize_keys=(cfg['data.mixup.binarize'].split(',')
                               if cfg['data.mixup.binarize'] else ()),
                alpha=cfg['data.mixup.alpha'])

    # custom sampling
    if cfg['data.class_sample_weights'] and designation == 'train':
        class_weights = cfg['data.class_sample_weights']
        if class_weights not in ('equal', 'roundrobin'):
            class_weights = list(map(float, class_weights.split(',')))
        dataset.sampler = ClassWeightedRandomSampler(train_csv.label_fg,
                                                     class_weights)

    return dataset
