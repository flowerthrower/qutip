# This file is part of QuTiP: Quantum Toolbox in Python.
#
#    Copyright (c) 2011 and later, Paul D. Nation and Robert J. Johansson.
#    All rights reserved.
#
#    Redistribution and use in source and binary forms, with or without
#    modification, are permitted provided that the following conditions are
#    met:
#
#    1. Redistributions of source code must retain the above copyright notice,
#       this list of conditions and the following disclaimer.
#
#    2. Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#
#    3. Neither the name of the QuTiP: Quantum Toolbox in Python nor the names
#       of its contributors may be used to endorse or promote products derived
#       from this software without specific prior written permission.
#
#    THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
#    "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
#    LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
#    PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
#    HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
#    SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
#    LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
#    DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
#    THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#    (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#    OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
###############################################################################
from collections.abc import Iterable
import warnings
from copy import deepcopy

import numpy as np
from scipy.interpolate import CubicSpline

from qutip.qobj import Qobj
from qutip.qobjevo import QobjEvo
from qutip.operators import identity
from qutip.qip.gates import expand_operator, globalphase
from qutip.tensor import tensor
from qutip.mesolve import mesolve
from qutip.qip.circuit import QubitCircuit
from qutip.qip.noise import (
    Noise, RelaxationNoise, DecoherenceNoise,
    ControlAmpNoise, RandomNoise, UserNoise, process_noise)
from qutip.qip.pulse import Pulse, Drift, _merge_qobjevo, _fill_coeff


__all__ = ['Processor']


class Processor(object):
    """
    A simulator of a quantum device based on the QuTiP solver
    :func:`qutip.mesolve`.
    It is defined by the available driving Hamiltonian and
    the decoherence time for each component systems.
    The processor can simulate the evolution under the given
    control pulses. Noisy evolution is supported by
    :class:`qutip.qip.Noise` and can be added to the processor.

    Parameters
    ----------
    N: int
        The number of component systems.

    t1: list or float, optional
        Characterize the decoherence of amplitude damping for
        each qubit. A list of size `N` or a float for all qubits.

    t2: list of float, optional
        Characterize the decoherence of dephasing for
        each qubit. A list of size `N` or a float for all qubits.

    dims: list, optional
        The dimension of each component system.
        Default value is a
        qubit system of ``dim=[2,2,2,...,2]``

    spline_kind: str, optional
        Type of the coefficient interpolation. Default is "step_func"
        Note that they have different requirement for the length of `coeff'.

        -"step_func":
        The coefficient will be treated as a step function.
        E.g. ``tlist=[0,1,2]`` and ``coeff=[3,2]``, means that the coefficient
        is 3 in t=[0,1) and 2 in t=[2,3). It requires
        ``len(coeff)=len(tlist)-1`` or ``len(coeff)=len(tlist)``, but
        in the second case the last element of `coeff` has no effect.

        -"cubic": Use cubic interpolation for the coefficient. It requires
        ``len(coeff)=len(tlist)``

    Attributes
    ----------
    N: int
        The number of component systems.

    pulses: list of :class:`qutip.qip.Pulse`
        A list of control pulses of this device

    t1: float or list
        Characterize the decoherence of amplitude damping of
        each qubit.

    t2: float or list
        Characterize the decoherence of dephasing for
        each qubit.

    noise: :class:`qutip.qip.Noise`, optional
        A list of noise objects. They will be processed when creating the
        noisy :class:`qutip.QobjEvo` from the processor or run the simulation.

    drift: :class:`qutip.qip.Drift`
        A `Drift` object representing the drift Hamiltonians.

    dims: list
        The dimension of each component system.
        Default value is a
        qubit system of ``dim=[2,2,2,...,2]``

    spline_kind: str
        Type of the coefficient interpolation.
        See parameters of :class:`qutip.qip.Processor` for details.
    """
    def __init__(self, N, t1=None, t2=None,
                 dims=None, spline_kind="step_func"):
        self.N = N
        self.pulses = []
        self.t1 = t1
        self.t2 = t2
        self.noise = []
        self.drift = Drift()
        if dims is None:
            self.dims = [2] * N
        else:
            self.dims = dims
        self.spline_kind = spline_kind

    def add_drift_ham(self, ham, targets, cyclic_permutation=False):
        """
        Add one Hamiltonian to the drift Hamiltonians

        Parameters
        ----------
        ham: :class:`qutip.Qobj`
            The drift Hamiltonian.
        targets: list
            The indices of the target qubits
            (or subquantum system of other dimensions).
        """
        if not isinstance(ham, Qobj):
            raise TypeError("The drift Hamiltonian must be a qutip.Qobj.")
        if not ham.isherm:
            raise ValueError("The drift Hamiltonian must be Hermitian.")

        num_qubits = len(ham.dims[0])
        if targets is None:
            targets = list(range(num_qubits))
        if not isinstance(targets, list):
            targets = [targets]
        if cyclic_permutation:
            for i in range(self.N):
                temp_targets = [(t + i) % self.N for t in targets]
                self.drift.add_ham(ham, temp_targets)
        else:
            self.drift.add_ham(ham, targets)

    def add_ctrl_ham(self, ham, targets=None, cyclic_permutation=False,
                     label=None):
        """
        Add a control Hamiltonian to the processor. It creates a new
        :class:`qutip.qip.Pulse`
        object for the device that is turned off
        (``tlist = None``, ``coeff = None``). To activate the pulse, one
        can set its `tlist` and `coeff`.

        Parameters
        ----------
        ham: :class:`qutip.Qobj`
            The Hamiltonian for the control pulse..

        targets: list, optional
            The indices of the target qubits
            (or subquantum system of other dimensions).

        cyclic_permutation: bool, optional
            If true, the Hamiltonian will be expanded for
            all cyclic permutation of the target qubits.

        label: str, optional
            The label (name) of the pulse
        """
        # Check validity of ctrl
        if not isinstance(ham, Qobj):
            raise TypeError("The control Hamiltonian must be a qutip.Qobj.")
        if not ham.isherm:
            raise ValueError("The control Hamiltonian must be Hermitian.")

        num_qubits = len(ham.dims[0])
        if targets is None:
            targets = list(range(num_qubits))
        if not isinstance(targets, list):
            targets = [targets]
        if cyclic_permutation:
            for i in range(self.N):
                temp_targets = [(t + i) % self.N for t in targets]
                if label is not None:
                    temp_label = label + "_" + str(temp_targets)
                temp_label = label
                self.pulses.append(
                    Pulse(ham, temp_targets, spline_kind=self.spline_kind,
                          label=temp_label))
        else:
            self.pulses.append(
                Pulse(ham, targets, spline_kind=self.spline_kind, label=label))

    @property
    def ctrls(self):
        """
        list: A list of Hamiltonian of all pulses.
        """
        result = []
        for pulse in self.pulses:
            result.append(pulse.get_ideal_qobj(self.dims))
        return result

    @property
    def coeffs(self):
        """
        A list of the coefficients for all control pulses.
        """
        if not self.pulses:
            return None
        coeffs_list = [pulse.coeff for pulse in self.pulses]
        return coeffs_list

    @coeffs.setter
    def coeffs(self, coeffs_list):
        if len(coeffs_list) != len(self.pulses):
            raise ValueError("The row number of coeffs must be same "
                             "as the number of control pulses.")
        for i, coeff in enumerate(coeffs_list):
            self.pulses[i].coeff = coeff

    def get_full_tlist(self):
        """
        Return the full tlist of the ideal pulses.
        It means that if different `tlist`s are present, they will be merged
        to one with all time points stored in a sorted array.

        Returns
        -------
        full_tlist: array-like 1d
            The full time sequence for the ideal evolution.
        """
        all_tlists = [pulse.tlist
                      for pulse in self.pulses if pulse.tlist is not None]
        if not all_tlists:
            return None
        return np.unique(np.sort(np.hstack(all_tlists)))

    def get_full_coeffs(self):
        """
        Return the full coefficients in a 2d matrix form.
        Each row corresponds to one pulse. If the `tlist` are
        different for different pulses, the length of each row
        will be same as the `full_tlist` (see method
        `get_full_tlist`). Interpolation is used for
        adding the missing coefficient according to `spline_kind`.

        Returns
        -------
        coeffs: array-like 2d
            The coefficients for all ideal pulses.
        """
        # TODO add tests
        self._is_pulses_valid()
        if not self.pulses:
            return np.array((0, 0), dtype=float)
        full_tlist = self.get_full_tlist()
        coeffs_list = []
        for pulse in self.pulses:
            if isinstance(pulse.coeff, bool):
                if pulse.coeff:
                    coeffs_list.append(np.ones(full_tlist))
                else:
                    coeffs_list.append(np.zeros(full_tlist))
            if not isinstance(pulse.coeff, np.ndarray):
                raise ValueError(
                    "get_full_coeffs only works for "
                    "NumPy array or bool coeff.")
            if self.spline_kind == "step_func":
                arg = {"_step_func_coeff": True}
                coeffs_list.append(
                    _fill_coeff(pulse.coeff, pulse.tlist, full_tlist, arg))
            elif self.spline_kind == "cubic":
                coeffs_list.append(
                    _fill_coeff(pulse.coeff, pulse.tlist, full_tlist, {}))
            else:
                raise ValueError("Unknown spline kind.")
        return np.array(coeffs_list)

    def set_all_tlist(self, tlist):
        # TODO add tests
        """
        Set `tlist` for all the pulses. It can be used to set `tlist` if
        all pulses are controlled by the same time sequence.

        Parameters
        ----------
        tlist: array-like, optional
            A list of time at which the time-dependent coefficients are
            applied. See :class:`qutip.qip.Pulse` for detailed information`
        """
        for pulse in self.pulses:
            pulse.tlist = tlist

    def add_pulse(self, pulse):
        """
        Add a new pulse to the device.

        Parameters
        ----------
        pulse: :class:`qutip.qip.Pulse`
            `Pulse` object to be added.
        """
        if isinstance(pulse, Pulse):
            self.pulses.append(pulse)
        else:
            raise ValueError("Invalid input, pulse must be a Pulse object")

    def remove_pulse(self, indices):
        """
        Remove the control pulse with given indices.

        Parameters
        ----------
        indices: int or list of int
            The indices of the control Hamiltonians to be removed.
        """
        if not isinstance(indices, Iterable):
            indices = [indices]
        indices.sort(reverse=True)
        for ind in indices:
            del self.pulses[ind]

    def _is_pulses_valid(self):
        """
        Check if the pulses are in the correct shape.

        Returns: bool
            If they are valid or not
        """
        for i, pulse in enumerate(self.pulses):
            if pulse.coeff is None or isinstance(pulse.coeff, bool):
                # constant pulse
                continue
            if pulse.tlist is None:
                raise ValueError(
                    "Pulse id={} is invalid. "
                    "Please define a tlist for the pulse.".format(i))
            if pulse.tlist is not None and pulse.coeff is None:
                raise ValueError(
                    "Pulse id={} is invalid. "
                    "Please define a coeff for the pulse.".format(i))
            coeff_len = len(pulse.coeff)
            tlist_len = len(pulse.tlist)
            if pulse.spline_kind == "step_func":
                if coeff_len == tlist_len-1 or coeff_len == tlist_len:
                    pass
                else:
                    raise ValueError(
                        "The length of tlist and coeff of the pulse "
                        "labelled {} is invalid. "
                        "It's either len(tlist)=len(coeff) or "
                        "len(tlist)-1=len(coeff) for coefficients "
                        "as step function".format(i))
            elif pulse.spline_kind == "cubic":
                if coeff_len == tlist_len:
                    pass
                else:
                    raise ValueError(
                        "The length of tlist and coeff of the pulse "
                        "labelled {} is invalid. "
                        "It should be either len(tlist)=len(coeff)".format(i))
            else:
                raise ValueError("Unknown spline_kind.")
        return True

    def add_noise(self, noise):
        """
        Add a noise object to the processor

        Parameters
        ----------
        noise: :class:`qutip.qip.Noise`
            The noise object defined outside the processor
        """
        if isinstance(noise, Noise):
            self.noise.append(noise)
        else:
            raise TypeError("Input is not a Noise object.")

    def save_coeff(self, file_name, inctime=True):
        """
        Save a file with the control amplitudes in each timeslot.

        Parameters
        ----------
        file_name: string
            Name of the file.

        inctime: bool, optional
            True if the time list should be included in the first column.
        """
        self._is_pulses_valid()
        # TODO this works only for step_func
        # TODO replace this by get_complete_coeffs
        coeffs = np.array(self.get_full_coeffs())
        if inctime:
            shp = coeffs.T.shape
            data = np.empty((shp[0], shp[1] + 1), dtype=np.float)
            data[:, 0] = self.get_full_tlist()
            data[:, 1:] = coeffs.T
        else:
            data = coeffs.T

        np.savetxt(file_name, data, delimiter='\t', fmt='%1.16f')

    def read_coeff(self, file_name, inctime=True):
        """
        Read the control amplitudes matrix and time list
        saved in the file by `save_amp`.

        Parameters
        ----------
        file_name: string
            Name of the file.

        inctime: bool, optional
            True if the time list in included in the first column.

        Returns
        -------
        tlist: array_like
            The time list read from the file.

        coeffs: array_like
            The pulse matrix read from the file.
        """
        data = np.loadtxt(file_name, delimiter='\t')
        if not inctime:
            self.coeffs = data.T
            return self.coeffs
        else:
            tlist = data[:, 0]
            self.set_all_tlist(tlist)
            self.coeffs = data[:, 1:].T
            return self.get_full_tlist, self.coeffs

    def get_noisy_pulses(self, ind_device_noise=False, ind_drift=False):
        """
        It takes the pulses defined in the `Processor` and
        add noise according to `Processor.noise`. It does not modify the
        pulses saved in `Processor.pulses` but return a new list.
        The length of the new list of noisy pulses might be longer
        because of drift Hamiltonian and device noise. They will be
        added to the end of the pulses list.

        Parameters
        ----------
        ind_device_noise: bool, optional
            If include pulse independent noise such as single qubit
            Relaxation. Default is False.
        ind_drift: bool, optional
            If include drift Hamiltonians. Default is False.

        Returns
        -------
        noisy_pulses: list of :class"`qutip.qip.Pulse`/:class:`qutip.qip.Drift`
            A list of noisy pulses.
        """
        # TODO add tests
        pulses = deepcopy(self.pulses)
        noisy_pulses = process_noise(
            pulses, self.noise, self.dims, t1=self.t1, t2=self.t2,
            ind_device_noise=ind_device_noise)
        if ind_drift:
            noisy_pulses += [self.drift]
        return noisy_pulses

    def get_qobjevo(self, args=None, noisy=False):
        """
        Create a :class:`qutip.QobjEvo` representation of the evolution.
        It calls the method `get_noisy_pulses` and create the `QobjEvo`
        from it.

        Parameters
        ----------
        args: dict, optional
            Arguments for :class:`qutip.QobjEvo`
        noisy: bool, optional
            If noise are included. Default is False.

        Returns
        -------
        qobjevo: :class:`qutip.QobjEvo`
            The :class:`qutip.QobjEvo` representation of the unitary evolution.
        c_ops: list of :class:`qutip.QobjEvo`
            A list of lindblad operators is also returned. if ``noisy==Flase``,
            it is always an empty list.
        """
        # TODO test it for non array-like coeff
        # check validity
        self._is_pulses_valid()

        if args is None:
            args = {}
        else:
            args = args
        # set step function

        if not noisy:
            dynamics = self.pulses
        else:
            dynamics = self.get_noisy_pulses(
                ind_device_noise=True, ind_drift=True)

        qu_list = []
        c_ops = []
        for pulse in dynamics:
            if noisy:
                qu, new_c_ops = pulse.get_noisy_qobjevo(dims=self.dims)
                c_ops += new_c_ops
            else:
                qu = pulse.get_ideal_qobjevo(dims=self.dims)
            qu_list.append(qu)

        final_qu = _merge_qobjevo(qu_list)
        final_qu.args.update(args)

        if noisy:
            return final_qu, c_ops
        else:
            return final_qu, []

    def run_analytically(self, init_state=None, qc=None):
        """
        Simulate the state evolution under the given `qutip.QubitCircuit`
        with matrice exponentiation. It will calculate the propagator
        with matrix exponentiation and return a list of :class:`qutip.Qobj`.
        This method won't include noise or collpase.

        Parameters
        ----------
        qc: :class:`qutip.qip.QubitCircuit`, optional
            Takes the quantum circuit to be implemented. If not given, use
            the quantum circuit saved in the processor by ``load_circuit``.

        init_state: :class:`qutip.Qobj`, optional
            The initial state of the qubits in the register.

        Returns
        -------
        evo_result: :class:`qutip.Result`
            An instance of the class
            :class:`qutip.Result` will be returned.
        """
        # TODO change init_state to init_state
        if init_state is not None:
            U_list = [init_state]
        else:
            U_list = []
        tlist = self.get_full_tlist()
        # TODO replace this by get_complete_coeff
        coeffs = np.array(self.coeffs)
        for n in range(len(tlist)-1):
            H = sum([coeffs[m, n] * self.ctrls[m]
                    for m in range(len(self.ctrls))])
            dt = tlist[n + 1] - tlist[n]
            U = (-1j * H * dt).expm()
            U = self.eliminate_auxillary_modes(U)
            U_list.append(U)

        try:  # correct_global_phase are defined for ModelProcessor
            if self.correct_global_phase and self.global_phase != 0:
                U_list.append(globalphase(self.global_phase, N=self.N))
        except AttributeError:
            pass

        return U_list

    def run(self, qc=None):
        """
        Calculate the propagator of the evolution by matrix exponentiation.
        This method won't include noise or collpase.

        Parameters
        ----------
        qc: :class:`qutip.qip.QubitCircuit`, optional
            Takes the quantum circuit to be implemented. If not given, use
            the quantum circuit saved in the processor by `load_circuit`.

        Returns
        -------
        U_list: list
            The propagator matrix obtained from the physical implementation.
        """
        if qc:
            self.load_circuit(qc)
        return self.run_analytically(qc=qc, init_state=None)

    def run_state(self, init_state=None, analytical=False, states=None,
                  noisy=True, **kwargs):
        """
        If `analytical` is False, use :func:`qutip.mesolve` to
        calculate the time of the state evolution
        and return the result. Other arguments of mesolve can be
        given as keyword arguments.
        If `analytical` is True, calculate the propagator
        with matrix exponentiation and return a list of matrices.
        Noise will be neglected in this choice.

        Parameters
        ----------
        init_state: Qobj
            Initial density matrix or state vector (ket).

        analytical: bool
            If True, calculate the evolution with matrices exponentiation.

        states: :class:`qutip.Qobj`, optional
            Old API, same as init_state.

        **kwargs
            Keyword arguments for the qutip solver.

        Returns
        -------
        evo_result: :class:`qutip.Result`
            If ``analytical`` is False,  an instance of the class
            :class:`qutip.Result` will be returned.

            If ``analytical`` is True, a list of matrices representation
            is returned.
        """
        if states is not None:
            warnings.warn(
                "states will be deprecated and replaced by init_state"
                "to be consistent with the QuTiP solver.",
                DeprecationWarning)
        if init_state is None and states is None:
            raise ValueError("Qubit state not defined.")
        elif init_state is None:
            # just to keep the old parameters `states`,
            # it is replaced by init_state
            init_state = states
        if analytical:
            if kwargs or self.noise:
                raise warnings.warn(
                    "Analytical matrices exponentiation"
                    "does not process noise or"
                    "any keyword arguments.")
            return self.run_analytically(init_state=init_state)

        # kwargs can not contain H or tlist
        if "H" in kwargs or "tlist" in kwargs:
            raise ValueError(
                "`H` and `tlist` are already specified by the processor "
                "and can not be given as a keyword argument")

        # construct qobjevo for unitary evolution
        if "args" in kwargs:
            noisy_qobjevo, sys_c_ops = self.get_qobjevo(
                    args=kwargs["args"], noisy=noisy)
        else:
            noisy_qobjevo, sys_c_ops = self.get_qobjevo(noisy=noisy)

        # add collpase operators into kwargs
        if "c_ops" in kwargs:
            if isinstance(kwargs["c_ops"], (Qobj, QobjEvo)):
                kwargs["c_ops"] += [kwargs["c_ops"]] + sys_c_ops
            else:
                kwargs["c_ops"] += sys_c_ops
        else:
            kwargs["c_ops"] = sys_c_ops

        evo_result = mesolve(
            H=noisy_qobjevo, rho0=init_state,
            tlist=noisy_qobjevo.tlist, **kwargs)
        return evo_result

    def load_circuit(self, qc):
        """
        Translate an :class:`qutip.qip.QubitCircuit` to its
        corresponding Hamiltonians. (Defined in subclasses)
        """
        raise NotImplementedError("Use the function in the sub-class")

    def eliminate_auxillary_modes(self, U):
        """
        Eliminate the auxillary modes like the cavity modes in cqed.
        (Defined in subclasses)
        """
        return U

    def plot_pulses(self, title=None, figsize=None, dpi=None):
        """
        Plot the pulse amplitude

        Parameters
        ----------
        noisy: bool, optional
            If true, plot the noisy pulses.

        title: str
            Title for the plot.

        figsize: tuple
            The size of the figure

        dpi: int
            The dpi of the figure

        Returns
        -------
        fig: matplotlib.figure.Figure
            The `Figure` object for the plot.

        ax: matplotlib.axes._subplots.AxesSubplot
            The axes for the plot.

        Notes
        -----
        ``plot_pulses`` only works for array_like coefficients
        """
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(1, 1, figsize=figsize, dpi=dpi)
        ax.set_ylabel("Control pulse amplitude")
        ax.set_xlabel("Time")

        # TODO add test
        coeffs = self.get_full_coeffs()
        tlist = self.get_full_tlist()

        for i in range(len(coeffs)):
            if not isinstance(coeffs[i], (Iterable, np.ndarray)):
                raise ValueError(
                    "plot_pulse only accepts array_like coefficients.")
            if self.spline_kind == "step_func":
                # if len(coeffs[i]) == len(tlist) - 1:
                #     coeffs[i] = np.hstack(
                #         [self.coeffs[i], self.coeffs[i, -1:]])
                # else:
                coeffs[i:-1] = coeffs[i:-2]
                ax.step(tlist, coeffs[i], where='post')
            elif self.spline_kind == "cubic":
                sp = CubicSpline(tlist, coeffs[i])
                t_line = np.linspace(tlist[0], tlist[-1], 200)
                c_line = [sp(t) for t in t_line]
                ax.plot(t_line, c_line)
        if title is not None:
            ax.set_title(title)
        fig.tight_layout()
        return fig, ax
