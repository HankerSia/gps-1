""" This file defines code for iLQG-based trajectory optimization. """
import logging
import copy

import numpy as np
from numpy.linalg import LinAlgError
import scipy as sp

from gps.algorithm.traj_opt.config import TRAJ_OPT_LQR
from gps.algorithm.traj_opt.traj_opt import TrajOpt
from gps.algorithm.traj_opt.traj_opt_utils import \
        DGD_MAX_ITER, DGD_MAX_LS_ITER, DGD_MAX_GD_ITER, \
        ALPHA, BETA1, BETA2, EPS, \
        traj_distr_kl, traj_distr_kl_alt

from gps.algorithm.algorithm_badmm import AlgorithmBADMM
from gps.algorithm.algorithm_mdgps import AlgorithmMDGPS


LOGGER = logging.getLogger(__name__)


class TrajOptLQRPython(TrajOpt):
    """ LQR trajectory optimization, Python implementation. """
    def __init__(self, hyperparams):
        config = copy.deepcopy(TRAJ_OPT_LQR)
        config.update(hyperparams)

        TrajOpt.__init__(self, config)

        self.cons_per_step = config['cons_per_step'] #enforce kl distrib per time step
        self._use_prev_distr = config['use_prev_distr']
        self._update_in_bwd_pass = config['update_in_bwd_pass']

    # TODO - Add arg and return spec on this function.
    def update(self, m, algorithm):
        """ Run dual gradient decent to optimize trajectories. """
        T = algorithm.T
        eta = algorithm.cur[m].eta
        if self.cons_per_step and type(eta) in (int, float):
            eta = np.ones(T) * eta
        step_mult = algorithm.cur[m].step_mult
        traj_info = algorithm.cur[m].traj_info

        if isinstance(algorithm, AlgorithmMDGPS):
            # For MDGPS, constrain to previous NN linearization
            prev_traj_distr = algorithm.cur[m].pol_info.traj_distr()
        else:
            # For BADMM/trajopt, constrain to previous LG controller
            prev_traj_distr = algorithm.cur[m].traj_distr

        # Set KL-divergence step size (epsilon).
        kl_step = algorithm.base_kl_step * step_mult
        if not self.cons_per_step:
            kl_step *= T

        # We assume at min_eta, kl_div > kl_step, opposite for max_eta.
        if not self.cons_per_step:
            min_eta = self._hyperparams['min_eta']
            max_eta = self._hyperparams['max_eta']
            LOGGER.debug("Running DGD for trajectory %d, eta: %f", m, eta)
        else:
            min_eta = np.ones(T) * self._hyperparams['min_eta']
            max_eta = np.ones(T) * self._hyperparams['max_eta']
            LOGGER.debug("Running DGD for trajectory %d, avg eta: %f", m,
                         np.mean(eta[:-1]))

        max_itr = (DGD_MAX_LS_ITER if self.cons_per_step else
                   DGD_MAX_ITER)
        for itr in range(max_itr):
            if not self.cons_per_step:
                LOGGER.debug("Iteration %d, bracket: (%.2e , %.2e , %.2e)", itr,
                             min_eta, eta, max_eta)

            # Run fwd/bwd pass, note that eta may be updated.
            # Compute KL divergence constraint violation.
            traj_distr, eta = self.backward(prev_traj_distr, traj_info,
                                            eta, algorithm, m)

            if not self._use_prev_distr:
                new_mu, new_sigma = self.forward(traj_distr, traj_info)
                kl_div = traj_distr_kl(
                        new_mu, new_sigma, traj_distr, prev_traj_distr,
                        tot=(not self.cons_per_step)
                )
            else:
                prev_mu, prev_sigma = self.forward(prev_traj_distr, traj_info)
                kl_div = traj_distr_kl_alt(
                        prev_mu, prev_sigma, traj_distr, prev_traj_distr,
                        tot=(not self.cons_per_step)
                )

            con = kl_div - kl_step

            # Convergence check - constraint satisfaction.
            if self._conv_check(con, kl_step):
                if not self.cons_per_step:
                    LOGGER.debug("KL: %f / %f, converged iteration %d", kl_div,
                                 kl_step, itr)
                else:
                    LOGGER.debug(
                            "KL: %f / %f, converged iteration %d",
                            np.mean(kl_div[:-1]), np.mean(kl_step[:-1]), itr
                    )
                break

            if not self.cons_per_step:
                # Choose new eta (bisect bracket or multiply by constant)
                if con < 0: # Eta was too big.
                    max_eta = eta
                    geom = np.sqrt(min_eta*max_eta)  # Geometric mean.
                    new_eta = max(geom, 0.1*max_eta)
                    LOGGER.debug("KL: %f / %f, eta too big, new eta: %f",
                                 kl_div, kl_step, new_eta)
                else: # Eta was too small.
                    min_eta = eta
                    geom = np.sqrt(min_eta*max_eta)  # Geometric mean.
                    new_eta = min(geom, 10.0*min_eta)
                    LOGGER.debug("KL: %f / %f, eta too small, new eta: %f",
                                 kl_div, kl_step, new_eta)
                # Logarithmic mean: log_mean(x,y) = (y - x)/(log(y) - log(x))
                eta = new_eta
            else:
                for t in range(T):
                    if con[t] < 0:
                        max_eta[t] = eta[t]
                        geom = np.sqrt(min_eta[t]*max_eta[t])
                        eta[t] = max(geom, 0.1*max_eta[t])
                    else:
                        min_eta[t] = eta[t]
                        geom = np.sqrt(min_eta[t]*max_eta[t])
                        eta[t] = min(geom, 10.0*min_eta[t])
                if itr % 10 == 0:
                    LOGGER.debug("avg KL: %f / %f, avg new eta: %f",
                                 np.mean(kl_div[:-1]), np.mean(kl_step[:-1]),
                                 np.mean(eta[:-1]))

        if (self.cons_per_step and not self._conv_check(con, kl_step)):
            m_b, v_b = np.zeros(T-1), np.zeros(T-1)

            for itr in range(DGD_MAX_GD_ITER):
                traj_distr, eta = self.backward(prev_traj_distr, traj_info,
                                                eta, algorithm, m)

                if not self._use_prev_distr:
                    new_mu, new_sigma = self.forward(traj_distr, traj_info)
                    kl_div = traj_distr_kl(
                            new_mu, new_sigma, traj_distr, prev_traj_distr,
                            tot=False
                    )
                else:
                    prev_mu, prev_sigma = self.forward(prev_traj_distr,
                                                       traj_info)
                    kl_div = traj_distr_kl_alt(
                            prev_mu, prev_sigma, traj_distr, prev_traj_distr,
                            tot=False
                    )

                con = kl_div - kl_step
                if self._conv_check(con, kl_step):
                    LOGGER.debug(
                            "KL: %f / %f, converged iteration %d",
                            np.mean(kl_div[:-1]), np.mean(kl_step[:-1]), itr
                    )
                    break

                m_b = (BETA1 * m_b + (1-BETA1) * con[:-1])
                m_u = m_b / (1 - BETA1 ** (itr+1))
                v_b = (BETA2 * v_b + (1-BETA2) * np.square(con[:-1]))
                v_u = v_b / (1 - BETA2 ** (itr+1))
                eta[:-1] = np.minimum(
                        np.maximum(eta[:-1] + ALPHA * m_u / (np.sqrt(v_u) + EPS),
                                   self._hyperparams['min_eta']),
                        self._hyperparams['max_eta']
                )

                if itr % 10 == 0:
                    LOGGER.debug("avg KL: %f / %f, avg new eta: %f",
                                 np.mean(kl_div[:-1]), np.mean(kl_step[:-1]),
                                 np.mean(eta[:-1]))

        if (np.mean(kl_div) > np.mean(kl_step) and
            not self._conv_check(con, kl_step)):
            LOGGER.warning(
                    "Final KL divergence after DGD convergence is too high."
            )
        return traj_distr, eta

    def update_protagonist(self, m, algorithm):
        """ Run dual gradient decent to optimize trajectories. """
        T = algorithm.T
        eta = algorithm.cur[m].eta
        if self.cons_per_step and type(eta) in (int, float):
            eta = np.ones(T) * eta
        step_mult = algorithm.cur[m].step_mult
        traj_info = algorithm.cur[m].traj_info

        if isinstance(algorithm, AlgorithmMDGPS):
            # For MDGPS, constrain to previous NN linearization
            prev_traj_distr = algorithm.cur[m].pol_info.traj_distr()
        else:
            # For BADMM/trajopt, constrain to previous LG controller
            prev_traj_distr = algorithm.cur[m].traj_distr

        # Set KL-divergence step size (epsilon).
        kl_step = algorithm.base_kl_step * step_mult
        if not self.cons_per_step:
            kl_step *= T

        # We assume at min_eta, kl_div > kl_step, opposite for max_eta.
        if not self.cons_per_step:
            min_eta = self._hyperparams['min_eta']
            max_eta = self._hyperparams['max_eta']
            LOGGER.debug("Running DGD for trajectory %d, eta: %f", m, eta)
        else:
            min_eta = np.ones(T) * self._hyperparams['min_eta']
            max_eta = np.ones(T) * self._hyperparams['max_eta']
            LOGGER.debug("Running DGD for trajectory %d, avg eta: %f", m,
                         np.mean(eta[:-1]))

        max_itr = (DGD_MAX_LS_ITER if self.cons_per_step else
                   DGD_MAX_ITER)
        for itr in range(max_itr):
            if not self.cons_per_step:
                LOGGER.debug("Protagonist Iteration %d, bracket: (%.2e , %.2e , %.2e)", itr,
                             min_eta, eta, max_eta)

            # Run fwd/bwd pass, note that eta may be updated.
            # Compute KL divergence constraint violation.
            traj_distr, eta = self.backward_protagonist(prev_traj_distr, traj_info,
                                            eta, algorithm, m)

            if not self._use_prev_distr:
                new_mu, new_sigma = self.forward_protagonist(traj_distr, traj_info)
                kl_div = traj_distr_kl(
                        new_mu, new_sigma, traj_distr, prev_traj_distr,
                        tot=(not self.cons_per_step)
                )
            else:
                prev_mu, prev_sigma = self.forward_protagonist(prev_traj_distr, traj_info)
                kl_div = traj_distr_kl_alt(
                        prev_mu, prev_sigma, traj_distr, prev_traj_distr,
                        tot=(not self.cons_per_step)
                )

            con = kl_div - kl_step

            # Convergence check - constraint satisfaction.
            if self._conv_check(con, kl_step):
                if not self.cons_per_step:
                    LOGGER.debug("KL: %f / %f, converged iteration %d", kl_div,
                                 kl_step, itr)
                else:
                    LOGGER.debug(
                            "KL: %f / %f, converged iteration %d",
                            np.mean(kl_div[:-1]), np.mean(kl_step[:-1]), itr
                    )
                break

            if not self.cons_per_step:
                # Choose new eta (bisect bracket or multiply by constant)
                if con < 0: # Eta was too big.
                    max_eta = eta
                    geom = np.sqrt(min_eta*max_eta)  # Geometric mean.
                    new_eta = max(geom, 0.1*max_eta)
                    LOGGER.debug("KL: %f / %f, eta too big, new eta: %f",
                                 kl_div, kl_step, new_eta)
                else: # Eta was too small.
                    min_eta = eta
                    geom = np.sqrt(min_eta*max_eta)  # Geometric mean.
                    new_eta = min(geom, 10.0*min_eta)
                    LOGGER.debug("KL: %f / %f, eta too small, new eta: %f",
                                 kl_div, kl_step, new_eta)
                # Logarithmic mean: log_mean(x,y) = (y - x)/(log(y) - log(x))
                eta = new_eta
            else:
                for t in range(T):
                    if con[t] < 0:
                        max_eta[t] = eta[t]
                        geom = np.sqrt(min_eta[t]*max_eta[t])
                        eta[t] = max(geom, 0.1*max_eta[t])
                    else:
                        min_eta[t] = eta[t]
                        geom = np.sqrt(min_eta[t]*max_eta[t])
                        eta[t] = min(geom, 10.0*min_eta[t])
                if itr % 10 == 0:
                    LOGGER.debug("avg KL: %f / %f, avg new eta: %f",
                                 np.mean(kl_div[:-1]), np.mean(kl_step[:-1]),
                                 np.mean(eta[:-1]))

        if (self.cons_per_step and not self._conv_check(con, kl_step)):
            m_b, v_b = np.zeros(T-1), np.zeros(T-1)

            for itr in range(DGD_MAX_GD_ITER):
                traj_distr, eta = self.backward_protagonist(prev_traj_distr, traj_info,
                                                eta, algorithm, m)

                if not self._use_prev_distr:
                    new_mu, new_sigma = self.forward_protagonist(traj_distr, traj_info)
                    kl_div = traj_distr_kl(
                            new_mu, new_sigma, traj_distr, prev_traj_distr,
                            tot=False
                    )
                else:
                    prev_mu, prev_sigma = self.forward_protagonist(prev_traj_distr,
                                                       traj_info)
                    kl_div = traj_distr_kl_alt(
                            prev_mu, prev_sigma, traj_distr, prev_traj_distr,
                            tot=False
                    )

                con = kl_div - kl_step
                if self._conv_check(con, kl_step):
                    LOGGER.debug(
                            "KL: %f / %f, converged iteration %d",
                            np.mean(kl_div[:-1]), np.mean(kl_step[:-1]), itr
                    )
                    break

                m_b = (BETA1 * m_b + (1-BETA1) * con[:-1])
                m_u = m_b / (1 - BETA1 ** (itr+1))
                v_b = (BETA2 * v_b + (1-BETA2) * np.square(con[:-1]))
                v_u = v_b / (1 - BETA2 ** (itr+1))
                eta[:-1] = np.minimum(
                        np.maximum(eta[:-1] + ALPHA * m_u / (np.sqrt(v_u) + EPS),
                                   self._hyperparams['min_eta']),
                        self._hyperparams['max_eta']
                )

                if itr % 10 == 0:
                    LOGGER.debug("avg KL: %f / %f, avg new eta: %f",
                                 np.mean(kl_div[:-1]), np.mean(kl_step[:-1]),
                                 np.mean(eta[:-1]))

        if (np.mean(kl_div) > np.mean(kl_step) and
            not self._conv_check(con, kl_step)):
            LOGGER.warning(
                    "Final KL divergence after DGD convergence is too high."
            )
        return traj_distr, eta

    def update_adversary(self, m, algorithm):
        """ Run dual gradient decent to optimize trajectories. """
        T = algorithm.T
        eta = algorithm.cur[m].eta
        if self.cons_per_step and type(eta) in (int, float):
            eta = np.ones(T) * eta
        step_mult = algorithm.cur[m].step_mult
        traj_info = algorithm.cur[m].traj_info

        if isinstance(algorithm, AlgorithmMDGPS):
            # For MDGPS, constrain to previous NN linearization
            prev_traj_distr = algorithm.cur[m].pol_info.traj_distr_adv()
        else:
            # For BADMM/trajopt, constrain to previous LG controller
            prev_traj_distr = algorithm.cur[m].traj_distr_adv

        # Set KL-divergence step size (epsilon).
        kl_step = algorithm.base_kl_step * step_mult
        if not self.cons_per_step:
            kl_step *= T

        # We assume at min_eta, kl_div > kl_step, opposite for max_eta.
        if not self.cons_per_step:
            min_eta = self._hyperparams['min_eta']
            max_eta = self._hyperparams['max_eta']
            LOGGER.debug("Running DGD for trajectory %d, eta: %f", m, eta)
        else:
            min_eta = np.ones(T) * self._hyperparams['min_eta']
            max_eta = np.ones(T) * self._hyperparams['max_eta']
            LOGGER.debug("Running DGD for trajectory %d, avg eta: %f", m,
                         np.mean(eta[:-1]))

        max_itr = (DGD_MAX_LS_ITER if self.cons_per_step else
                   DGD_MAX_ITER)
        for itr in range(max_itr):
            if not self.cons_per_step:
                LOGGER.debug("Adversarial Iteration %d, bracket: (%.2e , %.2e , %.2e)", itr,
                             min_eta, eta, max_eta)

            # Run fwd/bwd pass, note that eta may be updated.
            # Compute KL divergence constraint violation.
            traj_distr, eta = self.backward_adversary(prev_traj_distr, traj_info,
                                            eta, algorithm, m)

            if not self._use_prev_distr:
                new_mu, new_sigma = self.forward_adversary(traj_distr, traj_info)
                kl_div = traj_distr_kl(
                        new_mu, new_sigma, traj_distr, prev_traj_distr,
                        tot=(not self.cons_per_step)
                )
            else:
                prev_mu, prev_sigma = self.forward_adversary(prev_traj_distr, traj_info)
                kl_div = traj_distr_kl_alt(
                        prev_mu, prev_sigma, traj_distr, prev_traj_distr,
                        tot=(not self.cons_per_step)
                )

            con = kl_div - kl_step

            # Convergence check - constraint satisfaction.
            if self._conv_check(con, kl_step):
                if not self.cons_per_step:
                    LOGGER.debug("KL adversary: %f / %f, converged iteration %d", kl_div,
                                 kl_step, itr)
                else:
                    LOGGER.debug(
                            "KL adversary: %f / %f, converged iteration %d",
                            np.mean(kl_div[:-1]), np.mean(kl_step[:-1]), itr
                    )
                break

            if not self.cons_per_step:
                # Choose new eta (bisect bracket or multiply by constant)
                if con < 0: # Eta was too big.
                    max_eta = eta
                    geom = np.sqrt(min_eta*max_eta)  # Geometric mean.
                    new_eta = max(geom, 0.1*max_eta)
                    LOGGER.debug("KL adversary: %f / %f, eta too big, new eta: %f",
                                 kl_div, kl_step, new_eta)
                else: # Eta was too small.
                    min_eta = eta
                    geom = np.sqrt(min_eta*max_eta)  # Geometric mean.
                    new_eta = min(geom, 10.0*min_eta)
                    LOGGER.debug("KL adversary: %f / %f, eta too small, new eta: %f",
                                 kl_div, kl_step, new_eta)
                # Logarithmic mean: log_mean(x,y) = (y - x)/(log(y) - log(x))
                eta = new_eta
            else:
                for t in range(T):
                    if con[t] < 0:
                        max_eta[t] = eta[t]
                        geom = np.sqrt(min_eta[t]*max_eta[t])
                        eta[t] = max(geom, 0.1*max_eta[t])
                    else:
                        min_eta[t] = eta[t]
                        geom = np.sqrt(min_eta[t]*max_eta[t])
                        eta[t] = min(geom, 10.0*min_eta[t])
                if itr % 10 == 0:
                    LOGGER.debug("avg KL: %f / %f, avg new eta: %f",
                                 np.mean(kl_div[:-1]), np.mean(kl_step[:-1]),
                                 np.mean(eta[:-1]))

        if (self.cons_per_step and not self._conv_check(con, kl_step)):
            m_b, v_b = np.zeros(T-1), np.zeros(T-1)

            for itr in range(DGD_MAX_GD_ITER):
                traj_distr, eta = self.backward_adversary(prev_traj_distr, traj_info,
                                                eta, algorithm, m)

                if not self._use_prev_distr:
                    new_mu, new_sigma = self.forward_adversary(traj_distr, traj_info)
                    kl_div = traj_distr_kl(
                            new_mu, new_sigma, traj_distr, prev_traj_distr,
                            tot=False
                    )
                else:
                    prev_mu, prev_sigma = self.forward_adversary(prev_traj_distr,
                                                       traj_info)
                    kl_div = traj_distr_kl_alt(
                            prev_mu, prev_sigma, traj_distr, prev_traj_distr,
                            tot=False
                    )

                con = kl_div - kl_step
                if self._conv_check(con, kl_step):
                    LOGGER.debug(
                            "KL: %f / %f, converged iteration %d",
                            np.mean(kl_div[:-1]), np.mean(kl_step[:-1]), itr
                    )
                    break

                m_b = (BETA1 * m_b + (1-BETA1) * con[:-1])
                m_u = m_b / (1 - BETA1 ** (itr+1))
                v_b = (BETA2 * v_b + (1-BETA2) * np.square(con[:-1]))
                v_u = v_b / (1 - BETA2 ** (itr+1))
                eta[:-1] = np.minimum(
                        np.maximum(eta[:-1] + ALPHA * m_u / (np.sqrt(v_u) + EPS),
                                   self._hyperparams['min_eta']),
                        self._hyperparams['max_eta']
                )

                if itr % 10 == 0:
                    LOGGER.debug("avg KL: %f / %f, avg new eta: %f",
                                 np.mean(kl_div[:-1]), np.mean(kl_step[:-1]),
                                 np.mean(eta[:-1]))

        if (np.mean(kl_div) > np.mean(kl_step) and
            not self._conv_check(con, kl_step)):
            LOGGER.warning(
                    "Final KL divergence after DGD convergence is too high."
            )
        return traj_distr, eta

    def update_robust(self, m, algorithm):
        # obtain protagonist trajectory  and eta
        # LOGGER.debug("updating protagonist trajectory")
        # traj_prot, eta_prot = self.update_protagonist(m, algorithm)
        #
        # LOGGER.debug("updating adversary trajectory")
        # traj_adv, eta_adv = self.update_adversary(m, algorithm)

        LOGGER.debug("Computing conditional of protagonist on adversary")

    def estimate_cost(self, traj_distr, traj_info):
        """ Compute Laplace approximation to expected cost. """
        # Constants.
        T = traj_distr.T

        # Perform forward pass (note that we repeat this here, because
        # traj_info may have different dynamics from the ones that were
        # used to compute the distribution already saved in traj).
        mu, sigma = self.forward(traj_distr, traj_info)

        # Compute cost.
        predicted_cost = np.zeros(T)
        for t in range(T):
            predicted_cost[t] = traj_info.cc[t] + 0.5 * \
                    np.sum(sigma[t, :, :] * traj_info.Cm[t, :, :]) + 0.5 * \
                    mu[t, :].T.dot(traj_info.Cm[t, :, :]).dot(mu[t, :]) + \
                    mu[t, :].T.dot(traj_info.cv[t, :])
        return predicted_cost

    def forward(self, traj_distr, traj_info):
        """
        Perform LQR forward pass. Computes state-action marginals from
        dynamics and policy.
        Args:
            traj_distr: A linear Gaussian policy object.
            traj_info: A TrajectoryInfo object.
        Returns:
            mu: A T x dX mean action vector.
            sigma: A T x dX x dX covariance matrix.
        """
        # Compute state-action marginals from specified conditional
        # parameters and current traj_info.
        T = traj_distr.T
        dU = traj_distr.dU
        dX = traj_distr.dX

        # Constants.
        idx_x = slice(dX)

        # Allocate space.
        sigma = np.zeros((T, dX+dU, dX+dU))
        mu = np.zeros((T, dX+dU))

        # Pull out dynamics.
        Fm = traj_info.dynamics.Fm
        fv = traj_info.dynamics.fv
        dyn_covar = traj_info.dynamics.dyn_covar

        # Set initial covariance (initial mu is always zero).
        sigma[0, idx_x, idx_x] = traj_info.x0sigma
        mu[0, idx_x] = traj_info.x0mu

        for t in range(T):
            sigma[t, :, :] = np.vstack([
                np.hstack([
                    sigma[t, idx_x, idx_x],
                    sigma[t, idx_x, idx_x].dot(traj_distr.K[t, :, :].T)
                ]),
                np.hstack([
                    traj_distr.K[t, :, :].dot(sigma[t, idx_x, idx_x]),
                    traj_distr.K[t, :, :].dot(sigma[t, idx_x, idx_x]).dot(
                        traj_distr.K[t, :, :].T
                    ) + traj_distr.pol_covar[t, :, :]
                ])
            ])
            mu[t, :] = np.hstack([
                mu[t, idx_x],
                traj_distr.K[t, :, :].dot(mu[t, idx_x]) + traj_distr.k[t, :]
            ])
            if t < T - 1:
                sigma[t+1, idx_x, idx_x] = \
                        Fm[t, :, :].dot(sigma[t, :, :]).dot(Fm[t, :, :].T) + \
                        dyn_covar[t, :, :]
                mu[t+1, idx_x] = Fm[t, :, :].dot(mu[t, :]) + fv[t, :]
        return mu, sigma

    def forward_protagonist(self, traj_distr, traj_info):
        """
        Perform LQR forward pass. Computes state-action marginals from
        dynamics and policy.
        Args:
            traj_distr: A linear Gaussian policy object.
            traj_info: A TrajectoryInfo object.
        Returns:
            mu: A T x dX mean action vector.
            sigma: A T x dX x dX covariance matrix.
        """
        # Compute state-action marginals from specified conditional
        # parameters and current traj_info.
        T = traj_distr.T
        dU = traj_distr.dU
        dX = traj_distr.dX

        # Constants.
        idx_x = slice(dX)

        # Allocate space.
        sigma = np.zeros((T, dX+dU, dX+dU))
        mu = np.zeros((T, dX+dU))

        # Pull out dynamics.
        Fm = traj_info.dynamics.Fm
        fv = traj_info.dynamics.fv
        dyn_covar = traj_info.dynamics.dyn_covar

        # Set initial covariance (initial mu is always zero).
        sigma[0, idx_x, idx_x] = traj_info.x0sigma
        mu[0, idx_x] = traj_info.x0mu

        for t in range(T):
            sigma[t, :, :] = np.vstack([
                np.hstack([
                    sigma[t, idx_x, idx_x],
                    sigma[t, idx_x, idx_x].dot(traj_distr.Gu[t, :, :].T)
                ]),
                np.hstack([
                    traj_distr.Gu[t, :, :].dot(sigma[t, idx_x, idx_x]),
                    traj_distr.Gu[t, :, :].dot(sigma[t, idx_x, idx_x]).dot(
                        traj_distr.Gu[t, :, :].T
                    ) + traj_distr.pol_covar_u[t, :, :]
                ])
            ])
            mu[t, :] = np.hstack([
                mu[t, idx_x],
                traj_distr.Gu[t, :, :].dot(mu[t, idx_x]) + traj_distr.gu[t, :]
            ])
            if t < T - 1:
                sigma[t+1, idx_x, idx_x] = \
                        Fm[t, :, :].dot(sigma[t, :, :]).dot(Fm[t, :, :].T) + \
                        dyn_covar[t, :, :]
                mu[t+1, idx_x] = Fm[t, :, :].dot(mu[t, :]) + fv[t, :]
        return mu, sigma

    def forward_adversary(self, traj_distr, traj_info):
        """
        Perform LQR forward pass. Computes state-action marginals from
        dynamics and policy.
        Args:
            traj_distr: A linear Gaussian policy object.
            traj_info: A TrajectoryInfo object.
        Returns:
            mu: A T x dX mean action vector.
            sigma: A T x dX x dX covariance matrix.
        """
        # Compute state-action marginals from specified conditional
        # parameters and current traj_info.
        T = traj_distr.T
        dU = traj_distr.dV
        dX = traj_distr.dX

        # Constants.
        idx_x = slice(dX)

        # Allocate space.
        sigma = np.zeros((T, dX+dV, dX+dV))
        mu = np.zeros((T, dX+dV))

        # Pull out dynamics.
        Fm = traj_info.dynamics.Fm
        fv = traj_info.dynamics.fv
        dyn_covar = traj_info.dynamics.dyn_covar

        # Set initial covariance (initial mu is always zero).
        sigma[0, idx_x, idx_x] = traj_info.x0sigma
        mu[0, idx_x] = traj_info.x0mu

        for t in range(T):
            sigma[t, :, :] = np.vstack([
                np.hstack([
                    sigma[t, idx_x, idx_x],
                    sigma[t, idx_x, idx_x].dot(traj_distr.Gv[t, :, :].T)
                ]),
                np.hstack([
                    traj_distr.Gv[t, :, :].dot(sigma[t, idx_x, idx_x]),
                    traj_distr.Gv[t, :, :].dot(sigma[t, idx_x, idx_x]).dot(
                        traj_distr.Gv[t, :, :].T
                    ) + traj_distr.pol_covar_v[t, :, :]
                ])
            ])
            mu[t, :] = np.hstack([
                mu[t, idx_x],
                traj_distr.Gv[t, :, :].dot(mu[t, idx_x]) + traj_distr.gv[t, :]
            ])
            if t < T - 1:
                sigma[t+1, idx_x, idx_x] = \
                        Fm[t, :, :].dot(sigma[t, :, :]).dot(Fm[t, :, :].T) + \
                        dyn_covar[t, :, :]
                mu[t+1, idx_x] = Fm[t, :, :].dot(mu[t, :]) + fv[t, :]
        return mu, sigma

    def backward(self, prev_traj_distr, traj_info, eta, algorithm, m):
        """
        Perform LQR backward pass. This computes a new linear Gaussian
        policy object.
        Args:
            prev_traj_distr: A linear Gaussian policy object from
                previous iteration.
            traj_info: A TrajectoryInfo object.
            eta: Dual variable.
            algorithm: Algorithm object needed to compute costs.
            m: Condition number.
        Returns:
            traj_distr: A new linear Gaussian policy.
            new_eta: The updated dual variable. Updates happen if the
                Q-function is not PD.
        """
        # Constants.
        T = prev_traj_distr.T
        dU = prev_traj_distr.dU
        dX = prev_traj_distr.dX

        if self._update_in_bwd_pass:
            traj_distr = prev_traj_distr.nans_like()
        else:
            traj_distr = prev_traj_distr.copy()

        # Store pol_wt if necessary
        if type(algorithm) == AlgorithmBADMM:
            pol_wt = algorithm.cur[m].pol_info.pol_wt

        idx_x = slice(dX)
        idx_u = slice(dX, dX+dU)

        # Pull out dynamics.
        Fm = traj_info.dynamics.Fm
        fv = traj_info.dynamics.fv

        # Non-SPD correction terms.
        del_ = self._hyperparams['del0']
        if self.cons_per_step:
            del_ = np.ones(T) * del_
        eta0 = eta

        # Run dynamic programming.
        fail = True
        while fail:
            fail = False  # Flip to true on non-symmetric PD.

            # Allocate.
            Vxx = np.zeros((T, dX, dX))
            Vx = np.zeros((T, dX))
            Qtt = np.zeros((T, dX+dU, dX+dU))
            Qt = np.zeros((T, dX+dU))

            if not self._update_in_bwd_pass:
                new_K, new_k = np.zeros((T, dU, dX)), np.zeros((T, dU))
                new_pS = np.zeros((T, dU, dU))
                new_ipS, new_cpS = np.zeros((T, dU, dU)), np.zeros((T, dU, dU))

            fCm, fcv = algorithm.compute_costs(  #from algorithm_mdgps.py#L204
                    m, eta, augment=(not self.cons_per_step)
            )

            # Compute state-action-state function at each time step.
            for t in range(T - 1, -1, -1):
                # Add in the cost.
                Qtt[t] = fCm[t, :, :]   # (X+U) x (X+U)
                # Qtt[t] += eta * np.eye(Qtt.shape[-1])
                Qt[t]  = fcv[t, :]      # (X+U) x 1
                # Qt[t] += eta * np.eye(Qt.shape[0])
                # print(Qtt[t].shape)
                # Qtt[t] = np.eye(dX+dU)
                # Qt[t] = np.eye(dX+dU)

                # Add in the value function from the next time step.
                if t < T - 1:
                    if type(algorithm) == AlgorithmBADMM:
                        multiplier = (pol_wt[t+1] + eta)/(pol_wt[t] + eta)
                    else:
                        multiplier = 1.0
                    Qtt[t] += multiplier * \
                            Fm[t, :, :].T.dot(Vxx[t+1, :, :]).dot(Fm[t, :, :])
                    Qt[t] += multiplier * \
                            Fm[t, :, :].T.dot(Vx[t+1, :] +
                                            Vxx[t+1, :, :].dot(fv[t, :]))

                # Symmetrize quadratic component.
                Qtt[t] = 0.5 * (Qtt[t] + Qtt[t].T)

                # print('Qtt[t]: ', Qtt[t])
                if np.any(np.isnan(Qtt[t, idx_u, idx_u])): # Fix Q function
                    Qtt[t, idx_u, idx_u] = np.eye(Qtt[t].shape[-1])

                if not self.cons_per_step:
                    inv_term = Qtt[t, idx_u, idx_u]  # will be 7X7
                    k_term = Qt[t, idx_u]
                    K_term = Qtt[t, idx_u, idx_x]
                else:
                    inv_term = (1.0 / eta[t]) * Qtt[t, idx_u, idx_u] + \
                            prev_traj_distr.inv_pol_covar[t]
                    k_term = (1.0 / eta[t]) * Qt[t, idx_u] - \
                            prev_traj_distr.inv_pol_covar[t].dot(prev_traj_distr.k[t])
                    K_term = (1.0 / eta[t]) * Qtt[t, idx_u, idx_x] - \
                            prev_traj_distr.inv_pol_covar[t].dot(prev_traj_distr.K[t])
                # Compute Cholesky decomposition of Q function action
                # component.
                try:
                    U = sp.linalg.cholesky(inv_term)
                    L = U.T
                except LinAlgError as e:
                    # Error thrown when Qtt[idx_u, idx_u] is not
                    # symmetric positive definite.
                    LOGGER.debug('LinAlgError: %s', e)
                    fail = t if self.cons_per_step else True
                    break

                if self._hyperparams['update_in_bwd_pass']:
                    # Store conditional covariance, inverse, and Cholesky.
                    traj_distr.inv_pol_covar[t, :, :] = inv_term
                    traj_distr.pol_covar[t, :, :] = sp.linalg.solve_triangular(
                        U, sp.linalg.solve_triangular(L, np.eye(dU), lower=True)
                    )
                    traj_distr.chol_pol_covar[t, :, :] = sp.linalg.cholesky(
                        traj_distr.pol_covar[t, :, :]
                    )

                    # Compute mean terms.
                    traj_distr.k[t, :] = -sp.linalg.solve_triangular(
                        U, sp.linalg.solve_triangular(L, k_term, lower=True)
                    )
                    traj_distr.K[t, :, :] = -sp.linalg.solve_triangular(
                        U, sp.linalg.solve_triangular(L, K_term, lower=True)
                    )
                else:
                    # Store conditional covariance, inverse, and Cholesky.
                    new_ipS[t, :, :] = inv_term
                    new_pS[t, :, :] = sp.linalg.solve_triangular(
                        U, sp.linalg.solve_triangular(L, np.eye(dU), lower=True)
                    )
                    new_cpS[t, :, :] = sp.linalg.cholesky(
                        new_pS[t, :, :]
                    )

                    # Compute mean terms.
                    new_k[t, :] = -sp.linalg.solve_triangular(
                        U, sp.linalg.solve_triangular(L, k_term, lower=True)
                    )
                    new_K[t, :, :] = -sp.linalg.solve_triangular(
                        U, sp.linalg.solve_triangular(L, K_term, lower=True)
                    )

                # Compute value function.
                if (self.cons_per_step or
                    not self._hyperparams['update_in_bwd_pass']):
                    Vxx[t, :, :] = Qtt[t, idx_x, idx_x] + \
                            traj_distr.K[t].T.dot(Qtt[t, idx_u, idx_u]).dot(traj_distr.K[t]) + \
                            (2 * Qtt[t, idx_x, idx_u]).dot(traj_distr.K[t])
                    Vx[t, :] = Qt[t, idx_x].T + \
                            Qt[t, idx_u].T.dot(traj_distr.K[t]) + \
                            traj_distr.k[t].T.dot(Qtt[t, idx_u, idx_u]).dot(traj_distr.K[t]) + \
                            Qtt[t, idx_x, idx_u].dot(traj_distr.k[t])
                else:
                    Vxx[t, :, :] = Qtt[t, idx_x, idx_x] + \
                            Qtt[t, idx_x, idx_u].dot(traj_distr.K[t, :, :])
                    Vx[t, :] = Qt[t, idx_x] + \
                            Qtt[t, idx_x, idx_u].dot(traj_distr.k[t, :])
                Vxx[t, :, :] = 0.5 * (Vxx[t, :, :] + Vxx[t, :, :].T)

            if not self._hyperparams['update_in_bwd_pass']:
                traj_distr.K, traj_distr.k = new_K, new_k
                traj_distr.pol_covar = new_pS
                traj_distr.inv_pol_covar = new_ipS
                traj_distr.chol_pol_covar = new_cpS

            # Increment eta on non-SPD Q-function.
            if fail:
                if not self.cons_per_step:
                    old_eta = eta
                    eta = eta0 + del_
                    LOGGER.debug('Increasing eta: %f -> %f', old_eta, eta)
                    del_ *= 2  # Increase del_ exponentially on failure.
                else:
                    old_eta = eta[fail]
                    eta[fail] = eta0[fail] + del_[fail]
                    LOGGER.debug('Increasing eta %d: %f -> %f',
                                 fail, old_eta, eta[fail])
                    del_[fail] *= 2  # Increase del_ exponentially on failure.
                if self.cons_per_step:
                    fail_check = (eta[fail] >= 1e16)
                else:
                    fail_check = (eta >= 1e16)
                if fail_check:
                    if np.any(np.isnan(Fm)) or np.any(np.isnan(fv)):
                        raise ValueError('NaNs encountered in dynamics!')
                    raise ValueError('Failed to find PD solution even for very \
                            large eta (check that dynamics and cost are \
                            reasonably well conditioned)!')
        return traj_distr, eta

    def check_pdef(self, A):
        """
            checks if the invertible matrix is symmetric
            positive definite before cholesky LU decomposition
        """
        if np.array_equal(A, A.T) and np.all(np.linalg.eigvals(A)>0):
            # LOGGER.debug("sigma is pos. def. Computing cholesky factorization")
            return A
        else:
            # print("Regularizing inv term for positive-definiteness")
            return np.eye(A.shape[0])

    def backward_protagonist(self, prev_traj_distr, traj_info, eta, algorithm, m):
        """
        Perform LQR backward pass. This computes a new linear Gaussian
        policy object.
        Args:
            prev_traj_distr: A linear Gaussian policy object from
                previous iteration.
            traj_info: A TrajectoryInfo object.
            eta: Dual variable.
            algorithm: Algorithm object needed to compute costs.
            m: Condition number.
        Returns:
            traj_distr: A new linear Gaussian policy.
            new_eta: The updated dual variable. Updates happen if the
                Q-function is not PD.
        """
        # Constants.
        T = prev_traj_distr.T
        dU = prev_traj_distr.dU
        dV = prev_traj_distr.dV
        dX = prev_traj_distr.dX

        if self._update_in_bwd_pass:
            traj_distr = prev_traj_distr.nans_like()
        else:
            traj_distr = prev_traj_distr.copy()

        # Store pol_wt if necessary
        if type(algorithm) == AlgorithmBADMM:
            pol_wt = algorithm.cur[m].pol_info.pol_wt # note pol_wt is same for prot and adv

        idx_x = slice(dX)
        idx_u = slice(dX, dX+dU)
        idx_v = slice(dX, dX+dV)

        # Pull out dynamics.
        Fm = traj_info.dynamics.Fm
        fv = traj_info.dynamics.fv

        # Non-SPD correction terms.
        del_ = self._hyperparams['del0']
        if self.cons_per_step:
            del_ = np.ones(T) * del_
        eta0 = eta

        # Run dynamic programming.
        fail = True
        while fail:
            fail = False  # Flip to true on non-symmetric PD.

            # Allocate.
            Vxx = np.zeros((T, dX, dX))
            Vx = np.zeros((T, dX))
            Qtt = np.zeros((T, dX+dU+dV, dX+dU+dV))
            Qt = np.zeros((T, dX+dU+dV))

            PSig_u = np.zeros((T, dU, dV))  # Covariance of noise.
            cholPSig_u = np.zeros((T, dU, dU))  # Cholesky decomposition.
            invPSig_u = np.zeros((T, dU, dU))  # Inverse of covariance.

            PSig_v = np.zeros((T, dV, dV))  # Covariance of noise.
            cholPSig_v = np.zeros((T, dV, dV))  # Cholesky decomposition.
            invPSig_v = np.zeros((T, dV, dV))  # Inverse of covariance.

            if not self._update_in_bwd_pass:
                new_Gu, new_gu = np.zeros((T, dU, dX)), np.zeros((T, dU, dU))
                new_pS = np.zeros((T, dU, dU))
                new_ipS, new_cpS = np.zeros((T, dU, dU)), np.zeros((T, dU, dU))

            fCm, fcv = algorithm.compute_costs_protagonist(  #from algorithm_mdgps.py#L204
                    m, eta, augment=(not self.cons_per_step)
            )

            # Compute state-action-state function at each time step.
            for t in range(T - 1, -1, -1):
                # Add in the cost.
                Qtt[t] = fCm[t, :, :]   # (X+U+V) x (X+U+V)
                Qt[t]  = fcv[t, :]      # (X+U+V) x 1

                # Add in the value function from the next time step.
                if t < T - 1:
                    if type(algorithm) == AlgorithmBADMM:
                        multiplier = (pol_wt[t+1] + eta)/(pol_wt[t] + eta)
                    else:
                        multiplier = 1.0
                    Qtt[t] += multiplier * \
                            Fm[t, :, :].T.dot(Vxx[t+1, :, :]).dot(Fm[t, :, :])
                    Qt[t] += multiplier * \
                            Fm[t, :, :].T.dot(Vx[t+1, :] +
                                            Vxx[t+1, :, :].dot(fv[t, :]))

                # Symmetrize quadratic component.
                Qtt[t] = 0.5 * (Qtt[t] + Qtt[t].T)

                # first find Qvv inverse and Quu inverse
                U_u = sp.linalg.cholesky(Qtt[idx_u, idx_u])
                L_u = U_u.T

                # factorize Qvv
                U_v = sp.linalg.cholesky(Qtt[idx_v, idx_v])
                L_v = U_v.T

                invPSig_u[t, :, :] = Qtt[idx_u, idx_u]
                PSig_u[t, :, :] = sp.linalg.solve_triangular(
                    U_u, sp.linalg.solve_triangular(L_u, np.eye(dU), lower=True) )
                cholPSig_u[t, :, :] = sp.linalg.cholesky(PSig_u[t, :, :])

                invPSig_v[t, :, :] = Qtt[idx_v, idx_v]
                PSig_v[t, :, :] = sp.linalg.solve_triangular(
                    U_v, sp.linalg.solve_triangular(L_v, np.eye(dV), lower=True) )
                cholPSig_v[t, :, :] = sp.linalg.cholesky(PSig_v[t, :, :])

                inv_term = np.eye(dU) - PSig_u[t, :, :].dot(Qtt[idx_u, idx_v] \
                                ).dot(PSig_v[t, :, :]).dot(Qtt[idx_u, idx_v].T).dot(\
                                Qtt[idx_u, idx_u])

                # Compute Cholesky decomposition of Q function action component.
                try:
                    inv_term_U = sp.linalg.cholesky(inv_term)
                    inv_term_L = inv_term_U.T
                except LinAlgError as e:
                    # Error thrown when Qtt[idx_u, idx_u] is not
                    # symmetric positive definite.
                    LOGGER.debug('LinAlgError: %s', e)
                    fail = t if self.cons_per_step else True
                    break

                if not self.cons_per_step:
                    # invert inv_term now via solve_triangular
                    inv_termp   = sp.linalg.solve_triangular(inv_term_U, \
                                sp.linalg.solve_triangular(inv_term_L, np.eye(dU), lower=True))
                    gu_term = inv_termp.dot(Qtt[idx_u, idx_v].dot(PSig_v[t,:,:]).dot(Qt[idx_v]) - \
                                        Qt[idx_u].T)
                    Gu_term = inv_termp.dot(Qtt[idx_u, idx_v].dot(PSig_v[t,:,:]).dot(Qtt[idx_v, idx_x]) - \
                                        Qtt[idx_u, idx_x])
                else:
                    # invert inv_term now via solve_triangular
                    inv_termp   = (1.0 / eta[t]) * sp.linalg.solve_triangular(inv_term_U, \
                                sp.linalg.solve_triangular(inv_term_L, np.eye(dU), lower=True)) + \
                                prev_traj_distr.inv_pol_covar[t]
                    gu_term = (1.0 / eta[t]) * inv_termp.dot(Qtt[idx_u, idx_v].dot(PSig_v[t,:,:]).dot(Qt[idx_v]) - \
                                        Qt[idx_u].T) - \
                                prev_traj_distr.inv_pol_covar[t].dot(prev_traj_distr.gu[t])
                    Gu_term = (1.0 / eta[t]) * inv_termp.dot(Qtt[idx_u, idx_v].dot(PSig_v[t,:,:]).dot(Qtt[idx_v, idx_x]) - \
                                        Qtt[idx_u, idx_x]) - \
                                prev_traj_distr.inv_pol_covar[t].dot(prev_traj_distr.Gu[t])

                if self._hyperparams['update_in_bwd_pass']:
                    # Store conditional covariance, inverse, and Cholesky.
                    traj_distr.inv_pol_covar_u[t, :, :] = inv_term
                    traj_distr.pol_covar_u[t, :, :] = sp.linalg.solve_triangular(inv_term_U, \
                                sp.linalg.solve_triangular(inv_term_L, np.eye(dU), lower=True))
                    traj_distr.chol_pol_covar_u[t, :, :] = sp.linalg.cholesky(
                        traj_distr.pol_covar_u[t, :, :]
                    )

                    # Compute mean terms.
                    traj_distr.gu[t, :] = sp.linalg.solve_triangular(
                        inv_term_U, sp.linalg.solve_triangular(inv_term_L, gu_term, lower=True))
                    traj_distr.Gu[t, :, :] = sp.linalg.solve_triangular(
                        inv_term_U, sp.linalg.solve_triangular(inv_term_L, Gu_term, lower=True))
                else:
                    # Store conditional covariance, inverse, and Cholesky.
                    new_ipS[t, :, :] = inv_term
                    new_pS[t, :, :] = sp.linalg.solve_triangular(
                        inv_term_U, sp.linalg.solve_triangular(inv_term_L, gu_term, lower=True))
                    new_cpS[t, :, :] = sp.linalg.cholesky(
                        new_pS[t, :, :]
                    )

                    # Compute mean terms.
                    new_gu[t, :] = sp.linalg.solve_triangular(
                        inv_term_U, sp.linalg.solve_triangular(inv_term_L, gu_term, lower=True)
                    )
                    new_Gu[t, :, :] = sp.linalg.solve_triangular(
                        inv_term_U, sp.linalg.solve_triangular(inv_term_L, Gu_term, lower=True)
                    )

                # Compute value function.
                if (self.cons_per_step or \
                    not self._hyperparams['update_in_bwd_pass']):
                    Vxx[t, :, :] =  Qtt[t, idx_x, idx_x] + \
                                    traj_distr.Gu[t, :, :].T.dot(Qtt[t, idx_u, idx_u]).dot(traj_distr.Gu[t, :, :]) + \
                                    traj_distr.Gv[t, :, :].T.dot(Qtt[t, idx_v, idx_v]).dot(traj_distr.Gv[t, :, :]) + \
                                    2 * traj_distr.Gu[t, :, :].T.dot(Qtt[t, idx_u, idx_x]) + \
                                    2 * traj_distr.Gv[t, :, :].T.dot(Qtt[t, idx_v, idx_x]) + \
                                    2 * traj_distr.Gu[t, :, :].T.dot(Qtt[t, idx_u, idx_v]).dot(traj_distr.Gv[t, :, :])

                    Vx[t, :] =  Qt[t, idx_x].T + \
                                Qt[t, idx_u].T.dot(traj_distr.Gu[t, :, :]) + \
                                Qt[t, idx_v].T.dot(traj_distr.Gv[t, :, :]) + \
                                traj_distr.gu[t, :, :].T.dot(Qtt[t, idx_u, idx_u]).dot(traj_distr.Gu[t, :, :]) + \
                                traj_distr.gv[t, :, :].T.dot(Qtt[t, idx_v, idx_v]).dot(traj_distr.Gv[t, :, :]) + \
                                traj_distr.gu[t, :, :].T.dot(Qtt[t, idx_u, idx_x]) + \
                                traj_distr.gv[t, :, :].T.dot(Qtt[t, idx_v, idx_x]) + \
                                traj_distr.gu[t, :, :].T.dot(Qtt[t, idx_u, idx_v]).dot(traj_distr.Gv[t, :, :]) + \
                                traj_distr.gv[t, :, :].T.dot(Qtt[t, idx_v, idx_u]).dot(traj_distr.Gu[t, :, :])
                else:
                    Vxx[t, :, :] =  Qtt[t, idx_x, idx_x] + \
                                    traj_distr.Gu[t, :, :].T.dot(Qtt[t, idx_u, idx_u]).dot(traj_distr.Gu[t, :, :]) + \
                                    traj_distr.Gv[t, :, :].T.dot(Qtt[t, idx_v, idx_v]).dot(traj_distr.Gv[t, :, :]) + \
                                    2 * traj_distr.Gu[t, :, :].T.dot(Qtt[t, idx_u, idx_x]) + \
                                    2 * traj_distr.Gv[t, :, :].T.dot(Qtt[t, idx_v, idx_x]) + \
                                    2 * traj_distr.Gu[t, :, :].T.dot(Qtt[t, idx_u, idx_v]).dot(traj_distr.Gv[t, :, :])

                    Vx[t, :] =  Qt[t, idx_x].T + \
                                Qt[t, idx_u].T.dot(traj_distr.Gu[t, :, :]) + \
                                Qt[t, idx_v].T.dot(traj_distr.Gv[t, :, :]) + \
                                traj_distr.gu[t, :, :].T.dot(Qtt[t, idx_u, idx_u]).dot(traj_distr.Gu[t, :, :]) + \
                                traj_distr.gv[t, :, :].T.dot(Qtt[t, idx_v, idx_v]).dot(traj_distr.Gv[t, :, :]) + \
                                traj_distr.gu[t, :, :].T.dot(Qtt[t, idx_u, idx_x]) + \
                                traj_distr.gv[t, :, :].T.dot(Qtt[t, idx_v, idx_x]) + \
                                traj_distr.gu[t, :, :].T.dot(Qtt[t, idx_u, idx_v]).dot(traj_distr.Gv[t, :, :]) + \
                                traj_distr.gv[t, :, :].T.dot(Qtt[t, idx_v, idx_u]).dot(traj_distr.Gu[t, :, :])

                Vxx[t, :, :] = 0.5 * (Vxx[t, :, :] + Vxx[t, :, :].T)

            if not self._hyperparams['update_in_bwd_pass']:
                traj_distr.Gu, traj_distr.gu = new_Gu, new_gu
                traj_distr.pol_covar_u = new_pS
                traj_distr.inv_pol_covar_u = new_ipS
                traj_distr.chol_pol_covar_u = new_cpS

            # Increment eta on non-SPD Q-function.
            if fail:
                if not self.cons_per_step:
                    old_eta = eta
                    eta = eta0 + del_
                    LOGGER.debug('Increasing eta: %f -> %f', old_eta, eta)
                    del_ *= 2  # Increase del_ exponentially on failure.
                else:
                    old_eta = eta[fail]
                    eta[fail] = eta0[fail] + del_[fail]
                    LOGGER.debug('Increasing eta %d: %f -> %f',
                                 fail, old_eta, eta[fail])
                    del_[fail] *= 2  # Increase del_ exponentially on failure.
                if self.cons_per_step:
                    fail_check = (eta[fail] >= 1e16)
                else:
                    fail_check = (eta >= 1e16)
                if fail_check:
                    if np.any(np.isnan(Fm)) or np.any(np.isnan(fv)):
                        raise ValueError('NaNs encountered in dynamics!')
                    raise ValueError('Failed to find PD solution even for very \
                            large eta (check that dynamics and cost are \
                            reasonably well conditioned)!')
        return traj_distr, eta

    def backward_adversary(self, prev_traj_distr, traj_info, eta, algorithm, m):
        """
        Perform LQR backward pass. This computes a new linear Gaussian
        policy object.
        Args:
            prev_traj_distr: A linear Gaussian policy object from
                previous iteration.
            traj_info: A TrajectoryInfo object.
            eta: Dual variable.
            algorithm: Algorithm object needed to compute costs.
            m: Condition number.
        Returns:
            traj_distr: A new linear Gaussian policy.
            new_eta: The updated dual variable. Updates happen if the
                Q-function is not PD.
        """
        # Constants.
        T = prev_traj_distr.T
        dU = prev_traj_distr.dU
        dU = prev_traj_distr.dV
        dX = prev_traj_distr.dX

        if self._update_in_bwd_pass:
            traj_distr = prev_traj_distr.nans_like()
        else:
            traj_distr = prev_traj_distr.copy()

        # Store pol_wt if necessary
        if type(algorithm) == AlgorithmBADMM:
            pol_wt = algorithm.cur[m].pol_info.pol_wt

        idx_x = slice(dX)
        idx_u = slice(dX, dX+dU)
        idx_v = slice(dX, dX+dV)

        # Pull out dynamics.
        Fm = traj_info.dynamics.Fm
        fv = traj_info.dynamics.fv

        # Non-SPD correction terms.
        del_ = self._hyperparams['del0']
        if self.cons_per_step:
            del_ = np.ones(T) * del_
        eta0 = eta

        # Run dynamic programming.
        fail = True
        while fail:
            fail = False  # Flip to true on non-symmetric PD.

            # Allocate.
            Vxx = np.zeros((T, dX, dX))
            Vx = np.zeros((T, dX))
            Qtt = np.zeros((T, dX+dU+dV, dX+dU+dV))
            Qt = np.zeros((T, dX+dU+dV))
            Kv = np.zeros((T, dU, dV))
            Kv_inner = np.zeros((T, dU, dV))

            if not self._update_in_bwd_pass:
                new_K, new_k = np.zeros((T, dU, dX)), np.zeros((T, dU))
                new_pS = np.zeros((T, dU, dU))
                new_ipS, new_cpS = np.zeros((T, dU, dU)), np.zeros((T, dU, dU))

            fCm, fcv = algorithm.compute_costs_adversary(  #from algorithm_mdgps.py#L204
                    m, eta, augment=(not self.cons_per_step)
            )

            # Compute state-action-state function at each time step.
            for t in range(T - 1, -1, -1):
                # Add in the cost.
                Qtt[t] = fCm[t, :, :]   # (X+U+V) x (X+U+V)
                Qt[t]  = fcv[t, :]      # (X+U+V) x 1

                # Add in the value function from the next time step.
                if t < T - 1:
                    if type(algorithm) == AlgorithmBADMM:
                        multiplier = (pol_wt[t+1] + eta)/(pol_wt[t] + eta)
                    else:
                        multiplier = 1.0
                    Qtt[t] += multiplier * \
                            Fm[t, :, :].T.dot(Vxx[t+1, :, :]).dot(Fm[t, :, :])
                    Qt[t] += multiplier * \
                            Fm[t, :, :].T.dot(Vx[t+1, :] +
                                            Vxx[t+1, :, :].dot(fv[t, :]))

                # Symmetrize quadratic component.
                Qtt[t] = 0.5 * (Qtt[t] + Qtt[t].T)

                # print('Qtt[t]: ', Qtt[t])
                if np.any(np.isnan(Qtt[t, idx_u, idx_u])): # Fix Q function
                    Qtt[t, idx_u, idx_u] = np.eye(Qtt[t].shape[-1])

                if not self.cons_per_step:
                    inv_term = (Qtt[t, idx_u, idx_u].dot(Qtt[t, idx_v, idx_v]) - \
                            Qtt[t, idx_u, idx_v].T.dot(Qtt[t, idx_u, idx_v]))
                    try:
                        U = sp.linalg.cholesky(inv_term)
                        L = U.T
                        Kstar = sp.linalg.solve_triangular(
                            U, sp.linalg.solve_triangular(L, np.eye(dU), lower=True)
                        )
                        gv_term = Kstar.dot(Qtt[t, idx_u].dot(Qtt[t, idx_u, idx_v])
                                            - Qtt[t, idx_u, idx_u].dot(Qtt[t, idx_v]) )
                        Gv_term = - Kstar.dot(Qtt[t, idx_u, idx_v].dot(Qtt[t, idx_u, idx_x])
                                            - Qtt[t, idx_u, idx_u].dot(Qtt[t, idx_v, idx_x]) )
                    except LinAlgError as e:
                        # Error thrown when Qtt[idx_u, idx_u] is not
                        # symmetric positive definite.
                        LOGGER.debug('LinAlgError in prot cons per step: %s', e)
                        fail = t if self.cons_per_step else True
                        break
                else:
                    inv_term = (1.0 / eta[t]) * (Qtt[t, idx_u, idx_u].dot(Qtt[t, idx_v, idx_v]) - \
                            Qtt[t, idx_u, idx_v].T.dot(Qtt[t, idx_u, idx_v])) + \
                            prev_traj_distr.inv_pol_covar_u[t]
                    try:
                        U = sp.linalg.cholesky(inv_term)
                        L = U.T
                        Kstar = sp.linalg.solve_triangular(
                            U, sp.linalg.solve_triangular(L, np.eye(dU), lower=True)
                        )
                        gv_term = (1.0 / eta[t]) * Kstar.dot(Qtt[t, idx_u].dot(Qtt[t, idx_u, idx_v])
                                            - Qtt[t, idx_u, idx_u].dot(Qtt[t, idx_v]) ) - \
                                prev_traj_distr.inv_pol_covar_v[t].dot(prev_traj_distr.gv[t])
                        Gv_term = (1.0 / eta[t]) * Kstar.dot(Qtt[t, idx_u, idx_v].dot(Qtt[t, idx_u, idx_x])
                                            - Qtt[t, idx_u, idx_u].dot(Qtt[t, idx_v, idx_x]) ) - \
                                prev_traj_distr.inv_pol_covar_v[t].dot(prev_traj_distr.Gv[t])
                    except LinAlgError as e:
                        # Error thrown when Qtt[idx_u, idx_u] is not
                        # symmetric positive definite.
                        LOGGER.debug('LinAlgError in prot backward pass w/o cons per step: %s', e)
                        fail = t if self.cons_per_step else True
                        break

                if self._hyperparams['update_in_bwd_pass']:
                    # Store conditional covariance, inverse, and Cholesky.
                    traj_distr.inv_pol_covar_v[t, :, :] = inv_term
                    traj_distr.pol_covar_v[t, :, :] = Kstar
                    traj_distr.chol_pol_covar_v[t, :, :] = sp.linalg.cholesky(
                        traj_distr.pol_covar_v[t, :, :]
                    )

                    # Compute mean terms.
                    traj_distr.gv[t, :] = sp.linalg.solve_triangular(
                        U, sp.linalg.solve_triangular(L, gu_term, lower=True)
                    )
                    traj_distr.Gv[t, :, :] = sp.linalg.solve_triangular(
                        U, sp.linalg.solve_triangular(L, Gu_term, lower=True)
                    )
                else:
                    # Store conditional covariance, inverse, and Cholesky.
                    new_ipS[t, :, :] = inv_term
                    new_pS[t, :, :] = sp.linalg.solve_triangular(
                        U, sp.linalg.solve_triangular(L, np.eye(dU), lower=True)
                    )
                    new_cpS[t, :, :] = sp.linalg.cholesky(
                        new_pS[t, :, :]
                    )

                    # Compute mean terms.
                    new_gv[t, :] = sp.linalg.solve_triangular(
                        U, sp.linalg.solve_triangular(L, gv_term, lower=True)
                    )
                    new_Gv[t, :, :] = -sp.linalg.solve_triangular(
                        U, sp.linalg.solve_triangular(L, Gv_term, lower=True)
                    )

                # Compute value function.
                if (self.cons_per_step or
                    not self._hyperparams['update_in_bwd_pass']):
                    Kv_inner[t, :, :]  = (Qtt[t, idx_u, idx_v].T.dot(Qtt[t, idx_x, idx_x]).dot(Qtt[t, idx_u, idx_v]))
                    U_kv = sp.linalg.cholesky(Kv_inner[t, :, :])
                    L_kv = U_kv.T
                    Kv[t, :, :]   =  sp.linalg.solve_triangular(
                        U_kv, sp.linalg.solve_triangular(L_kv, np.eye(dU), lower=True)
                    )
                    Vxx[t, :, :] = -Kv[t,:,:].dot(
                                    (Qtt[t, idx_u, idx_v].T.dot(Qtt[t, idx_x, idx_x]).dot(Qtt[t, idx_u, idx_v])) - \
                                    (2 * Qtt[t, idx_u, idx_v].T.dot(Qtt[t, idx_u, idx_x]).dot(Qtt[t, idx_v, idx_x])) + \
                                    (Qtt[t, idx_u, idx_x].T.dot(Qtt[t, idx_v, idx_v]).dot(Qtt[t, idx_u, idx_x])) +
                                    (Qtt[t, idx_v, idx_x].T.dot(Qtt[t, idx_u, idx_u]).dot(Qtt[t, idx_v, idx_x])) -
                                    (Qtt[t, idx_u, idx_u].T.dot(Qtt[t, idx_v, idx_v]).dot(Qtt[t, idx_x, idx_x]))
                                    )

                    Vx[t, :] = -Kv[t,:,:].dot(
                                2*(Qtt[t, idx_v, idx_v].T.dot(Qtt[t, idx_u]).dot(Qtt[t, idx_u, idx_x])) - \
                                2*(Qtt[t, idx_u].T.dot(Qtt[t, idx_u, idx_v]).dot(Qtt[t, idx_v, idx_x])) + \
                                (Qtt[t, idx_u, idx_v].T.dot(Qtt[t, idx_x]).dot(Qtt[t, idx_u, idx_v])) - \
                                2*(Qtt[t, idx_u, idx_v].T.dot(Qtt[t, idx_u, idx_x]).dot(Qtt[t, idx_v])) + \
                                2*(Qtt[t, idx_u, idx_u].T.dot(Qtt[t, idx_v]).dot(Qtt[t, idx_v, idx_x])) - \
                                2*(Qtt[t, idx_u, idx_u].T.dot(Qtt[t, idx_v, idx_v]).dot(Qtt[t, idx_x]))
                                )
                else:
                    Vxx[t, :, :] = -Kv[t,:,:].dot(
                                    (Qtt[t, idx_u, idx_v].T.dot(Qtt[t, idx_x, idx_x]).dot(Qtt[t, idx_u, idx_v])) - \
                                    (2 * Qtt[t, idx_u, idx_v].T.dot(Qtt[t, idx_u, idx_x]).dot(Qtt[t, idx_v, idx_x])) + \
                                    (Qtt[t, idx_u, idx_x].T.dot(Qtt[t, idx_v, idx_v]).dot(Qtt[t, idx_u, idx_x])) +
                                    (Qtt[t, idx_v, idx_x].T.dot(Qtt[t, idx_u, idx_u]).dot(Qtt[t, idx_v, idx_x])) -
                                    (Qtt[t, idx_u, idx_u].T.dot(Qtt[t, idx_v, idx_v]).dot(Qtt[t, idx_x, idx_x]))
                                    )

                    Vx[t, :] = -Kv[t,:,:].dot(
                                2*(Qtt[t, idx_v, idx_v].T.dot(Qtt[t, idx_u]).dot(Qtt[t, idx_u, idx_x])) - \
                                2*(Qtt[t, idx_u].T.dot(Qtt[t, idx_u, idx_v]).dot(Qtt[t, idx_v, idx_x])) + \
                                (Qtt[t, idx_u, idx_v].T.dot(Qtt[t, idx_x]).dot(Qtt[t, idx_u, idx_v])) - \
                                2*(Qtt[t, idx_u, idx_v].T.dot(Qtt[t, idx_u, idx_x]).dot(Qtt[t, idx_v])) + \
                                2*(Qtt[t, idx_u, idx_u].T.dot(Qtt[t, idx_v]).dot(Qtt[t, idx_v, idx_x])) - \
                                2*(Qtt[t, idx_u, idx_u].T.dot(Qtt[t, idx_v, idx_v]).dot(Qtt[t, idx_x]))
                                )
                Vxx[t, :, :] = 0.5 * (Vxx[t, :, :] + Vxx[t, :, :].T)

            if not self._hyperparams['update_in_bwd_pass']:
                traj_distr.Gv, traj_distr.gv = new_Gv, new_gv
                traj_distr.pol_covar_v = new_pS
                traj_distr.inv_pol_covar_v = new_ipS
                traj_distr.chol_pol_covar_v = new_cpS

            # Increment eta on non-SPD Q-function.
            if fail:
                if not self.cons_per_step:
                    old_eta = eta
                    eta = eta0 + del_
                    LOGGER.debug('Increasing eta: %f -> %f', old_eta, eta)
                    del_ *= 2  # Increase del_ exponentially on failure.
                else:
                    old_eta = eta[fail]
                    eta[fail] = eta0[fail] + del_[fail]
                    LOGGER.debug('Increasing eta %d: %f -> %f',
                                 fail, old_eta, eta[fail])
                    del_[fail] *= 2  # Increase del_ exponentially on failure.
                if self.cons_per_step:
                    fail_check = (eta[fail] >= 1e16)
                else:
                    fail_check = (eta >= 1e16)
                if fail_check:
                    if np.any(np.isnan(Fm)) or np.any(np.isnan(fv)):
                        raise ValueError('NaNs encountered in dynamics!')
                    raise ValueError('Failed to find PD solution even for very \
                            large eta (check that dynamics and cost are \
                            reasonably well conditioned)!')
        return traj_distr, eta

    def backward_robust(self, prev_traj_distr, traj_info, eta, algorithm, m):
        """
        Perform LQR backward pass. This computes a new linear Gaussian
        policy object.
        Args:
            prev_traj_distr: A linear Gaussian policy object from
                previous iteration.
            traj_info: A TrajectoryInfo object.
            eta: Dual variable.
            algorithm: Algorithm object needed to compute costs.
            m: Condition number.
        Returns:
            traj_distr: A new linear Gaussian policy.
            new_eta: The updated dual variable. Updates happen if the
                Q-function is not PD.
        """
        # Constants.
        T = prev_traj_distr.T
        dU = prev_traj_distr.dU
        dV = prev_traj_distr.dV
        dX = prev_traj_distr.dX

        if self._update_in_bwd_pass:
            traj_distr = prev_traj_distr.nans_like()
        else:
            traj_distr = prev_traj_distr.copy()

        # Store pol_wt if necessary
        if type(algorithm) == AlgorithmBADMM:
            pol_wt = algorithm.cur[m].pol_info.pol_wt

        idx_x = slice(dX)
        idx_u = slice(dX, dX+dU)
        idx_v = slice(dX, dX+dV)

        # Pull out dynamics.
        Fm = traj_info.dynamics.Fm
        fv = traj_info.dynamics.fv

        # Non-SPD correction terms.
        del_ = self._hyperparams['del0']
        if self.cons_per_step:
            del_ = np.ones(T) * del_
        eta0 = eta

        # Run dynamic programming.
        fail = True
        while fail:
            fail = False  # Flip to true on non-symmetric PD.

            # Allocate.
            Vxx = np.zeros((T, dX, dX))
            Vx = np.zeros((T, dX))
            Qtt = np.zeros((T, dX+dU+dV, dX+dU+dV))
            Qt = np.zeros((T, dX+dU+dV))

            if not self._update_in_bwd_pass:
                new_K, new_k = np.zeros((T, dU, dX)), np.zeros((T, dU))
                new_pS = np.zeros((T, dU, dU))
                new_ipS, new_cpS = np.zeros((T, dU, dU)), np.zeros((T, dU, dU))

            fCm, fcv = algorithm.compute_costs_robust(  #from algorithm_mdgps.py#L204
                    m, eta, augment=(not self.cons_per_step)
            )

            # Compute state-action-adversarial-state function at each time step.
            for t in range(T - 1, -1, -1):
                # Add in the cost.
                Qtt[t] = fCm[t, :, :]   # (X+U) x (X+U)
                Qt[t]  = fcv[t, :]      # (X+U) x 1

                # Add in the value function from the next time step.
                if t < T - 1:
                    if type(algorithm) == AlgorithmBADMM:
                        multiplier = (pol_wt[t+1] + eta)/(pol_wt[t] + eta)
                    else:
                        multiplier = 1.0
                    Qtt[t] += multiplier * \
                            Fm[t, :, :].T.dot(Vxx[t+1, :, :]).dot(Fm[t, :, :])
                    Qt[t] += multiplier * \
                            Fm[t, :, :].T.dot(Vx[t+1, :] +
                                            Vxx[t+1, :, :].dot(fv[t, :]))

                # Symmetrize quadratic component.
                Qtt[t] = 0.5 * (Qtt[t] + Qtt[t].T)

                # print('Qtt[t]: ', Qtt[t])
                if np.any(np.isnan(Qtt[t, idx_v, idx_v])): # Fix Q function
                    Qtt[t, idx_v, idx_v] = np.eye(Qtt[t].shape[-1])

                if not self.cons_per_step:
                    # inv_term = Qtt[t, idx_v, idx_v]  # will be 7X7
                    # k_term = Qt[t, idx_u]
                    # K_term = Qtt[t, idx_u, idx_x]
                    # Guu_inv_term = - Qtt[idx_u, idx_u]
                    # Gvv_inv_term = - Qtt[idx_v, idx_v]
                    Kstar = Qtt[t, idx_u, idx_u].dot(Qtt_t[t, idx_v, idx_v]) - \
                            Qtt[t, idx_u, idx_v].dot(Qtt[t, idx_u, idx_v])
                    U_Kstar = sp.linalg.cholesky(Kstar)
                    L_Kstar = U_Kstar.T
                    InvKstar = sp.linalg.solve_triangular(
                            U_Kstar, sp.linalg.solve_triangular(L_Kstar, np.eye(dU), lower=True)
                        )

                    Guu = - Qtt[idx_u, idx_u] .dot(Qtt[idx_u, idx_x])
                    G_term = Qtt[id_u, idx_v].dot(PSigv.dot(
                                                np.eye(dV) + Qtt_t[idx_v, idx_u] )
                                            )
                else:
                    inv_term = (1.0 / eta[t]) * Qtt[t, idx_u, idx_u] + \
                            prev_traj_distr.inv_pol_covar[t]
                    k_term = (1.0 / eta[t]) * Qt[t, idx_u] - \
                            prev_traj_distr.inv_pol_covar[t].dot(prev_traj_distr.k[t])
                    K_term = (1.0 / eta[t]) * Qtt[t, idx_u, idx_x] - \
                            prev_traj_distr.inv_pol_covar[t].dot(prev_traj_distr.K[t])
                # Compute Cholesky decomposition of Q function action
                # component.
                try:
                    U = sp.linalg.cholesky(inv_term)
                    L = U.T
                except LinAlgError as e:
                    # Error thrown when Qtt[idx_u, idx_u] is not
                    # symmetric positive definite.
                    LOGGER.debug('LinAlgError: %s', e)
                    fail = t if self.cons_per_step else True
                    break

                if self._hyperparams['update_in_bwd_pass']:
                    # Store conditional covariance, inverse, and Cholesky.
                    traj_distr.inv_pol_covar[t, :, :] = inv_term
                    traj_distr.pol_covar[t, :, :] = sp.linalg.solve_triangular(
                        U, sp.linalg.solve_triangular(L, np.eye(dU), lower=True)
                    )
                    traj_distr.chol_pol_covar[t, :, :] = sp.linalg.cholesky(
                        traj_distr.pol_covar[t, :, :]
                    )

                    # Compute mean terms.
                    traj_distr.k[t, :] = -sp.linalg.solve_triangular(
                        U, sp.linalg.solve_triangular(L, k_term, lower=True)
                    )
                    traj_distr.K[t, :, :] = -sp.linalg.solve_triangular(
                        U, sp.linalg.solve_triangular(L, K_term, lower=True)
                    )
                else:
                    # Store conditional covariance, inverse, and Cholesky.
                    new_ipS[t, :, :] = inv_term
                    new_pS[t, :, :] = sp.linalg.solve_triangular(
                        U, sp.linalg.solve_triangular(L, np.eye(dU), lower=True)
                    )
                    new_cpS[t, :, :] = sp.linalg.cholesky(
                        new_pS[t, :, :]
                    )

                    # Compute mean terms.
                    new_k[t, :] = -sp.linalg.solve_triangular(
                        U, sp.linalg.solve_triangular(L, k_term, lower=True)
                    )
                    new_K[t, :, :] = -sp.linalg.solve_triangular(
                        U, sp.linalg.solve_triangular(L, K_term, lower=True)
                    )

                # Compute value function.
                if (self.cons_per_step or
                    not self._hyperparams['update_in_bwd_pass']):
                    Vxx[t, :, :] = Qtt[t, idx_x, idx_x] + \
                            traj_distr.K[t].T.dot(Qtt[t, idx_u, idx_u]).dot(traj_distr.K[t]) + \
                            (2 * Qtt[t, idx_x, idx_u]).dot(traj_distr.K[t])
                    Vx[t, :] = Qt[t, idx_x].T + \
                            Qt[t, idx_u].T.dot(traj_distr.K[t]) + \
                            traj_distr.k[t].T.dot(Qtt[t, idx_u, idx_u]).dot(traj_distr.K[t]) + \
                            Qtt[t, idx_x, idx_u].dot(traj_distr.k[t])
                else:
                    Vxx[t, :, :] = Qtt[t, idx_x, idx_x] + \
                            Qtt[t, idx_x, idx_u].dot(traj_distr.K[t, :, :])
                    Vx[t, :] = Qt[t, idx_x] + \
                            Qtt[t, idx_x, idx_u].dot(traj_distr.k[t, :])
                Vxx[t, :, :] = 0.5 * (Vxx[t, :, :] + Vxx[t, :, :].T)

            if not self._hyperparams['update_in_bwd_pass']:
                traj_distr.K, traj_distr.k = new_K, new_k
                traj_distr.pol_covar = new_pS
                traj_distr.inv_pol_covar = new_ipS
                traj_distr.chol_pol_covar = new_cpS

            # Increment eta on non-SPD Q-function.
            if fail:
                if not self.cons_per_step:
                    old_eta = eta
                    eta = eta0 + del_
                    LOGGER.debug('Increasing eta: %f -> %f', old_eta, eta)
                    del_ *= 2  # Increase del_ exponentially on failure.
                else:
                    old_eta = eta[fail]
                    eta[fail] = eta0[fail] + del_[fail]
                    LOGGER.debug('Increasing eta %d: %f -> %f',
                                 fail, old_eta, eta[fail])
                    del_[fail] *= 2  # Increase del_ exponentially on failure.
                if self.cons_per_step:
                    fail_check = (eta[fail] >= 1e16)
                else:
                    fail_check = (eta >= 1e16)
                if fail_check:
                    if np.any(np.isnan(Fm)) or np.any(np.isnan(fv)):
                        raise ValueError('NaNs encountered in dynamics!')
                    raise ValueError('Failed to find PD solution even for very \
                            large eta (check that dynamics and cost are \
                            reasonably well conditioned)!')
        return traj_distr, eta


    def _conv_check(self, con, kl_step):
        """Function that checks whether dual gradient descent has converged."""
        if self.cons_per_step:
            return all([abs(con[t]) < (0.1*kl_step[t]) for t in range(con.size)])
        return abs(con) < 0.1 * kl_step
