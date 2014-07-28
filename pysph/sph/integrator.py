"""Basic code for the templated integrators.

Currently we only support two-step integrators.

These classes are used to generate the code for the actual integrators
from the `sph_eval` module.
"""

from numpy import sqrt

# Local imports.
from .integrator_step import IntegratorStep

###############################################################################
# `Integrator` class
###############################################################################
class Integrator(object):
    r"""Generic class for multi-step integrators in PySPH for a system of
    ODES of the form :math:`\frac{dy}{dt} = F(y)`.
    """

    def __init__(self, **kw):
        """Pass fluid names and suitable `IntegratorStep` instances.

        For example::

            >>> integrator = Integrator(fluid=WCSPHStep(), solid=WCSPHStep())

        where "fluid" and "solid" are the names of the particle arrays.
        """
        for array_name, integrator_step in kw.iteritems():
            if not isinstance(integrator_step, IntegratorStep):
                msg='Stepper %s must be an instance of IntegratorStep'%(integrator_step)
                raise ValueError(msg)

        self.steppers = kw
        self.parallel_manager = None
        # This is set later when the underlying compiled integrator is created
        # by the SPHCompiler.
        self.integrator = None

    ##########################################################################
    # Public interface.
    ##########################################################################
    def set_fixed_h(self, fixed_h):
        # compute h_minimum once for constant smoothing lengths
        if fixed_h:
            self.compute_h_minimum()

        self.fixed_h=fixed_h

    def set_nnps(self, nnps):
        self.integrator.set_nnps(nnps)

    def compute_h_minimum(self):
        calc = self.integrator.sph_calc

        hmin = 1.0
        for pa in calc.particle_arrays:
            h = pa.get_carray('h')
            h.update_min_max()

            if h.minimum < hmin:
                hmin = h.minimum

        self.h_minimum = hmin

    def compute_time_step(self, dt, cfl):
        calc = self.integrator.sph_calc

        # different time step controls
        dt_cfl_factor = calc.dt_cfl
        dt_visc_factor = calc.dt_viscous

        # force factor is acceleration squared
        dt_force_factor = sqrt(calc.dt_force)

        # iterate over particles and find hmin if using vatialbe h
        if not self.fixed_h:
            self.compute_h_minimum()

        hmin = self.h_minimum

        # default time steps set to some large value
        dt_cfl = dt_force = dt_viscous = 1e20

        # stable time step based on courant condition
        if dt_cfl_factor > 0:
            dt_cfl = hmin/dt_cfl_factor

        # stable time step based on force criterion
        if dt_force_factor > 0:
            dt_force = sqrt( hmin/dt_force_factor )

        # stable time step based on viscous condition
        if dt_visc_factor > 0:
            dt_viscous = hmin/dt_visc_factor

        # minimum of all three
        dt_min = min( dt_cfl, dt_force, dt_viscous )

        # return the computed time steps. If dt factors aren't
        # defined, the default dt is returned
        if dt_min <= 0.0:
            return dt
        else:
            return cfl*dt_min

    def one_timestep(self, t, dt):
        """User written function that actually does one timestep.

        This function is used in the high-performance Cython implementation.
        The assumptions one may make are the following:

            - t and dt are passed.

            - the following methods are available:

                - self.initialize()

                - self.stage1(), self.stage2() etc. depending on the number of
                  stages available.

                - self.compute_accelerations(t, dt)
                - self.do_post_stage(stage_dt, stage_count_from_1)

        Please see any of the concrete implementations of the Integrator class
        to study.  By default the Integrator implements a
        predict-evaluate-correct method, the same as PECIntegrator.

        """
        self.initialize()

        # Predict
        self.stage1()

        # Call any post-stage functions.
        self.do_post_stage(0.5*dt, 1)

        self.compute_accelerations()

        # Correct
        self.stage2()

        # Call any post-stage functions.
        self.do_post_stage(dt, 2)

    def set_parallel_manager(self, pm):
        self.integrator.set_parallel_manager(pm)

    def set_integrator(self, integrator):
        self.integrator = integrator

    def set_post_stage_callback(self, callback):
        """This callback is called when the particles are moved, i.e
        one stage of the integration is done.

        This callback is passed the current time value, the timestep and the
        stage.

        The current time value is  t + stage_dt, for example this would be
        0.5*dt for a two stage predictor corrector integrator.

        """
        self.integrator.set_post_stage_callback(callback)

    def step(self, time, dt):
        """This function is called by the solver.

        To implement the integration step please override the
        ``one_timestep`` method.
        """
        self.integrator.step(time, dt)


###############################################################################
# `EulerIntegrator` class
###############################################################################
class EulerIntegrator(Integrator):
    def one_timestep(self, t, dt):
        self.initialize()
        self.compute_accelerations()
        self.stage1()
        self.do_post_stage(dt, 1)


###############################################################################
# `PECIntegrator` class
###############################################################################
class PECIntegrator(Integrator):
    r"""
    In the Predict-Evaluate-Correct (PEC) mode, the system is advanced using:

    .. math::

        y^{n+\frac{1}{2}} = y^n + \frac{\Delta t}{2}F(y^{n-\frac{1}{2}}) --> Predict

        F(y^{n+\frac{1}{2}}) --> Evaluate

        y^{n + 1} = y^n + \Delta t F(y^{n+\frac{1}{2}})

    """
    def one_timestep(self, t, dt):
        self.initialize()

        # Predict
        self.stage1()

        # Call any post-stage functions.
        self.do_post_stage(0.5*dt, 1)

        self.compute_accelerations()

        # Correct
        self.stage2()

        # Call any post-stage functions.
        self.do_post_stage(dt, 2)


###############################################################################
# `EPECIntegrator` class
###############################################################################
class EPECIntegrator(Integrator):
    r"""
    Predictor corrector integrators can have two modes of
    operation.

    In the Evaluate-Predict-Evaluate-Correct (EPEC) mode, the
    system is advanced using:

    .. math::

        F(y^n) --> Evaluate

        y^{n+\frac{1}{2}} = y^n + F(y^n) --> Predict

        F(y^{n+\frac{1}{2}}) --> Evaluate

        y^{n+1} = y^n + \Delta t F(y^{n+\frac{1}{2}}) --> Correct

    Notes:

    The Evaluate stage of the integrator forces a function
    evaluation. Therefore, the PEC mode is much faster but relies on
    old accelertions for the Prediction stage.

    In the EPEC mode, the final corrector can be modified to:

    :math:`y^{n+1} = y^n + \frac{\Delta t}{2}\left( F(y^n) + F(y^{n+\frac{1}{2}}) \right)`

    This would require additional storage for the accelerations.

    """
    def one_timestep(self, t, dt):
        self.initialize()

        self.compute_accelerations()

        # Predict
        self.stage1()

        # Call any post-stage functions.
        self.do_post_stage(0.5*dt, 1)

        self.compute_accelerations()

        # Correct
        self.stage2()

        # Call any post-stage functions.
        self.do_post_stage(dt, 2)
