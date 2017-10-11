"""ML-Ensemble

:author: Sebastian Flennerhag
:copyright: 2017
:license: MIT

Learner classes
"""
# pylint: disable=too-few-public-methods
# pylint: disable=too-many-arguments
# pylint: disable=too-many-instance-attributes

from __future__ import print_function, division

import warnings
from copy import deepcopy
from abc import ABCMeta, abstractmethod

from .base import BaseParallel, OutputMixin, ProbaMixin, Group
from ._base_functions import (slice_array, transform, set_output_columns,
                              assign_predictions, score_predictions, replace,
                              save, load, prune_files)
from ..metrics import Data
from ..utils import pickle_load, check_instances, safe_print, print_time
from ..utils.exceptions import NotFittedError, FitFailedWarning
from ..externals.sklearn.base import clone
from ..externals.joblib.parallel import delayed
try:
    from time import perf_counter as time
except ImportError:
    from time import time


# Types of indexers that require fits only on subsets or only on the full data
ONLY_SUB = []
ONLY_ALL = ['fullindex', 'nonetype']


def make_learners(indexer, estimators, preprocessing,
                  learner_kwargs=None, transformer_kwargs=None):
    """Set learners and preprocessing pipelines in layer"""
    preprocessing, estimators = check_instances(estimators, preprocessing)

    if learner_kwargs is None:
        learner_kwargs = {}
    if transformer_kwargs is None:
        transformer_kwargs = {}

    transformers = [
        Transformer(estimator=tr,
                    name=preprocess_name,
                    indexer=indexer,
                    **transformer_kwargs)
        for preprocess_name, tr in preprocessing]

    learners = [Learner(estimator=est,
                        preprocess=pr_name,
                        indexer=indexer,
                        name=learner_name,
                        **learner_kwargs)
                for pr_name, learner_name, est in estimators]

    return Group(indexer=indexer, learners=learners, transformers=transformers)


###############################################################################
class IndexedEstimator(object):
    """Indexed Estimator

    Lightweight wrapper around estimator dumps during fitting.

    """
    __slots__ = [
        '_estimator', 'name', 'index', 'in_index', 'out_index', 'data']

    def __init__(self, estimator, name, index, in_index, out_index, data):
        self._estimator = estimator
        self.name = name
        self.index = index
        self.in_index = in_index
        self.out_index = out_index
        self.data = data

    @property
    def estimator(self):
        """Deep copy of estimator"""
        return deepcopy(self._estimator)

    @estimator.setter
    def estimator(self, estimator):
        self._estimator = estimator

    def __getstate__(self):
        """Return pickable object"""
        return (self._estimator, self.name, self.index, self.in_index,
                self.out_index, self.data)

    def __setstate__(self, state):
        """Load tuple into instance"""
        (self._estimator, self.name, self.index, self.in_index,
         self.out_index, self.data) = state


class _BaseEstimator(BaseParallel):

    """Base estimator class

    Common API for estimation objects
    """

    __meta_class__ = ABCMeta

    def __init__(self, name, estimator, indexer, **kwargs):
        self.verbose = kwargs.pop('verbose', False)
        super(_BaseEstimator, self).__init__(name, **kwargs)
        self._data_ = None
        self._times_ = None
        self._learner_ = None
        self._sublearners_ = None
        self._set_indexer(indexer)
        self._estimator = estimator

        # Collection flag
        # Default is false, only turned on during a fit call with no refit
        # See gen_fit and collect methods
        self.__collect__ = False

    def __call__(self, parallel, args, arg_type='estimator'):
        """Caller for producing jobs"""
        job = args['job']
        path = args['dir']
        _threading = self.backend == 'threading'

        aux = getattr(self, 'auxiliary', None)
        if aux:
            aux(parallel, args, arg_type='auxiliary')

        parallel(delayed(subtask, not _threading)(path)
                 for subtask in getattr(self, 'gen_%s' % job)(
                     **args[arg_type]))

        if self.__collect__:
            self.collect(path)

    @abstractmethod
    def _gen_pred(self, job, X, P, generator):
        """Generator for predicting with fitted learner

        Parameters
        ----------
        job: str
            type of job

        X : array
            input array

        P : array
            output array to populate. Must be writeable.

        generator : iterable
            iterator of learners of sub-learners to predict with.
            One of ``self.learner_`` and ``self.sublearners_``.
        """
        yield

    @abstractmethod
    def gen_fit(self, X, y, P=None, refit=True):
        """Generator for fitting learner on given data

        Parameters
        ----------
        X : array
            input array

        y : array
            training targets

        P : array
            output array to populate. Must be writeable.

        refit: bool, (default = True)
            if ``False`` will not refit fitted learner.
        """
        yield

    def _gen_fit(self, cls, X, y, P=None, refit=True):
        """Routine for generating fit jobs conditional on refit"""
        if not refit and self.__fitted__:
            return self.gen_transform(X, P)
        else:
            # Generate new sub-learners and collect
            self.__collect__ = True
            return self._fit_generator(cls, X, y, P)

    def _fit_generator(self, cls, X, y, P):
        """Generator for fit jobs"""
        # We use an index to keep track of partition and fold
        # For single-partition estimations, index[0] is constant
        i = 0
        if not self.__only_sub__:
            out = P if self.__only_all__ else None
            for partition_index in self.indexer.partition():
                # This yields None, None for default indexers
                yield cls(job='fit',
                          parent=self,
                          estimator=self.estimator,
                          in_index=partition_index,
                          out_index=None,
                          in_array=X,
                          targets=y,
                          out_array=out,
                          index=(i, 0))
                i += 1

        if not self.__only_all__:
            # Fit sub-learners on cv folds
            for i, (train_index, test_index) in enumerate(
                    self.indexer.generate()):
                # Note that we bump index[1] by 1 to have index[1] start at 1
                if self._partitions == 1:
                    index = (0, i + 1)
                else:
                    splits = self.indexer.folds
                    index = (i // splits, i % splits + 1)

                yield cls(job='fit',
                          parent=self,
                          estimator=self.estimator,
                          in_index=train_index,
                          out_index=test_index,
                          in_array=X,
                          targets=y,
                          out_array=P,
                          index=index)

    def gen_transform(self, X, P=None):
        """Generate cross-validated predict jobs"""
        return self._gen_pred('transform', X, P, self.sublearners_)

    def gen_predict(self, X, P=None):
        """Generate predicting jobs"""
        return self._gen_pred('predict', X, P, self.learner_)

    def collect(self, path):
        """Load fitted estimator from cache"""
        if self.__collect__:
            self.clear()
            (learner_files,
             learner_data,
             sublearner_files,
             sublearner_data) = self._collect(path)

            self._learner_ = learner_files
            self._sublearners_ = sublearner_files
            self._data_ = sublearner_data
            self._times_ = learner_data
            self.__fitted__ = True

            # Collection complete, turn off
            self.__collect__ = False

    def clear(self):
        """Clear load"""
        self._sublearners_ = None
        self._learner_ = None
        self._data_ = None
        self.__fitted__ = False

    def _set_indexer(self, indexer):
        """Set indexer and auxiliary attributes"""
        self._indexer = indexer
        self._partitions = indexer.partitions
        self.__only_all__ = indexer.__class__.__name__.lower() in ONLY_ALL
        self.__only_sub__ = indexer.__class__.__name__.lower() in ONLY_SUB

    def _collect(self, path):
        """Collect files from cache"""
        files = prune_files(path, self.name)
        learner_files = list()
        learner_data = list()
        sublearner_files = list()
        sublearner_data = list()
        for f in files:
            if f.index[1] == 0:
                learner_files.append(f)
                learner_data.append((f.name, f.data))
            else:
                sublearner_files.append(f)
                sublearner_data.append((f.name, f.data))

        if self.__only_sub__:
            # Full learners are the same as the sub-learners
            learner_files, learner_data = replace(sublearner_files)
        if self.__only_all__:
            # Sub learners are the same as the sub-learners
            sublearner_files, sublearner_data = replace(learner_files)

        self.__fitted__ = True

        return learner_files, learner_data, sublearner_files, sublearner_data

    def _return_attr(self, attr):
        if not self.__fitted__:
            raise NotFittedError("Instance not fitted.")
        return getattr(self, attr)

    @property
    def estimator(self):
        """Copy of estimator"""
        return clone(self._estimator)

    @estimator.setter
    def estimator(self, estimator):
        """Replace blueprint estimator"""
        self._estimator = estimator

    @property
    def learner_(self):
        """Generator for learner fitted on full data"""
        # pylint: disable=not-an-iterable
        out = self._return_attr('_learner_')
        for estimator in out:
            yield deepcopy(estimator)

    @property
    def sublearners_(self):
        """Generator for learner fitted on folds"""
        # pylint: disable=not-an-iterable
        out = self._return_attr('_sublearners_')
        for estimator in out:
            yield deepcopy(estimator)

    @property
    def raw_data(self):
        """List of data collected from each sub-learner during fiting."""
        return self._return_attr('_data_')

    @property
    def data(self):
        """Dictionary with aggregated data from fitting sub-learners."""
        out = self._return_attr('_data_')
        return Data(out)

    @property
    def times(self):
        """Fit and predict times for the final learners"""
        out = self._return_attr('_times_')
        return Data(out)

    @property
    def indexer(self):
        """Copy of indexer"""
        return self._indexer

    @indexer.setter
    def indexer(self, indexer):
        """Update indexer"""
        self._set_indexer(indexer)


###############################################################################
class Learner(OutputMixin, ProbaMixin, _BaseEstimator):

    """Learner

    Wrapper for base learners.

    Parameters
    __________
    estimator : obj
        estimator to construct learner from

    preprocess : str, obj
        preprocess transformer. Pass either the string
        cache reference or the transformer instance. If the latter,
        the :attr:`preprocess` will refer to the transformer name.

    indexer : obj, None
        indexer to use for generating fits.
        Set to ``None`` to fit only on all data.

    name : str
        name of learner. If ``preprocess`` is not ``None``,
        the name will be prepended to ``preprocess__name``.

    attr : str (default='predict')
        predict attribute, typically one of 'predict' and 'predict_proba'

    scorer : func
        function to use for scoring predictions during cross-validated
        fitting.

    output_columns : dict, optional
        mapping of prediction feature columns from learner to columns in
        output array. Normally, this map is ``{0: x}``, but if the ``indexer``
        creates partitions, each partition needs to be mapped:
        ``{0: x, 1: x + 1}``. Note that if ``output_columns`` are not given at
        initialization, the ``set_output_columns`` method must be called before
        running estimations.

    verbose : bool, int (default = False)
        whether to report completed fits.

    **kwargs : bool (default=True)
        Optional ParallelProcessing arguments. See :class:`BaseParallel`.
    """

    def __init__(self, estimator, preprocess, indexer, name, attr=None,
                 scorer=None, output_columns=None, proba=False,
                 auxiliary=None, **kwargs):
        super(Learner, self).__init__(name, estimator, indexer, **kwargs)
        self.proba = proba
        self.feature_span = None
        self.output_columns = output_columns
        self.attr = attr if attr else self._predict_attr
        self.n_pred = self._partitions

        # Set protected arguments
        self.preprocess = preprocess
        self.auxiliary = auxiliary

        # Ensure auxiliary is based on same indexer
        if self.auxiliary and self.auxiliary.indexer is not self.indexer:
            self.auxiliary.indexer = self.indexer

        # We protect these to avoid corrupting a fitted learner
        self._scorer = scorer

        if preprocess and not self.name.startswith('%s__' % preprocess):
            self.name = '%s__%s' % (preprocess, self.name)

        self._sublearners_ = None        # Fitted sub-learners
        self._learner_ = None            # Fitted learners

    def gen_fit(self, X, y, P=None, refit=True):
        # If we have a transformer, run it
        return self._gen_fit(SubLearner, X, y, P, refit)

    def _gen_pred(self, job, X, P, generator):
        for estimator in generator:
            yield SubLearner(job=job,
                             parent=self,
                             estimator=estimator.estimator,
                             in_index=estimator.in_index,
                             out_index=estimator.out_index,
                             in_array=X,
                             targets=None,
                             out_array=P,
                             index=estimator.index)

    def set_output_columns(self, X=None, y=None, n_left_concats=0):
        """Set the output_columns attribute"""
        # pylint: disable=unused-argument
        multiplier = self._check_proba_multiplier(y)
        target = self._partitions * multiplier + n_left_concats
        set_output_columns(
            [self], self._partitions, multiplier, n_left_concats, target)

        mi = n_left_concats
        mx = max([i for i in self.output_columns.values()]) + multiplier
        self.feature_span = (mi, mx)

    @property
    def scorer(self):
        """Copy of scorer"""
        return deepcopy(self._scorer)

    @scorer.setter
    def scorer(self, scorer):
        """Replace blueprint scorer"""
        self._scorer = scorer


class SubLearner(object):
    """Estimation task

    Wrapper around a sub_learner job.
    """
    def __init__(self, job, parent, estimator, in_index, out_index,
                 in_array, targets, out_array, index):
        self.job = job
        self.estimator = estimator
        self.in_index = in_index
        self.out_index = out_index
        self.in_array = in_array
        self.targets = targets
        self.out_array = out_array
        self.score_ = None
        self.index = tuple(index)

        self.attr = parent.attr
        self.preprocess = parent.preprocess
        self.scorer = parent.scorer
        self.raise_on_exception = parent.raise_on_exception
        self.verbose = parent.verbose
        self.output_columns = parent.output_columns[index[0]]

        self.score_ = None
        self.fit_time_ = None
        self.pred_time_ = None

        self.name = parent.name
        self.name_index = '__'.join(
            [self.name] + [str(i) for i in index])

        if self.preprocess is not None:
            self.preprocess_index = '__'.join(
                [self.preprocess] + [str(i) for i in index])
        else:
            self.processing_index = ''

    def __call__(self, path):
        """Launch job"""
        return getattr(self, self.job)(path)

    def fit(self, path):
        """Fit sub-learner"""
        t0 = time()
        transformers = self._load_preprocess(path)

        self._fit(transformers)

        if self.out_array is not None:
            self._predict(transformers, self.scorer is not None)

        o = IndexedEstimator(estimator=self.estimator,
                             name=self.name_index,
                             index=self.index,
                             in_index=self.in_index,
                             out_index=self.out_index,
                             data=self.data)

        save(path, self.name_index, o)

        if self.verbose:
            msg = "{:<30} {}".format(self.name_index, "done")
            f = "stdout" if self.verbose < 10 - 3 else "stderr"
            print_time(t0, msg, file=f)

    def predict(self, path):
        """Predict with sublearner"""
        t0 = time()
        transformers = self._load_preprocess(path)

        self._predict(transformers, False)
        if self.verbose:
            msg = "{:<30} {}".format(self.name_index, "done")
            f = "stdout" if self.verbose < 10 - 3 else "stderr"
            print_time(t0, msg, file=f)

    def transform(self, path):
        """Predict with sublearner"""
        return self.predict(path)

    def _fit(self, transformers):
        """Sub-routine to fit sub-learner"""
        xtemp, ytemp = slice_array(self.in_array,
                                   self.targets,
                                   self.in_index)

        # Transform input (triggers copying)
        t0 = time()
        for _, tr in transformers:
            xtemp, ytemp = transform(tr, xtemp, ytemp)

        # Fit estimator
        self.estimator.fit(xtemp, ytemp)
        self.fit_time_ = time() - t0

    def _load_preprocess(self, path):
        """Load preprocessing pipeline"""
        if self.preprocess is not None:
            obj = load(path, self.preprocess_index, self.raise_on_exception)
            tr_list = obj.estimator
        else:
            tr_list = list()
        return tr_list

    def _predict(self, transformers, score_preds):
        """Sub-routine to with sublearner"""
        n = self.in_array.shape[0]
        # For training, use ytemp to score predictions
        # During test time, ytemp is None
        xtemp, ytemp = slice_array(self.in_array,
                                   self.targets,
                                   self.out_index)
        t0 = time()
        for _, tr in transformers:
            xtemp, ytemp = transform(tr, xtemp, ytemp)

        predictions = getattr(self.estimator, self.attr)(xtemp)
        self.pred_time_ = time() - t0

        # Assign predictions to matrix
        assign_predictions(self.out_array,
                           predictions,
                           self.out_index,
                           self.output_columns,
                           n)

        # Score predictions if applicable
        if score_preds:
            self.score_ = score_predictions(
                ytemp, predictions, self.scorer,
                self.name_index, self.name)

    @property
    def data(self):
        """fit data"""
        out = {'score': self.score_,
               'ft': self.fit_time_,
               'pt': self.pred_time_,
               }
        return out


###############################################################################
class Transformer(OutputMixin, _BaseEstimator):

    """Preprocessing handler.

    Wrapper for transformation pipeline.

    Parameters
    __________
    estimator : obj
        transformation pipeline to construct learner from

    indexer : obj, None
        indexer to use for generating fits.
        Set to ``None`` to fit only on all data.

    name : str
        name of learner. If ``preprocess`` is not ``None``,
        the name will be prepended to ``preprocess__name``.

    output_columns : dict, optional
        If transformer is to be used to output data, need to
        set ``output_columns``. Normally, this map is
        ``{0: x}``, but if the ``indexer``
        creates partitions, each partition needs to be mapped:
        ``{0: x, 1: x + 1}``.

    verbose : bool, int (default = False)
        whether to report completed fits.

    raise_on_exception : bool (default=True)
        whether to warn on non-fatal exceptions or raise an error.
    """

    # Default to no output
    __no_output__ = True

    def __init__(
            self, estimator, indexer, name, output_columns=None, **kwargs):
        super(Transformer, self).__init__(name, estimator, indexer, **kwargs)
        self.feature_span = None
        self._output_columns = None
        self.output_columns = output_columns

    def gen_fit(self, X, y, P=None, refit=True):
        return self._gen_fit(SubTransformer, X, y, P, refit)

    def set_output_columns(self, X, y=None, n_left_concats=0):
        """Set the output_columns attribute"""
        # pylint: disable=unused-argument
        multiplier = X.shape[1]
        target = self._partitions * multiplier + n_left_concats
        set_output_columns(
            [self], self._partitions, multiplier, n_left_concats, target)

        mi = n_left_concats
        mx = max([i for i in self.output_columns.values()]) + multiplier
        self.feature_span = (mi, mx)

    @property
    def output_columns(self):
        """Output columns mapping"""
        return self._output_columns

    @output_columns.setter
    def output_columns(self, obj):
        """Update output columns"""
        self.__no_output__ = obj is None
        if obj is None:
            self._output_columns = obj
        elif isinstance(obj, dict):
            self._output_columns = obj
        else:
            self.set_output_columns(obj)

    @property
    def estimator(self):
        """Blueprint pipeline"""
        return [(tr_name, clone(tr))
                for tr_name, tr in self._estimator]

    @estimator.setter
    def estimator(self, estimator):
        """Update pipeline blueprint"""
        self._estimator = estimator

    def _gen_pred(self, job, X, P, generator):
        """Generator for Cache object"""
        for obj in generator:
            if P is None:
                yield Cache(obj, self.verbose)
            else:
                yield SubTransformer(job=job,
                                     parent=self,
                                     estimator=obj.estimator,
                                     in_index=obj.in_index,
                                     out_index=obj.out_index,
                                     in_array=X,
                                     out_array=P,
                                     index=obj.index,
                                     targets=None)


class Cache(object):

    """Cache wrapper for IndexedEstimator
    """

    def __init__(self, obj, verbose):
        self.obj = obj
        self.name = obj.name
        self.verbose = verbose

    def __call__(self, path):
        """Cache estimator to path"""
        save(path, self.name, self.obj)
        if self.verbose:
            msg = "{:<30} {}".format(self.name, "cached")
            f = "stdout" if self.verbose < 10 - 3 else "stderr"
            safe_print(msg, file=f)


class SubTransformer(object):

    """Sub-routine for fitting a pipeline
    """

    def __init__(self, job, parent, estimator, in_index, in_array,
                 targets, index, out_index=None, out_array=None):
        self.job = job
        self.estimator = estimator
        self.in_index = in_index
        self.out_index = out_index
        self.in_array = in_array
        self.out_array = out_array
        self.targets = targets
        self.index = index

        self.transform_time_ = None

        self.verbose = parent.verbose
        self.name = parent.name
        self.name_index = '__'.join(
            [self.name] + [str(i) for i in index])

        if parent.output_columns is not None:
            self.output_columns = parent.output_columns[index[0]]

    def __call__(self, path):
        """Launch job"""
        return getattr(self, self.job)(path)

    def predict(self, path=None):
        """Dump transformers for prediction"""
        # pylint: disable=unused-argument
        self._transform()

    def transform(self, path=None):
        """Dump transformers for prediction"""
        # pylint: disable=unused-argument
        self._transform()

    def _transform(self):
        """Run a transformation"""
        t0 = time()
        n = self.in_array.shape[0]
        xtemp, ytemp = slice_array(
            self.in_array, self.targets, self.out_index)

        for _, tr in self.estimator:
            xtemp, ytemp = transform(tr, xtemp, ytemp)

        assign_predictions(
            self.out_array, xtemp, self.out_index, self.output_columns, n)

        if self.verbose:
            msg = "{:<30} {}".format(self.name_index, "done")
            f = "stdout" if self.verbose < 10 - 3 else "stderr"
            print_time(t0, msg, file=f)

    def fit(self, path):
        """Fit transformers"""
        t0 = time()
        n = len(self.estimator)
        xtemp, ytemp = slice_array(
            self.in_array, self.targets, self.in_index)

        t0_f = time()
        fitted_transformers = list()
        for tr_name, tr in self.estimator:
            tr.fit(xtemp, ytemp)
            fitted_transformers.append((tr_name, tr))

            if n > 1:
                xtemp, ytemp = transform(tr, xtemp, ytemp)

        self.transform_time_ = time() - t0_f

        if self.out_array is not None:
            self._transform()

        o = IndexedEstimator(estimator=fitted_transformers,
                             name=self.name_index,
                             index=self.index,
                             in_index=self.in_index,
                             out_index=self.out_index,
                             data=self.data)
        save(path, self.name_index, o)
        if self.verbose:
            f = "stdout" if self.verbose < 10 else "stderr"
            msg = "{:<30} {}".format(self.name_index, "done")
            print_time(t0, msg, file=f)

    @property
    def data(self):
        """fit data"""
        return {'ft': self.transform_time_}


###############################################################################
class EvalTransformer(Transformer):

    r"""Evaluator version of the Transformer.

    Derived class from Transformer adapted to cross\-validated grid-search.
    See :class:`Transformer` for more details.
    """

    def __init__(self, *args, **kwargs):
        super(EvalTransformer, self).__init__(*args, **kwargs)
        self.__only_all__ = False
        self.__only_sub__ = True


class EvalLearner(Learner):

    """EvalLearner

    EvalLearner is a derived class from Learner used for cross-validated
    scoring of an estimator.

    Parameters
    __________
    estimator : obj
        estimator to construct learner from

    preprocess : str
        preprocess cache refernce

    indexer : obj, None
        indexer to use for generating fits.
        Set to ``None`` to fit only on all data.

    name : str
        name of learner. If ``preprocess`` is not ``None``,
        the name will be prepended to ``preprocess__name``.

    attr : str (default='predict')
        predict attribute, typically one of 'predict' and 'predict_proba'

    scorer : func
        function to use for scoring predictions during cross-validated
        fitting.

    error_score : int, float, None (default = None)
        score to set if cross-validation fails. Set to ``None`` to raise error.

    verbose : bool, int (default = False)
        whether to report completed fits.

    raise_on_exception : bool (default=True)
        whether to warn on non-fatal exceptions or raise an error.
    """
    def __init__(self, estimator, preprocess, indexer, name, attr, scorer,
                 error_score=None, verbose=False, raise_on_exception=False):
        super(EvalLearner, self).__init__(
            estimator=estimator, preprocess=preprocess, indexer=indexer,
            name=name, attr=attr, scorer=scorer, output_columns={0: 0},
            verbose=verbose, raise_on_exception=raise_on_exception)
        self.error_score = error_score

        # For consistency, set fit flags
        self.__only_sub__ = True
        self.__only_all__ = False

    def gen_fit(self, X, y, P=None, refit=True):
        """Generator for fitting learner on given data"""
        if not refit and self.__fitted__:
            self.gen_transform(X, P)

        # We use an index to keep track of partition and fold
        # For single-partition estimations, index[0] is constant
        if self.indexer is None:
            raise ValueError("Cannot run cross-validation without an indexer")

        self.__collect__ = True
        for i, (train_index, test_index) in enumerate(
                self.indexer.generate()):
            # Note that we bump index[1] by 1 to have index[1] start at 1
            if self._partitions == 1:
                index = (0, i + 1)
            else:
                index = (0, i % self._partitions + 1)
            yield EvalSubLearner(job='fit',
                                 parent=self,
                                 estimator=self.estimator,
                                 in_index=train_index,
                                 out_index=test_index,
                                 in_array=X,
                                 targets=y,
                                 index=index)


class EvalSubLearner(SubLearner):

    """EvalSubLearner

    sub-routine for cross-validated evaluation.
    """
    def __init__(self, job, parent, estimator, in_index, out_index,
                 in_array, targets, index):

        super(EvalSubLearner, self).__init__(
            job=job, parent=parent, estimator=estimator,
            in_index=in_index, out_index=out_index,
            in_array=in_array, out_array=None,
            targets=targets, index=index)
        self.error_score = parent.error_score
        self.train_score_ = None
        self.test_score_ = None
        self.train_pred_time_ = None
        self.test_pred_time_ = None

    def fit(self, path):
        """Evaluate sub-learner"""
        if self.scorer is None:
            raise ValueError("Cannot generate CV-scores without a scorer")
        t0 = time()
        transformers = self._load_preprocess(path)
        self._fit(transformers)
        self._predict(transformers)

        o = IndexedEstimator(estimator=self.estimator,
                             name=self.name_index,
                             index=self.index,
                             in_index=self.in_index,
                             out_index=self.out_index,
                             data=self.data)
        save(path, self.name_index, o)

        if self.verbose:
            f = "stdout" if self.verbose else "stderr"
            msg = "{:<30} {}".format(self.name_index, "done")
            print_time(t0, msg, file=f)

    def _predict(self, transformers, score_preds=None):
        """Sub-routine to with sublearner"""
        # Train set
        self.train_score_, self.train_pred_time_ = self._score_preds(
            transformers, self.in_index)

        # Validation set
        self.test_score_, self.test_pred_time_ = self._score_preds(
            transformers, self.out_index)

    def _score_preds(self, transformers, index):
        # Train scores
        xtemp, ytemp = slice_array(self.in_array,
                                   self.targets,
                                   index)
        for _, tr in transformers:
            xtemp, ytemp = transform(tr, xtemp, ytemp)

        t0 = time()

        if self.error_score is not None:
            try:
                scores = self.scorer(self.estimator, xtemp, ytemp)
            except Exception as exc:  # pylint: disable=broad-except
                warnings.warn(
                    "Scoring failed. Setting error score %r."
                    "Details:\n%r" % (self.error_score, exc),
                    FitFailedWarning)
                scores = self.error_score
        else:
            scores = self.scorer(self.estimator, xtemp, ytemp)
        pred_time = time() - t0

        return scores, pred_time

    @property
    def data(self):
        """Score data"""
        out = {'test_score': self.test_score_,
               'train_score': self.train_score_,
               'fit_time': self.fit_time_,
               'pred_time': self.train_pred_time_,
               # 'test_pred_time': self.train_pred_time_,
               }
        return out
