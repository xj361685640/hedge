# -*- coding: utf8 -*-

"""Adams-Bashforth ODE solvers."""

from __future__ import division

__copyright__ = "Copyright (C) 2007 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""



import numpy
import numpy.linalg as la
from pytools import memoize
from hedge.timestep.base import TimeStepper




# coefficient generators ------------------------------------------------------
def make_generic_ab_coefficients(levels, int_start, tap):
    """Find coefficients (αᵢ) such that
       ∑ᵢ αᵢ F(tᵢ) = ∫[int_start..tap] f(t) dt."""

    # explanations --------------------------------------------------------------
    # To calculate the AB coefficients this method makes use of the interpolation
    # connection of the Vandermonde matrix:
    #
    #  Vᵀ * α = fe(t₀₊₁),                                    (1)
    #
    # with Vᵀ as the transposed Vandermonde matrix (with monomial base: xⁿ), 
    # 
    #  α = (..., α₋₂, α₋₁,α₀)ᵀ                               (2)
    #
    # a vector of interpolation coefficients and 
    # 
    #  fe(t₀₊₁) = (t₀₊₁⁰, t₀₊₁¹, t₀₊₁²,...,t₀₊₁ⁿ)ᵀ           (3)
    #
    # a vector of the evaluated interpolation polynomial f(t) at t₀₊₁ = t₀ ∓ h 
    # (h being any arbitrary stepsize).
    #
    # Solving the system (1) by knowing Vᵀ and fe(t₀₊₁) receiving α makes it 
    # possible for any function F(t) - the function which gets interpolated 
    # by the interpolation polynomial f(t) - to calculate f(t₀₊₁) by:
    #
    # f(t₀₊₁) =  ∑ᵢ αᵢ F(tᵢ)                                 (5)
    #
    # with F(tᵢ) being the values of F(t) at the sampling points tᵢ.
    # --------------------------------------------------------------------------
    # The Adams-Bashforth method is defined by:
    #
    #  y(t₀₊₁) = y(t₀) + Δt * ∫₀⁰⁺¹ f(t) dt                  (6)
    #
    # with:
    # 
    #  ∫₀⁰⁺¹ f(t) dt = ∑ᵢ ABcᵢ F(tᵢ),                        (8)
    #
    # with ABcᵢ = [AB coefficients], f(t) being the interpolation polynomial,
    # and F(tᵢ) being the values of F (= RHS) at the sampling points tᵢ.
    # --------------------------------------------------------------------------
    # For the AB method (1) becomes:
    #
    #  Vᵀ * ABc = ∫₀⁰⁺¹ fe(t₀₊₁)                             (7)
    #
    # with ∫₀⁰⁺¹ fe(t₀₊₁) being a vector evalueting the integral of the 
    # interpolation polynomial in the form oft 
    # 
    #  1/(n+1)*(t₀₊₁⁽ⁿ⁾-t₀⁽ⁿ⁾)                               (8)
    # 
    #  for n = 0,1,...,N sampling points, and 
    # 
    # ABc = [c₀,c₁, ... , cn]ᵀ                               (9)
    #
    # being the AB coefficients.
    # 
    # For example ∫₀⁰⁺¹ f(t₀₊₁) evaluated for the timestep [t₀,t₀₊₁] = [0,1]
    # is:
    #
    #  point_eval_vec = [1, 0.5, 0.333, 0.25, ... ,1/n]ᵀ.
    #
    # For substep levels the bounds of the integral has to be adapted to the
    # size and position of the substep interval: 
    # 
    #  [t₀,t₀₊₁] = [substep_int_start, substep_int_end] 
    # 
    # which is equal to the implemented [int_start, tap].
    #
    # Since Vᵀ and ∫₀⁰⁺¹ f(t₀₊₁) is known the AB coefficients c can be
    # predicted by solving system (7) and calculating:
    # 
    #  ∫₀⁰⁺¹ f(t) dt = ∑ᵢ ABcᵢ F(tᵢ),

    from hedge.polynomial import monomial_vdm
    point_eval_vec = numpy.array([
        1/(n+1)*(tap**(n+1)-int_start**(n+1)) for n in range(len(levels))])
    return la.solve(monomial_vdm(levels).T, point_eval_vec)




@memoize
def make_ab_coefficients(order):
    return make_generic_ab_coefficients(numpy.arange(0, -order, -1), 0, 1)




# time steppers ---------------------------------------------------------------
class AdamsBashforthTimeStepper(TimeStepper):
    dt_fudge_factor = 0.95

    def __init__(self, order, startup_stepper=None, dtype=numpy.float64, rcon=None):
        self.f_history = []

        from pytools import match_precision
        self.dtype = numpy.dtype(dtype)
        self.scalar_dtype = match_precision(
                numpy.dtype(numpy.float64), self.dtype)
        self.coefficients = numpy.asarray(make_ab_coefficients(order),
                dtype=self.scalar_dtype)

        if startup_stepper is not None:
            self.startup_stepper = startup_stepper
        else:
            from hedge.timestep.runge_kutta import LSRK4TimeStepper
            self.startup_stepper = LSRK4TimeStepper(self.dtype)

        from pytools.log import IntervalTimer, EventCounter
        timer_factory = IntervalTimer
        if rcon is not None:
            timer_factory = rcon.make_timer

        self.timer = timer_factory(
                "t_ab", "Time spent doing algebra in Adams-Bashforth")
        self.flop_counter = EventCounter(
                "n_flops_ab", "Floating point operations performed in AB")

    @property
    def order(self):
        return len(self.coefficients)

    def get_stability_relevant_init_args(self):
        return (self.order,)

    def add_instrumentation(self, logmgr):
        logmgr.add_quantity(self.timer)
        logmgr.add_quantity(self.flop_counter)

    def __getinitargs__(self):
        return (self.order, self.startup_stepper)

    def __call__(self, y, t, dt, rhs):
        if len(self.f_history) == 0:
            # insert IC
            self.f_history.append(rhs(t, y))

            from hedge.tools import count_dofs
            self.dof_count = count_dofs(self.f_history[0])

        if len(self.f_history) < len(self.coefficients):
            ynew = self.startup_stepper(y, t, dt, rhs)
            if len(self.f_history) == len(self.coefficients) - 1:
                # here's some memory we won't need any more
                del self.startup_stepper

        else:
            from operator import add

            sub_timer = self.timer.start_sub_timer()
            assert len(self.coefficients) == len(self.f_history)
            ynew = y + dt * reduce(add,
                    (coeff * f 
                        for coeff, f in 
                        zip(self.coefficients, self.f_history)))

            self.f_history.pop()
            sub_timer.stop().submit()

        self.flop_counter.add((2+2*len(self.coefficients)-1)*self.dof_count)

        self.f_history.insert(0, rhs(t+dt, ynew))
        return ynew
