#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import absolute_import
from __future__ import division

import six
from six.moves import zip
from copy import copy
from collections import namedtuple
import tensorflow as tf

from zhusuan.utils import merge_dicts


__all__ = [
    "SGMCMC",
    "SGLD",
    "PSGLD",
    "SGHMC",
    "SGNHT",
]


class SGMCMC(object):
    """
    Base class for stochastic gradient MCMC (SGMCMC) algorithms.

    SGMCMC is a class of MCMC algorithms which utilize stochastic gradients
    instead of the true gradients. To deal with the problems brought by
    stochasticity in gradients, more sophisticated updating scheme, such as
    SGHMC and SGNHT, were proposed. We provided four SGMCMC algorithms here:
    SGLD, PSGLD, SGHMC and SGNHT. For SGHMC and SGNHT, we support 2nd-order
    integrators introduced in (Chen et al., 2015).

    The implementation framework is similar to that of
    :class:`~zhusuan.hmc.HMC` class. However, SGMCMC algorithms do not include
    Metropolis update, and typically do not include hyperparameter adaptation,
    so the implementation here is easier to read than that of
    :class:`~zhusuan.hmc.HMC` class.

    The usage is also similar to that of :class:`~zhusuan.hmc.HMC` class.
    Running multiple SGMCMC chains in parallel is supported.

    To use the sampler, the user first defines the sampling method and
    corresponding hyperparameters by calling the subclass :class:`SGLD`,
    :class:`PSGLD`, :class:`SGHMC` or :class:`SGNHT`. Then the user creates a
    (list of) tensorflow `Variable` storing the initial sample, whose shape is
    ``chain axes + data axes``. There can be arbitrary number of chain axes
    followed by arbitrary number of data axes. Then the user provides a
    `log_joint` function which returns a tensor of shape ``chain axes``, which
    is the log joint density for each chain. Alternatively, the user can also
    provide a `meta_bn` instance as a description of `log_joint`. Then the user
    invokes :meth:`make_grad_func` to generate the gradient function used by
    SGMCMC algorithm. Finally, the user runs the operation returned by
    :meth:`sample`, which updates the sample stored in the `Variable`.

    The typical code for SGMCMC inference is like::

        sgmcmc = zs.SGHMC(learning_rate=2e-6, friction=0.2,
                          n_iter_resample_v=1000, second_order=True)
        sgmcmc.make_grad_func(meta_bn, observed={'x': x, 'y': y},
                              latent={'w1': w1, 'w2': w2})
        sample_op, new_w, sample_info = sgmcmc.sample()

        with tf.Session() as sess:
            for _ in range(n_iters):
                _, w, info = sess.run([sample_op, new_w, sample_info],
                                      feed_dict=...)

    .. note::

        The difference with :class:`~zhusuan.hmc.HMC` in usage is that one
        needs to call :meth:`make_grad_func` first, then call :meth:`sample`
        with a `grad_func`. This design is intended to enable the user to
        customize the gradient function, since the user may apply some variance
        reduction technique to the stochastic gradient when using SGMCMC.

    After getting the sample_op, the user can feed mini-batches to a data
    placeholder `observed` so that the gradient is a stochastic gradient. Then
    the user runs the sample_op like using HMC.
    """
    def __init__(self):
        self.t = tf.Variable(0, name="t", trainable=False, dtype=tf.int32)

    def make_grad_func(self, meta_bn, observed, latent):
        """
        Return the gradient function for sampling, given the log joint function
        (or a :class:`~zhusuan.framework.meta_bn.MetaBayesianNet` instance),
        observed values and latent variables.

        :param meta_bn: A function or a
            :class:`~zhusuan.framework.meta_bn.MetaBayesianNet` instance. If it
            is a function, it accepts a dictionary argument of ``(string,
            Tensor)`` pairs, which are mappings from all `StochasticTensor`
            names in the model to their observed values. The function should
            return a Tensor, representing the log joint likelihood of the
            model. More conveniently, the user can also provide a
            :class:`~zhusuan.framework.meta_bn.MetaBayesianNet` instance
            instead of directly providing a log_joint function. Then a
            log_joint function will be created so that `log_joint(obs) =
            meta_bn.observe(**obs).log_joint()`.
        :param observed: A dictionary of ``(string, Tensor)`` pairs. Mapping
            from names of observed `StochasticTensor` s to their values
        :param latent: A dictionary of ``(string, Variable)`` pairs.
            Mapping from names of latent `StochasticTensor` s to corresponding
            tensorflow `Variables` for storing their initial values and
            samples.

        :return: A default gradient function for sampling algorithm, whose
            argument is a list of tensorflow Variables corresponding to the
            latent variables.
        :return: The same gradient function whose arguments are the `Variable`
            list above **and** a `observed` dictionary, to allow users to
            create a gradient function with dataset splitting in case of large
            batch size and limited memory.
        :return: The `Variable` list which will serve as the argument of the
            gradient function.

        .. note::

            The return values are intended for enabling users to customize the
            gradient function. If the user wants to use the default gradient
            function, there is no need to fetch the return values (however this
            function should always be called).

        """
        if callable(meta_bn):
            self._meta_bn = None
            self._log_joint = meta_bn
        else:
            self._meta_bn = meta_bn
            self._log_joint = lambda obs: meta_bn.observe(**obs).log_joint()

        self._observed = observed
        self._latent = latent

        latent_k, latent_v = [list(i) for i in zip(*six.iteritems(latent))]
        for i, v in enumerate(latent_v):
            if not isinstance(v, tf.Variable):
                raise TypeError("latent['{}'] is not a tensorflow Variable."
                                .format(latent_k[i]))

        def _get_log_posterior(var_list, observed):
            joint_obs = merge_dicts(dict(zip(latent_k, var_list)), observed)
            return self._log_joint(joint_obs)

        def _get_gradient(var_list, observed):
            return tf.gradients(
                _get_log_posterior(var_list, observed), var_list)

        self._default_get_gradient = \
            lambda var_list: _get_gradient(var_list, observed)
        self._latent_k = latent_k
        self._var_list = latent_v
        return self._default_get_gradient, _get_gradient, latent_v

    def sample(self, grad_func=None):
        """
        Return the sampling `Operation` that runs a SGMCMC iteration and the
        statistics collected during it.

        :param grad_func: A function that accepts a list argument of tensorflow
            Variables and return the gradient at corresponding values. If the
            argument is not provided, then the default gradient function
            created in :meth:`make_grad_func` is used.

        :return: A Tensorflow `Operation` that runs a SGMCMC iteration.
        :return: A dictionary that records the new values of latent variables.
        :return: A dictionary that records some useful statistics (only for
            SGHMC and SGNHT).

        .. note::

            :meth:`make_grad_func` should be called first before calling this
            method.

        """
        qs = copy(self._var_list)
        self._define_variables(qs)
        if grad_func is None:
            grad_func = self._default_get_gradient
        update_ops, new_qs, infos = self._update(qs, grad_func)

        with tf.control_dependencies([self.t.assign_add(1)]):
            sample_op = tf.group(*update_ops)
        new_samples = dict(zip(self._latent_k, new_qs))
        sample_info = dict(zip(self._latent_k, infos))
        return sample_op, new_samples, sample_info

    def _update(self, qs, grad_func):
        return NotImplementedError()

    def _define_variables(self, qs):
        return NotImplementedError()

    @property
    def bn(self):
        if hasattr(self, "_meta_bn"):
            if self._meta_bn:
                if not hasattr(self, "_bn"):
                    self._bn = self._meta_bn.observe(
                        **merge_dicts(self._latent, self._observed))
                return self._bn
            else:
                return None
        else:
            return None


class SGLD(SGMCMC):
    """
    Subclass of SGMCMC which implements Stochastic Gradient Langevin Dynamics
    (Welling & Teh, 2011) (SGLD) update. The updating equation implemented
    below follows Equation (3) in the paper.

    :param learning_rate: A 0-D `float32` Tensor. It can be either a constant
        or a placeholder for decaying learning rate.
    :param add_noise: A `Bool` Tensor, if set to False, then the algorithm runs
        SGD instead of SGLD. The option is for comparison between SGD and SGLD.
    """
    def __init__(self, learning_rate, add_noise=True):
        self.lr = tf.convert_to_tensor(
            learning_rate, tf.float32, name="learning_rate")
        if type(add_noise) == bool:
            add_noise = tf.constant(add_noise)
        self.add_noise = add_noise
        super(SGLD, self).__init__()

    def _define_variables(self, qs):
        pass

    def _update(self, qs, grad_func):
        return zip(*[self._update_single(q, grad)
                     for q, grad in zip(qs, grad_func(qs))])

    def _update_single(self, q, grad):
        new_q = q + 0.5 * self.lr * grad
        new_q_with_noise = new_q + tf.random_normal(
            tf.shape(q), stddev=tf.sqrt(self.lr))
        new_q = tf.cond(
            self.add_noise, lambda: new_q_with_noise, lambda: new_q)
        update_q = q.assign(new_q)
        return update_q, new_q, tf.constant("No info.")


class PSGLD(SGLD):

    """
    Subclass of SGLD implementing preconditioned stochastic gradient Langevin
    dynamics, a variant proposed in (Li et al, 2015). We implement the RMSprop
    preconditioner (Equation (4-5) in the paper). Other preconditioners can be
    implemented similarly.

    :param learning_rate: A 0-D `float32` Tensor. It can be either a constant
        or a placeholder for decaying learning rate.
    :param add_noise: A `Bool` Tensor, if set to False, then the algorithm runs
        SGD instead of SGLD. The option is for comparison between SGD and SGLD.
    """

    class RMSPreconditioner:

        HParams = namedtuple('RMSHParams', 'decay epsilon')
        default_hps = HParams(decay=0.9, epsilon=1e-3)

        @staticmethod
        def _define_variables(qs):
            return [tf.Variable(tf.zeros_like(q)) for q in qs]

        @staticmethod
        def _get_preconditioner(hps, q, grad, aux):
            aux = tf.assign(aux, hps.decay * aux + (1-hps.decay) * grad**2)
            return 1 / (hps.epsilon + tf.sqrt(aux))

    def __init__(self, learning_rate, add_noise=True,
                 preconditioner='rms', preconditioner_hparams=None):
        self.preconditioner = {
            'rms': PSGLD.RMSPreconditioner
        }[preconditioner]
        if preconditioner_hparams is None:
            preconditioner_hparams = self.preconditioner.default_hps
        self.preconditioner_hparams = preconditioner_hparams
        super(PSGLD, self).__init__(learning_rate, add_noise)

    def _define_variables(self, qs):
        self.vs = self.preconditioner._define_variables(qs)

    def _update(self, qs, grad_func):
        return zip(*[self._update_single(q, grad, aux)
                     for q, grad, aux in zip(qs, grad_func(qs), self.vs)])

    def _update_single(self, q, grad, aux):
        g = self.preconditioner._get_preconditioner(
            self.preconditioner_hparams, q, grad, aux)
        new_q = q + 0.5 * self.lr * g * grad
        new_q_with_noise = new_q + tf.random_normal(
            tf.shape(q), stddev=tf.sqrt(self.lr * g))
        new_q = tf.cond(
            self.add_noise, lambda: new_q_with_noise, lambda: new_q)
        update_q = q.assign(new_q)
        return update_q, new_q, tf.constant("No info.")


class SGHMC(SGMCMC):
    """
    Subclass of SGMCMC which implements Stochastic Gradient Hamiltonian Monte
    Carlo (Chen et al., 2014) (SGHMC) update. Compared to SGLD, it adds a
    momentum variable to the dynamics. Compared to naive HMC using stochastic
    gradient which diverges, SGHMC simultanenously adds (often the same amount
    of) friction and noise to make the dynamics have a stationary distribution.
    The updating equation implemented below follows Equation (15) in the paper.
    A 2nd-order integrator introduced in (Chen et al., 2015) is supported.

    In the following description, we refer to Eq.(*) as Equation (15) in the
    SGHMC paper.

    :param learning_rate: A 0-D `float32` Tensor corresponding to :math:`\eta`
        in Eq.(*). Note that it does not scale the same as `learning_rate` in
        :class:`SGLD` since :math:`\eta=O(\epsilon^2)` in Eq.(*) where
        :math:`\epsilon` is the step size. When NaN occurs, please consider
        decreasing `learning_rate`.
    :param friction: A 0-D `float32` Tensor corresponding to :math:`\\alpha` in
        Eq.(*). A coefficient which simultaneously decays the momentum and adds
        an additional noise (hence here the name `friction` is not accurate).
        Larger `friction` makes the stationary distribution closer to the true
        posterior since it reduces the effect of stochasticity in the gradient,
        but slowers mixing of the MCMC chain.
    :param variance_estimate: A 0-D `float32` Tensor corresponding to
        :math:`\\beta` in Eq.(*). Just set it to zero if it is hard to estimate
        the gradient variance well. Note that `variance_estimate` must be
        smaller than `friction`.
    :param n_iter_resample_v: A 0-D `int32` Tensor. Each `n_iter_resample_v`
        calls to the sampling operation, the momentum variable will be
        resampled from the corresponding normal distribution once. Smaller
        `n_iter_resample_v` may lead to a stationary distribution closer to the
        true posterior but slowers mixing. If you do not want the momentum
        variable resampled, set the parameter to ``None`` or 0.
    :param second_order: A `bool` Tensor indicating whether to enable the
        2nd-order integrator introduced in (Chen et al., 2015) or to use the
        ordinary 1st-order integrator.
    """
    def __init__(self, learning_rate, friction=0.25, variance_estimate=0.,
                 n_iter_resample_v=20, second_order=True):
        self.lr = tf.convert_to_tensor(
            learning_rate, tf.float32, name="learning_rate")
        self.alpha = tf.convert_to_tensor(
            friction, tf.float32, name="alpha")
        self.beta = tf.convert_to_tensor(
            variance_estimate, tf.float32, name="beta")
        if n_iter_resample_v is None:
            n_iter_resample_v = 0
        self.n_iter_resample_v = tf.convert_to_tensor(
            n_iter_resample_v, tf.int32, name="n_iter_resample_v")
        self.second_order = second_order
        super(SGHMC, self).__init__()

    def _define_variables(self, qs):
        # Define the augmented momentum variables.
        self.vs = [
            tf.Variable(tf.random_normal(tf.shape(q), stddev=tf.sqrt(self.lr)))
            for q in qs]

    def _update(self, qs, grad_func):
        def resample_momentum(v):
            return tf.random_normal(tf.shape(v), stddev=tf.sqrt(self.lr))

        old_vs = [
            tf.cond(
                tf.equal(self.n_iter_resample_v, 0),
                lambda: v,
                lambda: tf.cond(
                    tf.equal(tf.mod(self.t, self.n_iter_resample_v), 0),
                    lambda: resample_momentum(v), lambda: v))
            for v in self.vs]
        gaussian_terms = [
            tf.random_normal(
                tf.shape(old_v),
                stddev=tf.sqrt(2*(self.alpha-self.beta)*self.lr))
            for old_v in old_vs]
        if not self.second_order:
            new_vs = [
                (1 - self.alpha) * old_v + self.lr * grad + gaussian_term
                for (old_v, grad, gaussian_term) in zip(
                        old_vs, grad_func(qs), gaussian_terms)]
            new_qs = [q + new_v for (q, new_v) in zip(qs, new_vs)]
        else:
            decay_half = tf.exp(-0.5*self.alpha)
            q1s = [q + 0.5 * old_v for (q, old_v) in zip(qs, old_vs)]
            new_vs = [
                decay_half * (
                    decay_half * old_v + self.lr * grad + gaussian_term)
                for (old_v, grad, gaussian_term) in zip(
                        old_vs, grad_func(q1s), gaussian_terms)]
            new_qs = [q1 + 0.5 * new_v for (q1, new_v) in zip(q1s, new_vs)]

        mean_ks = [tf.reduce_mean(new_v**2) for new_v in new_vs]
        infos = [{"mean_k": mean_k} for mean_k in mean_ks]

        with tf.control_dependencies(new_vs + new_qs):
            update_qs = [q.assign(new_q) for (q, new_q) in zip(qs, new_qs)]
            update_vs = [v.assign(new_v)
                         for (v, new_v) in zip(self.vs, new_vs)]

        update_ops = [tf.group(update_q, update_v)
                      for (update_q, update_v) in zip(update_qs, update_vs)]

        return update_ops, new_qs, infos


class SGNHT(SGMCMC):
    """
    Subclass of SGMCMC which implements Stochastic Gradient Nosé-Hoover
    Thermostat (Ding et al., 2014) (SGNHT) update. It is built upon SGHMC, and
    it could tune the friction parameter :math:`\\alpha` in SGHMC automatically
    (here is an abuse of notation: in SGNHT :math:`\\alpha` only refers to the
    friction coefficient, and the noise term is independent of it (unlike
    SGHMC)), i.e. it adds a new friction variable to the dynamics. The updating
    equation implemented below follows Algorithm 2 in the supplementary
    material of the paper. A 2nd-order integrator introduced in
    (Chen et al., 2015) is supported.

    In the following description, we refer to Eq.(**) as the equation in
    Algorithm 2 in the SGNHT paper.

    :param learning_rate: A 0-D `float32` Tensor corresponding to :math:`\eta`
        in Eq.(**). Note that it does not scale the same as `learning_rate` in
        :class:`SGLD` since :math:`\eta=O(\epsilon^2)` in Eq.(*) where
        :math:`\epsilon` is the step size. When NaN occurs, please consider
        decreasing `learning_rate`.
    :param variance_extra: A 0-D `float32` Tensor corresponding to :math:`a` in
        Eq.(**), representing the additional noise added in the update (and the
        initial friction :math:`\\alpha` will be set to this value). Normally
        just set it to zero.
    :param tune_rate: A 0-D `float32` Tensor. In Eq.(**), this parameter is not
        present (i.e. its value is implicitly set to 1), but a non-1 value is
        also valid. Higher `tune_rate` represents higher (multiplicative) rate
        of tuning the friction :math:`\\alpha`.
    :param n_iter_resample_v: A 0-D `int32` Tensor. Each `n_iter_resample_v`
        calls to the sampling operation, the momentum variable will be
        resampled from the corresponding normal distribution once. Smaller
        `n_iter_resample_v` may lead to a stationary distribution closer to the
        true posterior but slowers mixing. If you do not want the momentum
        variable resampled, set the parameter to ``None`` or 0.
    :param second_order: A `bool` Tensor indicating whether to enable the
        2nd-order integrator introduced in (Chen et al., 2015) or to use the
        ordinary 1st-order integrator.
    :param use_vector_alpha: A `bool` Tensor indicating whether to use a vector
        friction :math:`\\alpha`. If it is true, then the friction has the same
        shape as the latent variable. That is, each component of the latent
        variable corresponds to an independently tunable friction. Else, the
        friction is a scalar.
    """
    def __init__(self, learning_rate, variance_extra=0., tune_rate=1.,
                 n_iter_resample_v=None, second_order=True,
                 use_vector_alpha=True):
        self.lr = tf.convert_to_tensor(
            learning_rate, tf.float32, name="learning_rate")
        self.a = tf.convert_to_tensor(
            variance_extra, tf.float32, name="variance_extra")
        self.tune_rate = tf.convert_to_tensor(
            tune_rate, tf.float32, name="tune_rate")
        if n_iter_resample_v is None:
            n_iter_resample_v = 0
        self.n_iter_resample_v = tf.convert_to_tensor(
            n_iter_resample_v, tf.int32, name="n_iter_resample_v")
        self.second_order = second_order
        self.use_vector_alpha = use_vector_alpha
        super(SGNHT, self).__init__()

    def _define_variables(self, qs):
        # Define the augmented momentum variables.
        self.vs = [
            tf.Variable(tf.random_normal(tf.shape(q), stddev=tf.sqrt(self.lr)))
            for q in qs]
        # Define the augmented friction variables.
        if self.use_vector_alpha:
            self.alphas = [tf.Variable(self.a*tf.ones(tf.shape(q)))
                           for q in qs]
        else:
            self.alphas = [tf.Variable(self.a) for q in qs]

    def _update(self, qs, grad_func):
        def resample_momentum(v):
            return tf.random_normal(tf.shape(v), stddev=tf.sqrt(self.lr))

        def maybe_reduce_mean(tensor):
            if self.use_vector_alpha:
                return tensor
            else:
                return tf.reduce_mean(tensor)

        old_vs = [
            tf.cond(
                tf.equal(self.n_iter_resample_v, 0),
                lambda: v,
                lambda: tf.cond(
                    tf.equal(tf.mod(self.t, self.n_iter_resample_v), 0),
                    lambda: resample_momentum(v),
                    lambda: v))
            for v in self.vs]
        gaussian_terms = [
            tf.random_normal(tf.shape(old_v), stddev=tf.sqrt(2*self.a*self.lr))
            for old_v in old_vs]
        if not self.second_order:
            new_vs = [
                (1 - alpha) * old_v + self.lr * grad + gaussian_term
                for (old_v, alpha, grad, gaussian_term) in zip(
                        old_vs, self.alphas, grad_func(qs), gaussian_terms)]
            new_qs = [q + new_v for (q, new_v) in zip(qs, new_vs)]
            mean_ks = [maybe_reduce_mean(new_v**2) for new_v in new_vs]
            new_alphas = [alpha + self.tune_rate * (mean_k - self.lr)
                          for (alpha, mean_k) in zip(self.alphas, mean_ks)]
        else:
            q1s = [q + 0.5 * old_v for (q, old_v) in zip(qs, old_vs)]
            mean_k1s = [maybe_reduce_mean(old_v**2) for old_v in old_vs]
            alpha1s = [alpha + 0.5 * self.tune_rate * (mean_k1 - self.lr)
                       for (alpha, mean_k1) in zip(self.alphas, mean_k1s)]
            decay_halfs = [tf.exp(-0.5*alpha1) for alpha1 in alpha1s]
            new_vs = [
                decay_half * (
                    decay_half * old_v + self.lr * grad + gaussian_term)
                for (decay_half, old_v, grad, gaussian_term) in zip(
                        decay_halfs, old_vs, grad_func(q1s), gaussian_terms)]
            new_qs = [q1 + 0.5 * new_v for (q1, new_v) in zip(q1s, new_vs)]
            mean_ks = [maybe_reduce_mean(new_v**2) for new_v in new_vs]
            new_alphas = [alpha1 + 0.5 * self.tune_rate * (mean_k - self.lr)
                          for (alpha1, mean_k) in zip(alpha1s, mean_ks)]

        infos = [{
            "mean_k": tf.reduce_mean(mean_k),
            "alpha": tf.reduce_mean(new_alpha)
        } for (mean_k, new_alpha) in zip(mean_ks, new_alphas)]

        with tf.control_dependencies(new_vs + new_qs + new_alphas):
            update_qs = [q.assign(new_q) for (q, new_q) in zip(qs, new_qs)]
            update_vs = [v.assign(new_v)
                         for (v, new_v) in zip(self.vs, new_vs)]
            update_alphas = [alpha.assign(new_alpha) for (alpha, new_alpha)
                             in zip(self.alphas, new_alphas)]

        update_ops = [
            tf.group(update_q, update_v, update_alpha)
            for (update_q, update_v, update_alpha) in zip(
                    update_qs, update_vs, update_alphas)]

        return update_ops, new_qs, infos
