""" This file defines utility classes and functions for algorithms. """
import numpy as np

from gps.utility.general_utils import BundleType
from gps.algorithm.policy.lin_gauss_policy import LinearGaussianPolicy, LinearGaussianPolicyRobust


class IterationData(BundleType):
    """ Collection of iteration variables. """
    def __init__(self):
        variables = {
            'sample_list': None,  # List of samples for the current iteration.
            'sample_list_adv': None,
            'traj_info': None,  # Current TrajectoryInfo object.
            'pol_info': None,  # Current PolicyInfo object.
            'traj_distr': None,  # Initial trajectory distribution.
            'new_traj_distr': None, # Updated trajectory distribution.
            'traj_distr_adv': None,  # Initial trajectory distribution for adversary.
            'new_traj_distr_adv': None, # Updated trajectory distribution for adversary
            'traj_distr_robust': None,  # Initial trajectory distribution for both agents.
            'new_traj_distr_robust': None, # Updated trajectory distribution for both agentsy
            'cs': None,  # Sample costs of the current iteration.
            'step_mult': 1.0,  # KL step multiplier for the current iteration.
            'eta': 1.0,  # Dual variable used in LQR backward pass.
            'eta_adv': 1.0,  # Dual variable used in LQR backward pass.
        }
        BundleType.__init__(self, variables)

class TrajectoryInfo(BundleType):
    """ Collection of trajectory-related variables. """
    def __init__(self):
        variables = {
            'dynamics': None,  # Dynamics object for the current iteration.
            'x0mu': None,  # Mean for the initial state, used by the dynamics.
            'x0sigma': None,  # Covariance for the initial state distribution.
            'cc': None,  # Cost estimate constant term.
            'cv': None,  # Cost estimate vector term.
            'Cm': None,  # Cost estimate matrix term.
            'target_distance': None, # distance from eef pts to bottom of slot
            'last_kl_step': float('inf'),  # KL step of the previous iteration.
        }
        BundleType.__init__(self, variables)

class PolicyInfo(BundleType):
    """ Collection of policy-related variables. """
    def __init__(self, hyperparams):
        self.mode = 'antagonist' #could also be antagonist

        if self.mode == 'antagonist':
            T, dU, dX = hyperparams['T'], hyperparams['dU'], hyperparams['dX']
        elif self.mode == 'protagonist':
            T, dU, dX = hyperparams['T'], hyperparams['dU'], hyperparams['dX']
        else:
            print("you have entered an invalid mode in PolicyInfo class" +
                  "in algorithm_utils.py file")

        variables = {
            'lambda_k': np.zeros((T, dU)),  # Dual variables.
            'lambda_K': np.zeros((T, dU, dX)),  # Dual variables.
            'pol_wt': hyperparams['init_pol_wt'] * np.ones(T),  # Policy weight.
            'pol_mu': None,  # Mean of the current policy output.
            'pol_sig': None,  # Covariance of the current policy output.
            'pol_K': np.zeros((T, dU, dX)),  # Policy linearization.
            'pol_k': np.zeros((T, dU)),  # Policy linearization.
            'pol_S': np.zeros((T, dU, dU)),  # Policy linearization covariance.
            'chol_pol_S': np.zeros((T, dU, dU)),  # Cholesky decomp of covar.
            'prev_kl': None,  # Previous KL divergence.
            'init_kl': None,  # The initial KL divergence, before the iteration.
            'policy_samples': [],  # List of current policy samples.
            'policy_prior': None,  # Current prior for policy linearization.
        }
        BundleType.__init__(self, variables)

    def traj_distr(self):
        """ Create a trajectory distribution object from policy info. """
        T, dU, dX = self.pol_K.shape
        # Compute inverse policy covariances.
        inv_pol_S = np.empty_like(self.chol_pol_S)
        for t in range(T):
            inv_pol_S[t, :, :] = np.linalg.solve(
                self.chol_pol_S[t, :, :],
                np.linalg.solve(self.chol_pol_S[t, :, :].T, np.eye(dU))
            )
        return LinearGaussianPolicy(self.pol_K, self.pol_k, self.pol_S,
                self.chol_pol_S, inv_pol_S)

class PolicyInfoRobust(BundleType):
    """ Collection of policy-related variables. """
    def __init__(self, hyperparams):
        T, dU, dV, dX = hyperparams['T'], hyperparams['dU'], hyperparams['dV'], hyperparams['dX']

        variables = {
            'lambda_k': np.zeros((T, dU)),  # Dual variables.
            'lambda_K': np.zeros((T, dU, dX)),  # Dual variables.
            'pol_wt': hyperparams['init_pol_wt'] * np.ones(T),  # Policy weight.
            'pol_mu': None,  # Mean of the current policy output.
            'pol_sig': None,  # Covariance of the current policy output.
            'pol_mu_prot': None,  # Mean of the current policy output.
            'pol_sig_prot': None,  # Covariance of the current policy output
            'pol_mu_adv': None,  # Mean of the current policy output.
            'pol_sig_adv': None,  # Covariance of the current policy output
            'pol_Gu': np.zeros((T, dU, dX)),  # Policy linearization.
            'pol_gu': np.zeros((T, dU)),  # Policy linearization.
            'pol_Su': np.zeros((T, dU, dU)),  # Policy linearization covariance.
            'chol_pol_Su': np.zeros((T, dU, dU)),  # Cholesky decomp of covar.
            'pol_Gv': np.zeros((T, dU, dX)),  # Policy linearization.
            'pol_gv': np.zeros((T, dU)),  # Policy linearization.
            'pol_Sv': np.zeros((T, dU, dU)),  # Policy linearization covariance.
            'chol_pol_Sv': np.zeros((T, dU, dU)),  # Cholesky decomp of covar.
            'pol_G': np.zeros((T, dU, dX)),  # Policy linearization.
            'pol_g': np.zeros((T, dU)),  # Policy linearization.
            'pol_S': np.zeros((T, dU, dU)),  # Policy linearization covariance.
            'chol_pol_S': np.zeros((T, dU, dU)),  # Cholesky decomp of covar.
            'pol_G_tilde': np.zeros((T, dU+dV, dX)),  # Policy linearization.
            'pol_g_tilde': np.zeros((T, dU+dV)),  # Policy linearization.
            'pol_S_tilde': np.zeros((T, dU+dV, dU+dV)),  # Policy linearization covariance.
            'chol_pol_S_tilde': np.zeros((T, dU+dV, dU+dV)),  # Cholesky decomp of covar.
            'prev_kl': None,  # Previous KL divergence.
            'init_kl': None,  # The initial KL divergence, before the iteration.
            'policy_samples': [],  # List of current policy samples.
            'policy_prior': None,  # Current prior for policy linearization. PolicyPriorGMM
        }
        BundleType.__init__(self, variables)

    def traj_distr(self):
        """ Create a trajectory distribution object from policy info. """
        T, dU, dX = self.pol_Gu.shape
        # Compute inverse policy covariances.
        inv_pol_Su = np.empty_like(self.chol_pol_Su)
        inv_pol_Sv = np.empty_like(self.chol_pol_Sv)
        for t in range(T):
            inv_pol_Su[t, :, :] = np.linalg.solve(
                self.chol_pol_Su[t, :, :],
                np.linalg.solve(self.chol_pol_Su[t, :, :].T, np.eye(dU))
            )
            inv_pol_Sv[t, :, :] = np.linalg.solve(
                self.chol_pol_Sv[t, :, :],
                np.linalg.solve(self.chol_pol_Sv[t, :, :].T, np.eye(dU))
            )

        return LinearGaussianPolicyRobust(self.pol_Gu, self.pol_gu, self.pol_Su,
                                            self.chol_pol_Su, inv_pol_Su,
                                        self.pol_Gv, self.pol_gv, self.pol_Sv,
                                            self.chol_pol_Sv, inv_pol_Sv)

def estimate_moments(X, mu, covar):
    """ Estimate the moments for a given linearized policy. """
    N, T, dX = X.shape
    dU = mu.shape[-1]
    if len(covar.shape) == 3:
        covar = np.tile(covar, [N, 1, 1, 1])
    Xmu = np.concatenate([X, mu], axis=2)
    ev = np.mean(Xmu, axis=0)
    em = np.zeros((N, T, dX+dU, dX+dU))
    pad1 = np.zeros((dX, dX+dU))
    pad2 = np.zeros((dU, dX))
    for n in range(N):
        for t in range(T):
            covar_pad = np.vstack([pad1, np.hstack([pad2, covar[n, t, :, :]])])
            em[n, t, :, :] = np.outer(Xmu[n, t, :], Xmu[n, t, :]) + covar_pad
    return ev, em

# computes the normal inverse Wishart prior as described in A.3
def gauss_fit_joint_prior(pts, mu0, Phi, m, n0, dwts, dX, dU, sig_reg):
    """ Perform Gaussian fit to data with a prior. """
    # Build weights matrix.
    D = np.diag(dwts)
    # Compute empirical mean and covariance.
    mun = np.sum((pts.T * dwts).T, axis=0)
    diff = pts - mun
    empsig = diff.T.dot(D).dot(diff)
    empsig = 0.5 * (empsig + empsig.T)
    # MAP estimate of joint distribution.
    N = dwts.shape[0]
    mu = mun
    sigma = (N * empsig + Phi + (N * m) / (N + m) *
             np.outer(mun - mu0, mun - mu0)) / (N + n0)
    sigma = 0.5 * (sigma + sigma.T)

    # Add sigma regularization.
    sigma += sig_reg
    # Conditioning to get dynamics.
    # print('sigma[:dX, :dX]: {}, sigma[:dX, dX:dX+dU]: {}'.format(sigma[:dX, :dX].shape, sigma[:dX, dX:dX+dU].shape))
    fd = np.linalg.solve(sigma[:dX, :dX], sigma[:dX, dX:dX+dU]).T
    fc = mu[dX:dX+dU] - fd.dot(mu[:dX])
    dynsig = sigma[dX:dX+dU, dX:dX+dU] - fd.dot(sigma[:dX, :dX]).dot(fd.T)
    dynsig = 0.5 * (dynsig + dynsig.T)
    return fd, fc, dynsig

# computes the normal inverse Wishart prior as described in A.3
def gauss_fit_joint_prior_v(pts, mu0, Phi, m, n0, dwts, dX, dV, sig_reg):
    """ Perform Gaussian fit to data with a prior. """
    # Build weights matrix.
    D = np.diag(dwts)
    # Compute empirical mean and covariance.
    mun = np.sum((pts.T * dwts).T, axis=0)
    diff = pts - mun
    empsig = diff.T.dot(D).dot(diff)
    empsig = 0.5 * (empsig + empsig.T)
    # MAP estimate of joint distribution.
    N = dwts.shape[0]
    mu = mun
    sigma = (N * empsig + Phi + (N * m) / (N + m) *
             np.outer(mun - mu0, mun - mu0)) / (N + n0)
    sigma = 0.5 * (sigma + sigma.T)

    # Add sigma regularization.
    sigma += sig_reg
    # Conditioning to get dynamics.
    fd = np.linalg.solve(sigma[:dX, :dX], sigma[:dX, dX:dX+dV]).T
    fc = mu[dX:dX+dV] - fd.dot(mu[:dX])
    dynsig = sigma[dX:dX+dV, dX:dX+dV] - fd.dot(sigma[:dX, :dX]).dot(fd.T)
    dynsig = 0.5 * (dynsig + dynsig.T)
    return fd, fc, dynsig
