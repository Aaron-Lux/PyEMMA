__author__ = 'wehmeyer, mey, paul'

import numpy as _np
from six.moves import range
from pyemma._base.estimator import Estimator as _Estimator
from pyemma.thermo.models.multi_therm import MultiThermModel as _MultiThermModel
from pyemma.msm import MSM as _MSM
from pyemma.util import types as _types
from msmtools.estimation import largest_connected_set as _largest_connected_set
import sys
try:
    from thermotools import tram as _tram
    from thermotools import tram_direct as _tram_direct
    from thermotools import mbar as _mbar
    from thermotools import util as _util
except ImportError:
    pass

class TRAM(_Estimator, _MultiThermModel):
    def __init__(self, lag=1, ground_state=None, count_mode='sliding',
                 dt_traj='1 step', maxiter=1000, maxerr=1e-5, call_back=None):
        self.lag = lag
        self.ground_state = ground_state
        self.count_mode = count_mode
        self.dt_traj = dt_traj
        self.maxiter = maxiter
        self.maxerr = maxerr
        # set cset variable
        self.model_active_set = None
        # set iteration variables
        self.biased_conf_energies = None
        self.log_lagrangian_mult = None
        self.call_back = call_back
        self.initialization = 'MBAR'

    def _estimate(self, trajs):
        """
        Parameters
        ----------
        trajs : ndarray(X, 2+T) or list of ndarray(X_i, 2+T)
            Thermodynamic trajectories. Each trajectory is a (X_i, 2+T)-array
            with X_i time steps. The first column is the thermodynamic state
            index, the second column is the configuration state index.
        """
        # format input if needed
        if isinstance(trajs, _np.ndarray):
            trajs = [trajs]
        # validate input
        assert _types.is_list(trajs)
        for ttraj in trajs:
            _types.assert_array(ttraj, ndim=2, kind='f')
            assert _np.shape(ttraj)[1] > 2 # TODO: make strict test
            
        # find dimensions
        self.nstates_full = int(max(_np.max(ttraj[:, 1]) for ttraj in trajs))+1
        self.nthermo = int(max(_np.max(ttraj[:, 0]) for ttraj in trajs))+1
        #print 'M,T:', self.nstates_full, self.nthermo

        # find state visits and dimensions
        self.state_counts_full = _util.state_counts(trajs)
        self.nstates_full = self.state_counts_full.shape[1]
        self.nthermo = self.state_counts_full.shape[0]

        # count matrices
        self.count_matrices_full = _util.count_matrices(
            [_np.ascontiguousarray(t[:, :2]).astype(_np.intc) for t in trajs], self.lag,
            sliding=self.count_mode, sparse_return=False, nstates=self.nstates_full)

        # restrict to connected set
        C_sum = self.count_matrices_full.sum(axis=0)
        # TODO: report fraction of lost counts
        cset = _largest_connected_set(C_sum, directed=True)
        self.active_set = cset
        # correct counts
        self.count_matrices = self.count_matrices_full[:, cset[:, _np.newaxis], cset]
        self.count_matrices = _np.require(self.count_matrices, dtype=_np.intc ,requirements=['C', 'A'])
        state_counts = self.state_counts_full[:, cset]
        state_counts = _np.require(state_counts, dtype=_np.intc, requirements=['C', 'A'])
        # create flat bias energy arrays
        state_sequence_full = None
        bias_energy_sequence_full = None
        for traj in trajs:
            if state_sequence_full is None and bias_energy_sequence_full is None:
                state_sequence_full = traj[:, :2]
                bias_energy_sequence_full = traj[:, 2:]
            else:
                state_sequence_full = _np.concatenate(
                    (state_sequence_full, traj[:, :2]), axis=0)
                bias_energy_sequence_full = _np.concatenate(
                    (bias_energy_sequence_full, traj[:, 2:]), axis=0)
        state_sequence_full = _np.ascontiguousarray(state_sequence_full.astype(_np.intc))
        bias_energy_sequence_full = _np.ascontiguousarray(
            bias_energy_sequence_full.astype(_np.float64).transpose())
        state_sequence, bias_energy_sequence = _util.restrict_samples_to_cset(
            state_sequence_full, bias_energy_sequence_full, self.active_set)
        
        # self-test
        assert _np.all(_np.bincount(state_sequence[:, 1]) == state_counts.sum(axis=0))
        assert _np.all(_np.bincount(state_sequence[:, 0]) == state_counts.sum(axis=1))
        assert _np.all(state_counts >= _np.maximum(self.count_matrices.sum(axis=1), self.count_matrices.sum(axis=2)))

        # initialize with MBAR
        if self.initialization == 'MBAR' and self.biased_conf_energies is None:
            # run MBAR for a few steps
            print >>sys.stderr, 'running MBAR'
            self.mbar_result  = _mbar.estimate(state_counts.sum(axis=1), bias_energy_sequence,
                                               _np.ascontiguousarray(state_sequence[:, 1]),
                                               maxiter=100, maxerr=1.0E-8)
            therm_energies, _, mbar_biased_conf_energies = self.mbar_result
            self.biased_conf_energies = mbar_biased_conf_energies
            print 'therm energies:', therm_energies
            print 'done'
            
            # adapt the Lagrange multiplers to this result
            if False:
                log_lagrangian_mult = _np.zeros(shape=state_counts.shape, dtype=_np.float64)
                scratch_M = _np.zeros(shape=state_counts.shape[1], dtype=_np.float64)
                _tram.init_lagrangian_mult(self.count_matrices, log_lagrangian_mult)
                new_log_lagrangian_mult = log_lagrangian_mult.copy()
                print 'initializing Lagrange multipliers'
                for _m in range(1000):
                        _tram.update_lagrangian_mult(log_lagrangian_mult, mbar_biased_conf_energies, self.count_matrices,
                        state_counts, scratch_M, new_log_lagrangian_mult)
                        nz = _np.where(_np.logical_and(new_log_lagrangian_mult>-30,
                                                       log_lagrangian_mult>-30))
                        if _np.max(_np.abs(new_log_lagrangian_mult[nz] - log_lagrangian_mult[nz])) < self.maxerr:
                            break
                        log_lagrangian_mult[:] = new_log_lagrangian_mult
                self.log_lagrangian_mult = new_log_lagrangian_mult
                print 'done'


        # run mbar to generate a good initial guess
        f_therm, f, self.biased_conf_energies = estimate(
            self.state_counts.sum(axis=1), bias_energy_sequence,
            _np.ascontiguousarray(state_sequence[:, 1]), maxiter=1000, maxerr=1.0E-8)

        # run estimator
        self.biased_conf_energies, conf_energies, therm_energies, self.log_lagrangian_mult = _tram_direct.estimate(
            self.count_matrices, state_counts, bias_energy_sequence, _np.ascontiguousarray(state_sequence[:, 1]),
            maxiter=self.maxiter, maxerr=self.maxerr,
            log_lagrangian_mult=self.log_lagrangian_mult,
            biased_conf_energies=self.biased_conf_energies,
            call_back=self.call_back)

        self.state_counts = state_counts # debug
        #return self # debug
        # compute models
        fmsms = [_tram.estimate_transition_matrix(
            self.log_lagrangian_mult, self.biased_conf_energies,
            self.count_matrices, None, K) for K in range(self.nthermo)]
        self.model_active_set = [_largest_connected_set(msm, directed=False) for msm in fmsms]
        fmsms = [_np.ascontiguousarray(
            (msm[lcc, :])[:, lcc]) for msm, lcc in zip(fmsms, self.model_active_set)]
        models = [_MSM(msm) for msm in fmsms]

        # set model parameters to self
        self.set_model_params(models=models, f_therm=therm_energies, f=conf_energies)
        # done, return estimator (+model?)
        return self

    def log_likelihood(self):
        raise Exception('not implemented')
        #return (self.state_counts * (
        #    self.f_therm[:, _np.newaxis] - self.b_K_i - self.f[_np.newaxis, :])).sum()
