"""Simulations for (real) time evolution."""
# Copyright 2020-2023 TeNPy Developers, GNU GPLv3

import numpy as np

from . import simulation
from .simulation import *
from .simulation_processing import SpectralFunctionProcessor
from ..networks.mps import MPSEnvironment, MPS, MPSEnvironmentJW
from ..tools import hdf5_io

__all__ = simulation.__all__ + ['RealTimeEvolution', 'SpectralSimulation']


class RealTimeEvolution(Simulation):
    """Perform a real-time evolution on a tensor network state.

    Parameters
    ----------
    options : dict-like
        The simulation parameters. Ideally, these options should be enough to fully specify all
        parameters of a simulation to ensure reproducibility.

    Options
    -------
    .. cfg:config :: TimeEvolution
        :include: Simulation

        final_time : float
            Mandatory. Perform time evolution until ``engine.evolved_time`` reaches this value.
            Note that we can go (slightly) beyond this time if it is not a multiple of
            the individual time steps.
    """
    default_algorithm = 'TEBDEngine'
    default_measurements = Simulation.default_measurements + [
        ('tenpy.simulations.measurement', 'm_evolved_time'),
    ]

    def __init__(self, options, **kwargs):
        super().__init__(options, **kwargs)
        self.final_time = self.options['final_time'] - 1.e-10  # subtract eps: roundoff errors

    def run_algorithm(self):
        """Run the algorithm.

        Calls ``self.engine.run()`` and :meth:`make_measurements`.
        """
        # TODO: more fine-grained/custom break criteria?
        while True:
            if np.real(self.engine.evolved_time) >= self.final_time:
                break
            self.logger.info("evolve to time %.2f, max chi=%d", self.engine.evolved_time.real,
                             max(self.psi.chi))
            self.engine.run()
            # for time-dependent H (TimeDependentExpMPOEvolution) the engine can re-init the model;
            # use it for the measurements....
            self.model = self.engine.model

            self.make_measurements()
            self.engine.checkpoint.emit(self.engine)  # TODO: is this a good idea?

    def perform_measurements(self):
        if getattr(self.engine, 'time_dependent_H', False):
            # might need to re-initialize model with current time
            # in particular for a sequential/resume run, the first `self.init_model()` might not
            # yet have had the initial start time of the algorithm engine!
            self.engine.reinit_model()
            self.model = self.engine.model
        return super().perform_measurements()

    def resume_run_algorithm(self):
        self.run_algorithm()

    def final_measurements(self):
        """Do nothing.

        We already performed a set of measurements after the evolution in :meth:`run_algorithm`.
        """
        pass


class SpectralSimulation(RealTimeEvolution):
    """A subclass of :class:`RealTimeEvolution` to specifically calculate the time
     dependent correlation function.

    Parameters
    ----------
    options : dict-like
        For command line use, a .yml file should hold the information.
        These parameters are converted to a (dict-like) :class:`~tenpy.tools.params.Config`,
        by the :class:`Simulation` parent class.
        An example of options specific to this class:
        params = {'final_time': 1,
                  'operator_t0': {'op': ['Sigmay', 'Sigmaz'], 'i': [5, 0] , 'idx_form': 'mps'},
                  'operator_t': ['op2_1_name', 'op2_2_name'], # TODO: handle custom operators (not specified by name)
                  'evolve_bra': False,
                  'addJW': True}
        Furthermore, params should hold information about the model, algorithm, etc.
        It's necessary to provide a final_time, this is inherited from the :class:`RealTimeEvolution`.
        params['operator_t0']['op']: a list of operators to apply at the given indices 'i' (they all get applied before
        the time evolution), when a more complicated operator is needed. For simple (one-site) operators simply pass
        a string, e.g.: params['operator_t0']['op'] = 'Sigmay'
        params['operator_t0']['i']: a list of indices either given in mps or lat form - when the lat form is used, the
        list must contain d+1 elements (due to the unit cell index) like e.g. for 2d systems [x, y, u]
    """
    default_measurements = RealTimeEvolution.default_measurements + [
        ('simulation_method', 'm_spectral_function'),
    ]
    post_processor = SpectralFunctionProcessor

    def __init__(self, options, *, gs_data=None, **kwargs):
        super().__init__(options, **kwargs)
        self.gs_data = gs_data  # property, calling setter method here

        self.gs_energy = None  # turn into property too?
        self.engine_ground_state = None
        # TODO: operator_t might be a list of operators, deal with fermionic default
        self.operator_t = self.options.get('operator_t', 'Sigmay')
        # generate info for operator before time evolution as subconfig
        self.operator_t0_config = self.options.subconfig('operator_t0')
        self.evolve_bra = self.options.get('evolve_bra', False)
        self.addJW = self.options.get('addJW', False)
        # TODO: How to ensure resuming from checkpoint works
        # for resuming simulation from checkpoint # this is provided in super().__init__
        resume_data = self.results.get("resume_data", None)
        if resume_data:
            if 'psi_groundstate' in self.results['simulation_parameters'].keys():
                self.psi_groundstate = self.results['simulation_parameters']['psi_groundstate']

    @property
    def gs_data(self):
        return self._gs_data

    @gs_data.setter
    def gs_data(self, data):
        if isinstance(data, MPS) or data is None:
            self._gs_data = data
            return  # skip code below
        elif isinstance(data, str):  # if path is given
            dict_data = hdf5_io.load(data)
        elif isinstance(data, dict):
            dict_data = data
        else:
            raise TypeError("Can't read out ground state MPS, make sure to supply either an instance of an MPS,\
                             a valid filename or a dictionary containing the ground state data (as an MPS).")
        # TODO: check for model params and make sure psi is valid
        self._gs_data = dict_data['psi']

    @classmethod
    def from_gs_search(cls, filename, sim_params, **kwargs):
        """Initialize an instance of a :class:`SpectralSimulation` from
        a finished run of :class:`GroundStateSearch`. This simply fetches
        the relevant parameters ('model_params', 'psi')

        Parameters
        ----------
        filename : str or dict
            The filename of the ground state search output to be loaded.
            Alternatively directly the results as dictionary.
        sim_params : dict
            The necessary simulation parameters, it is necessary to specify final_time (inherited from
            :class:`RealTimeEvolution`. The parameters of the spectral simulation should also be given similar
            to the example params in the :class:`SpectralSimulation`.

        **kwargs :
            Further keyword arguments given to :meth:`__init__` of the class :class:`SpectralSimulation`.
        """
        if isinstance(filename, dict):
            gs_results = filename
        else:
            gs_results = hdf5_io.load(filename)

        assert gs_results['version_info']['simulation_class'] == 'GroundStateSearch', "Must be loaded from a GS search"
        assert 'psi' in gs_results.keys(), "MPS for ground state not found"

        options = dict()
        options['model_class'] = gs_results['simulation_parameters']['model_class']
        options['model_params'] = gs_results['simulation_parameters']['model_params']

        for key in ['model_class', 'model_params']:
            if key in sim_params.keys():
                if options[key] != sim_params[key]:
                    # TODO: change this to output warning in logger only ?
                    raise Exception("Different Model and/or parameters for GroundStateSearch and SpectralSimulation!")
        # update dictionary parameters
        options.update(sim_params)

        sim = cls(options, **kwargs)
        # already update the attributes of the class
        sim.psi_groundstate = gs_results['psi']  # sim.psi will be constructed in init_state()
        if 'energy' in gs_results.keys():
            sim.gs_energy = gs_results['energy']
        return sim

    def init_state(self):
        # make sure state is not reinitialized if psi and psi_groundstate are given
        if not hasattr(self, 'psi_groundstate'):
            psi_gs = self.options.get('psi_groundstate', None)
            if (psi_gs is not None) and (self.gs_data is not None):
                raise KeyError("Supplied gs_data explicitly and in options")
            elif self.gs_data is not None:
                self.psi_groundstate = self.gs_data
            elif psi_gs is not None:
                assert isinstance(psi_gs, MPS), "psi must be an instance of :class:`MPS`"
                self.psi_groundstate = psi_gs
            else:
                self.logger.warning("No ground state data is supplied, calling the initial state builder on\
                                     SpectralSimulation class. You probably want to supply a ground state")
                super().init_state()  # this sets self.psi from init state builder (should be avoided)
                self.psi_groundstate = self.psi.copy()
                delattr(self, 'psi')  # free memory

        if not hasattr(self, 'psi'):
            # copy is essential, since time evolution is probably only performed on psi
            self.psi = self.psi_groundstate.copy()
            self.apply_op_list_to_psi()

        # check for saving
        if self.options.get('save_psi', False):
            self.results['psi'] = self.psi
            self.results['psi_groundstate'] = self.psi_groundstate

    def apply_op_list_to_psi(self):
        # TODO: think about segment boundary conditions
        # TODO: make JW string consistent, watch for changes in apply_local_op to have autoJW
        op_list = self._get_op_list_from_operator_t0()
        if len(op_list) == 1:
            op, i = op_list[0]
            if self.model.lat.site(i).op_needs_JW(op):
                for j in range(i):
                    self.psi.apply_local_op(j, 'JW')
            self.psi.apply_local_op(i, op)  # TODO: check if renormalize=True makes sense here
        else:
            ops, i_min, _ = self.psi._term_to_ops_list(op_list, True)  # applies JW string automatically
            for i, op in enumerate(ops):
                self.psi.apply_local_op(i_min + i, op)

    def _get_op_list_from_operator_t0(self):
        """Converts the specified operators and indices into a zipped list [(op1, i_1), (op2, i_2)]"""
        idx = self.operator_t0_config.get('i', self.psi.L // 2)
        ops = self.operator_t0_config.get('op', 'Sigmay')
        ops = [ops] if type(ops) is not list else ops  # pass ops as list
        form = self.operator_t0_config.get('idx_form', 'mps')
        assert form == 'mps' or form == 'lat', "the idx_form must be either mps or lat"
        if form == 'mps':
            idx = list(idx if type(idx) is list else [idx])
        else:
            assert type(idx) == list, "for idx_form lat, i must be given as list [x, y, u] or list of lists"
            mps_idx = self.model.lat.lat2mps_idx(idx)
            idx = list(mps_idx) if isinstance(mps_idx, np.ndarray) else [mps_idx]  # convert to mps index

        if len(ops) == len(idx):
            pass
        elif len(ops) > len(idx):
            assert len(idx) == 1, "Ill defined tiling"
            idx = idx*len(ops)
        else:
            assert len(ops) == 1, "Ill defined tiling"
            ops = ops * len(idx)

        op_list = list(zip(ops, idx))  # form [(op1, i_1), (op2, i_2)]...
        return op_list

    def init_algorithm(self, **kwargs):
        super().init_algorithm(**kwargs)  # links to RealTimeEvolution class, not to Simulation
        if self.evolve_bra is True:  # make sure second engine is used when evolving the bra
            # fetch engine that evolves ket
            AlgorithmClass = self.engine.__class__
            # instantiate the second engine for the ground state
            algorithm_params = self.options.subconfig('algorithm_params')
            self.engine_ground_state = AlgorithmClass(self.psi_groundstate, self.model, algorithm_params, **kwargs)
        else:
            # get the energy of the ground state
            if self.gs_energy is None:
                self.gs_energy = self.model.H_MPO.expectation_value(self.psi_groundstate)
        # TODO: think about checkpoints
        # TODO: resume data is handled by engine, how to pass this on to second engine?

    def run_algorithm(self):
        if self.evolve_bra is False:
            super().run_algorithm()
        else:  # Threading?
            while True:
                if np.real(self.engine.evolved_time) >= self.final_time:
                    break
                self.logger.info("evolve to time %.2f, max chi=%d", self.engine.evolved_time.real,
                                 max(self.psi.chi))

                self.engine_ground_state.run()
                self.engine.run()
                # sanity check, bra and ket should evolve to same time
                assert self.engine.evolved_time == self.engine.evolved_time, 'Bra evolved to different time than ket'
                # for time-dependent H (TimeDependentExpMPOEvolution) the engine can re-init the model;
                # use it for the measurements....
                # TODO: is this a good idea?
                self.model = self.engine.model
                self.make_measurements()
                self.engine.checkpoint.emit(self.engine)

    def prepare_results_for_save(self):
        """Wrapper around :meth:`prepare_results_for_save` of :class:`Simulation`.
        Makes it possible to include post-processing run during the run of the
        actual simulation.
        """
        if self.post_processor is not None:
            processing_params = self.options.get('post_processing_params', None)
            post_processor_cls = self.post_processor.from_simulation(self, processing_params=processing_params)
            post_processor_cls.run()
        return super().prepare_results_for_save()

    def m_spectral_function(self, results, psi, model, simulation, **kwargs):
        """Calculate the overlap <psi_0| e^{iHt} op2^j e^{-iHt} op1_idx |psi_0> between
        op1 at MPS position idx and op2 at the MPS position j"""
        self.logger.info("calling m_spectral_function")
        env = MPSEnvironment(self.psi_groundstate, self.psi)
        # TODO: get better naming convention, store this in dict ?
        if isinstance(self.operator_t, list):
            for i, op in enumerate(self.operator_t):
                if isinstance(op, str):
                    results[f'spectral_function_t_{op}'] = self._m_spectral_function_op(env, op)
                else:
                    results[f'spectral_function_t_{i}'] = self._m_spectral_function_op(env, op)
        else:
            if isinstance(self.operator_t, str):
                results[f'spectral_function_t_{self.operator_t}'] = self._m_spectral_function_op(env, self.operator_t)
            else:
                results[f'spectral_function_t'] = self._m_spectral_function_op(env, self.operator_t)

    def _m_spectral_function_op(self, env: MPSEnvironment, op):
        """Calculate the overlap of <psi| op_j |phi>, where |phi> = e^{-iHt} op1_idx |psi_0>
        (the time evolved state after op1 was applied at MPS position idx) and
        <psi| is either <psi_0| e^{iHt} (if evolve_bra is True) or e^{i E_0 t} <psi| (if evolve_bra is False).

        Returns
        ----------
        spectral_function_t : 1D array
                              representing <psi_0| e^{iHt} op2^i_j e^{-iHt} op1_idx |psi_0>
                              where op2^i is the i-th operator given in the list [op2^1, op2^2, ..., op2^N]
                              and spectral_function_t[j] corresponds to this overlap at MPS site j at time t
        """
        # TODO: case dependent if op needs JW string
        if self.addJW is False:
            spectral_function_t = env.expectation_value(op)
        else:
            spectral_function_t = []
            for i in range(self.psi.L):
                term_list, i0, _ = env._term_to_ops_list([('Id', 0), (op, i)], True)
                # this generates a list from left to right
                # ["JW", "JW", ... "JW", "op (at idx)"], the problem is, that _term_to_ops_list does not generate
                # a JW string for one operator, therefore insert Id at idx 0.
                assert i0 == 0  # make sure to really start on the left site
                spectral_function_t.append(env.expectation_value_multi_sites(term_list, i0))
                # TODO: change when :meth:`expectation_value` of :class:`MPSEnvironment` automatically handles JW-string
            spectral_function_t = np.array(spectral_function_t)

        if self.evolve_bra is False:
            phase = np.exp(1j * self.gs_energy * self.engine.evolved_time)
            spectral_function_t = spectral_function_t * phase

        return spectral_function_t


class SpectralSimulationExperimental(SpectralSimulation):
    """Improved version of :class:`SpectralSimulation`, which gives an advantage
    for calculating the correlation function of Fermions. This is done
    by calling the :class:`MPSEnvironmentJW` instead of the usual :class:`MPSEnvironment`.
    This class automatically adds a (hanging) JW string to each LP (only) when moving
    the environment to the right; otherwise the advantage of the MPS environment is lost
    (since only the overlap with the full operator string is calculated).
    """
    def __int__(self, options, *, gs_data=None, **kwargs):
        super().__init__(options, gs_data=gs_data, **kwargs)

    def m_spectral_function(self, results, psi, model, simulation, **kwargs):
        """Calculate the overlap <psi_0| e^{iHt} op2^j e^{-iHt} op1_idx |psi_0> between
        op1 at MPS position idx and op2 at the MPS position j"""
        self.logger.info("calling m_spectral_function")
        if self.addJW is False:
            env = MPSEnvironment(self.psi_groundstate, self.psi)
        else:
            env = MPSEnvironmentJW(self.psi_groundstate, self.psi)

        # TODO: get better naming convention, store this in dict ?
        if isinstance(self.operator_t, list):
            for i, op in enumerate(self.operator_t):
                if isinstance(op, str):
                    results[f'spectral_function_t_{op}'] = self._m_spectral_function_op(env, op)
                else:
                    results[f'spectral_function_t_{i}'] = self._m_spectral_function_op(env, op)
        else:
            if isinstance(self.operator_t, str):
                results[f'spectral_function_t_{self.operator_t}'] = self._m_spectral_function_op(env, self.operator_t)
            else:
                results[f'spectral_function_t'] = self._m_spectral_function_op(env, self.operator_t)

    def _m_spectral_function_op(self, env, op):  # type hint for either mps env or mps env jw
        """Calculate the overlap of <psi| op_j |phi>, where |phi> = e^{-iHt} op1_idx |psi_0>
        (the time evolved state after op1 was applied at MPS position idx) and
        <psi| is either <psi_0| e^{iHt} (if evolve_bra is True) or e^{i E_0 t} <psi| (if evolve_bra is False).

        Returns
        ----------
        spectral_function_t : 1D array
                              representing <psi_0| e^{iHt} op2^i_j e^{-iHt} op1_idx |psi_0>
                              where op2^i is the i-th operator given in the list [op2^1, op2^2, ..., op2^N]
                              and spectral_function_t[j] corresponds to this overlap at MPS site j at time t
        """
        spectral_function_t = env.expectation_value(op)
        if self.evolve_bra is False:
            phase = np.exp(1j * self.gs_energy * self.engine.evolved_time)
            spectral_function_t = spectral_function_t * phase

        return spectral_function_t
