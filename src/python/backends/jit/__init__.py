"""Just-in-time compiling backend."""

from __future__ import division

__copyright__ = "Copyright (C) 2008 Andreas Kloeckner"

__license__ = """
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see U{http://www.gnu.org/licenses/}.
"""




import hedge.backends.cpu_base
import hedge.discretization
import hedge.optemplate
from hedge.backends.cpu_base import ExecutorBase, ExecutionMapperBase
from pymbolic.mapper.c_code import CCodeMapper
import numpy
from hedge.compiler import OperatorCompilerBase, FluxBatchAssign




# flux to code mapper ---------------------------------------------------------
class FluxToCodeMapper(CCodeMapper):
    def __init__(self, flux_idx, fvi, is_flipped=False):
        CCodeMapper.__init__(self, repr, reverse=False)
        self.flux_idx = flux_idx
        self.flux_var_info = fvi
        self.is_flipped = is_flipped

    def map_normal(self, expr, enclosing_prec):
        if self.is_flipped:
            where = "opp"
        else:
            where = "loc"
        return "fp.%s.normal[%d]" % (where, expr.axis)

    def map_penalty_term(self, expr, enclosing_prec):
        if self.is_flipped:
            where = "opp"
        else:
            where = "loc"
        return ("pow(fp.%(where)s.order*fp.%(where)s.order/fp.%(where)s.h, %(pwr)r)" 
                % {"pwr": expr.power, "where": where})

    def map_field_component(self, expr, enclosing_prec):
        if expr.is_local ^ self.is_flipped:
            where = "loc"
        else:
            where = "opp"

        arg_name = self.flux_var_info.flux_idx_and_dep_to_arg_name[
                self.flux_idx, expr]
        
        if not arg_name:
            return "0"
        else:
            return "%s_it[%s_idx]" % (arg_name, where)




# flux kinds ------------------------------------------------------------------
class InteriorFluxKind(object):
    def __hash__(self):
        return hash(self.__class__)

    def __str__(self):
        return "interior"

    def __eq__(self, other):
        return (other.__class__ == self.__class__)

class BoundaryFluxKind(object):
    def __init__(self, tag):
        self.tag = tag

    def __str__(self):
        return "boundary(%s)" % self.tag

    def __hash__(self):
        return hash((self.__class__, self.tag))

    def __eq__(self, other):
        return (other.__class__ == self.__class__
                and other.tag == self.tag)



        
# exec mapper -----------------------------------------------------------------
class ExecutionMapper(hedge.optemplate.Evaluator,
        hedge.optemplate.BoundOpMapperMixin, 
        hedge.optemplate.LocalOpReducerMixin):
    def __init__(self, context, discr, executor):
        hedge.optemplate.Evaluator.__init__(self, context.copy())
        self.discr = discr
        self.executor = executor

    # code execution functions ------------------------------------------------
    def exec_discard(self, insn):
        del self.context[insn.name]

    def exec_assign(self, insn):
        self.context[insn.name] = self(insn.expr)

    def exec_flux_batch_assign(self, insn):
        is_bdry = isinstance(insn.kind, BoundaryFluxKind)

        from pymbolic.primitives import is_zero

        def eval_arg(arg_spec):
            arg_expr, is_int = arg_spec
            arg = self.rec(arg_expr)
            if is_zero(arg):
                if is_bdry and not is_int:
                    return self.discr.boundary_zeros(insn.kind.tag)
                else:
                    return self.discr.volume_zeros()
            else:
                return arg

        args = [eval_arg(arg_expr) for arg_expr in insn.arg_specs]

        if is_bdry:
            bdry = self.discr.get_boundary(insn.kind.tag)
            face_groups = bdry.face_groups
        else:
            face_groups = self.discr.face_groups

        for fg in face_groups:
            fof_shape = (fg.face_count*fg.face_length()*fg.element_count(),)
            all_fluxes_on_faces = [
                    numpy.zeros(fof_shape, dtype=self.discr.default_scalar_type)
                    for f in insn.fluxes]
            insn.compiled_func(fg, *(all_fluxes_on_faces+args))
            
            for name, flux, fluxes_on_faces in zip(insn.names, insn.fluxes, 
                    all_fluxes_on_faces):
                from hedge.optemplate import LiftingFluxOperator

                out = self.discr.volume_zeros()
                if isinstance(flux.op, LiftingFluxOperator):
                    self.executor.lift_flux(fg, fg.ldis_loc.lifting_matrix(),
                            fg.local_el_inverse_jacobians, fluxes_on_faces, out)
                else:
                    self.executor.lift_flux(fg, fg.ldis_loc.multi_face_mass_matrix(),
                            None, fluxes_on_faces, out)

                self.context[name] = out

    def exec_diff_batch_assign(self, insn):
        rst_derivatives = [
                self.executor.diff_rst(insn.op_class, i, 
                    self.rec(insn.field)) 
                for i in range(self.discr.dimensions)]

        for name, op in zip(insn.names, insn.operators):
            self.context[name] = self.executor.diff_rst_to_xyz(op, rst_derivatives)

    # mapper functions --------------------------------------------------------
    def map_mass_base(self, op, field_expr):
        field = self.rec(field_expr)

        if isinstance(field, (float, int)) and field == 0:
            return 0

        out = self.discr.volume_zeros()
        self.executor.do_mass(op, field, out)
        return out




# compiler stuff --------------------------------------------------------------
class CompiledFluxBatchAssign(FluxBatchAssign):
    __slots__ = ["compiled_func", "arg_specs"]

class OperatorCompiler(OperatorCompilerBase):
    def __init__(self, discr):
        OperatorCompilerBase.__init__(self)
        self.discr = discr

    def get_contained_fluxes(self, expr):
        from hedge.optemplate import FluxCollector, BoundaryPair
        from hedge.tools import is_obj_array

        def get_deps(field):
            if is_obj_array(field):
                return set(field)
            else:
                return set([field])

        def get_flux_deps(op_binding):
            if isinstance(op_binding.field, BoundaryPair):
                bpair = op_binding.field
                return get_deps(bpair.field) | get_deps(bpair.bfield)
            else:
                return get_deps(op_binding.field)

        def get_flux_kind(op_binding):
            if isinstance(op_binding.field, BoundaryPair):
                return BoundaryFluxKind(op_binding.field.tag)
            else:
                return InteriorFluxKind()

        return [self.FluxRecord(
            flux_expr=flux_binding, 
            kind=get_flux_kind(flux_binding),
            dependencies=get_flux_deps(flux_binding))
            for flux_binding in FluxCollector()(expr)]

    def internal_map_flux(self, flux_bind):
        from hedge.optemplate import IdentityMapper, BoundaryPair
        return IdentityMapper.map_operator_binding(self, flux_bind)

    def map_operator_binding(self, expr):
        from hedge.optemplate import FluxOperatorBase
        if isinstance(expr.op, FluxOperatorBase):
            return self.map_planned_flux(expr)
        else:
            return OperatorCompilerBase.map_operator_binding(self, expr)

    # flux compilation --------------------------------------------------------
    def make_flux_batch_assign(self, names, fluxes, kind):
        if isinstance(kind, BoundaryFluxKind):
            return self.make_boundary_flux_batch_assign(names, fluxes, kind)
        elif isinstance(kind, InteriorFluxKind):
            return self.make_interior_flux_batch_assign(names, fluxes, kind)
        else:
            raise ValueError("invalid flux batch type: %s" % kind)

    def _get_flux_var_info(self, fluxes):
        from pytools import Record
        class FluxVariableInfo(Record):
            pass

        fvi = FluxVariableInfo(
                arg_specs = [],
                arg_names = [],
                flux_idx_and_dep_to_arg_name = {}, # or 0 if zero
                )

        field_expr_to_arg_name = {}

        from hedge.flux import FieldComponent, FluxDependencyMapper
        from hedge.optemplate import BoundaryPair

        for flux_idx, flux_binding in enumerate(fluxes):
            for fc in FluxDependencyMapper(composite_leaves=True)(flux_binding.op.flux):
                assert isinstance(fc, FieldComponent)
                if isinstance(flux_binding.field, BoundaryPair):
                    if fc.is_local:
                        this_field_expr = flux_binding.field.field
                    else:
                        this_field_expr = flux_binding.field.bfield
                else:
                    this_field_expr = flux_binding.field

                from hedge.tools import is_obj_array
                if is_obj_array(this_field_expr):
                    fc_field_expr = this_field_expr[fc.index]
                else:
                    assert fc.index == 0
                    fc_field_expr = this_field_expr

                from pymbolic.primitives import is_zero
                if is_zero(fc_field_expr):
                    fvi.flux_idx_and_dep_to_arg_name[flux_idx, fc] = 0
                else:
                    if fc_field_expr not in field_expr_to_arg_name:
                        arg_name = "arg%d" % len(fvi.arg_specs)
                        field_expr_to_arg_name[fc_field_expr] = arg_name

                        fvi.arg_names.append(arg_name)
                        fvi.arg_specs.append((fc_field_expr, fc.is_local))
                    else:
                        arg_name = field_expr_to_arg_name[fc_field_expr]

                    fvi.flux_idx_and_dep_to_arg_name[flux_idx, fc] = arg_name

        return fvi

    def make_interior_flux_batch_assign(self, names, fluxes, kind):
        fvi = self._get_flux_var_info(fluxes)

        from codepy.cgen import \
                FunctionDeclaration, FunctionBody, \
                Const, Reference, Value, MaybeUnused, \
                Statement, Include, Line, Block, Initializer, Assign, \
                CustomLoop, For

        from codepy.bpl import BoostPythonModule
        mod = BoostPythonModule()

        S = Statement
        mod.add_to_module([
            Include("hedge/face_operators.hpp"), 
            Include("boost/foreach.hpp"), 
            Line(),
            S("using namespace hedge"),
            Line()
            ])

        fdecl = FunctionDeclaration(
                Value("void", "gather_flux"), 
                [
                    Const(Reference(Value("face_group", "fg"))),
                    ]+[
                    Value("py_vector", "flux%d_on_faces" % i)
                    for i in range(len(fluxes))
                    ]+[
                    Const(Reference(Value("py_vector", arg_name)))
                    for arg_name in fvi.arg_names
                    ]
                )

        from pytools import flatten

        from pymbolic.mapper.stringifier import PREC_PRODUCT

        fbody = Block([
            Initializer(
                Const(Value("py_vector::iterator", "fof%d_it" % i)),
                "flux%d_on_faces.begin()" % i)
            for i in range(len(fluxes))
            ]+[
            Initializer(
                Const(Value("py_vector::const_iterator", "%s_it" % arg_name)),
                arg_name + ".begin()")
            for arg_name in fvi.arg_names
            ]+[
            Line(),
            CustomLoop("BOOST_FOREACH(const face_pair &fp, fg.face_pairs)", Block(
                list(flatten([
                Initializer(Value("node_number_t", "%s_ebi" % where),
                    "fp.%s.el_base_index" % where),
                Initializer(Value("index_lists_t::const_iterator", "%s_idx_list" % where),
                    "fg.index_list(fp.%s.face_index_list_number)" % where),
                Initializer(Value("node_number_t", "%s_fof_base" % where),
                    "fg.face_length()*(fp.%(where)s.local_el_number*fg.face_count"
                    " + fp.%(where)s.face_id)" % {"where": where}),
                Line(),
                ]
                for where in ["loc", "opp"]
                ))+[
                Initializer(Value("index_lists_t::const_iterator", "opp_write_map"),
                    "fg.index_list(fp.opp_native_write_map)"),
                Line(),
                For(
                    "unsigned i = 0",
                    "i < fg.face_length()",
                    "++i",
                    Block(
                        [
                        Initializer(MaybeUnused(Value("node_number_t", "%s_idx" % where)),
                            "%(where)s_ebi + %(where)s_idx_list[i]" 
                            % {"where": where})
                        for where in ["loc", "opp"]
                        ]+[
                        Assign("fof%d_it[%s_fof_base+%s]" % (flux_idx, where, tgt_idx),
                            "fp.loc.face_jacobian * " +
                            FluxToCodeMapper(flux_idx, fvi, is_flipped=is_flipped)(flux.op.flux, PREC_PRODUCT))
                        for flux_idx, flux in enumerate(fluxes)
                        for where, is_flipped, tgt_idx in [
                            ("loc", False, "i"),
                            ("opp", True, "opp_write_map[i]")
                            ]
                        ]
                        )
                    )
                ]))
            ])
        mod.add_function(FunctionBody(fdecl, fbody)) 

        compiled_func = mod.compile(
                self.discr.platform, wait_on_error=True).gather_flux

        #print "----------------------------------------------------------------"
        #print FunctionBody(fdecl, fbody)

        if self.discr.instrumented:
            from hedge.tools import time_count_flop, gather_flops
            compiled_func = \
                    time_count_flop(
                            compiled_func,
                            self.discr.gather_timer,
                            self.discr.gather_counter,
                            self.discr.gather_flop_counter,
                            len(fluxes)*gather_flops(self.discr)*len(fvi.arg_names))

        return CompiledFluxBatchAssign(
                names=names, fluxes=fluxes, kind=kind,
                arg_specs=fvi.arg_specs, compiled_func=compiled_func)

    def make_boundary_flux_batch_assign(self, names, fluxes, kind):
        fvi = self._get_flux_var_info(fluxes)

        from codepy.cgen import \
                FunctionDeclaration, FunctionBody, Template, \
                Const, Reference, Value, MaybeUnused, \
                Statement, Include, Line, Block, Initializer, Assign, \
                CustomLoop, For

        from codepy.bpl import BoostPythonModule
        mod = BoostPythonModule()

        S = Statement
        mod.add_to_module([
            Include("hedge/face_operators.hpp"), 
            Include("boost/foreach.hpp"), 
            Line(),
            S("using namespace hedge"),
            Line()
            ])

        fdecl = FunctionDeclaration(
                    Value("void", "gather_flux"), 
                    [
                    Const(Reference(Value("face_group", "fg"))),
                    ]+[
                    Value("py_vector", "flux%d_on_faces" % i)
                    for i in range(len(fluxes))
                    ]+[
                    Const(Reference(Value("py_vector", arg_name)))
                    for arg_name in fvi.arg_names])

        from pytools import flatten

        from pymbolic.mapper.stringifier import PREC_PRODUCT

        fbody = Block([
            Initializer(
                Const(Value("py_vector::iterator", "fof%d_it" % i)),
                "flux%d_on_faces.begin()" % i)
            for i in range(len(fluxes))
            ]+[
            Initializer(
                Const(Value("py_vector::const_iterator", 
                    "%s_it" % arg_name)),
                "%s.begin()" % arg_name)
            for arg_name in fvi.arg_names
            ]+[
            Line(),
            CustomLoop("BOOST_FOREACH(const face_pair &fp, fg.face_pairs)", Block(
                list(flatten([
                Initializer(Value("node_number_t", "%s_ebi" % where),
                    "fp.%s.el_base_index" % where),
                Initializer(Value("index_lists_t::const_iterator", "%s_idx_list" % where),
                    "fg.index_list(fp.%s.face_index_list_number)" % where),
                Line(),
                ]
                for where in ["loc", "opp"]
                ))+[
                Line(),
                Initializer(Value("node_number_t", "loc_fof_base"),
                    "fg.face_length()*(fp.%(where)s.local_el_number*fg.face_count"
                    " + fp.%(where)s.face_id)" % {"where": "loc"}),
                Line(),
                For(
                    "unsigned i = 0",
                    "i < fg.face_length()",
                    "++i",
                    Block(
                        [
                        Initializer(MaybeUnused(
                            Value("node_number_t", "%s_idx" % where)),
                            "%(where)s_ebi + %(where)s_idx_list[i]" 
                            % {"where": where})
                        for where in ["loc", "opp"]
                        ]+[
                        Assign("fof%d_it[loc_fof_base+i]" % flux_idx,
                            "fp.loc.face_jacobian * " +
                            FluxToCodeMapper(flux_idx, fvi)(flux.op.flux, PREC_PRODUCT))
                        for flux_idx, flux in enumerate(fluxes)
                        ]
                        )
                    )
                ]))
            ])
        mod.add_function(FunctionBody(fdecl, fbody))

        #print "----------------------------------------------------------------"
        #print FunctionBody(fdecl, fbody)

        compiled_func = mod.compile(self.discr.platform, wait_on_error=True).gather_flux

        if self.discr.instrumented:
            from pytools.log import time_and_count_function
            compiled_func = time_and_count_function(compiled_func, self.discr.gather_timer)

        return CompiledFluxBatchAssign(
                names=names, fluxes=fluxes, kind=kind,
                arg_specs=fvi.arg_specs, 
                compiled_func=compiled_func)




class Executor(ExecutorBase):
    def __init__(self, discr, code):
        ExecutorBase.__init__(self, discr)
        self.code = code

    def __call__(self, **vars):
        return self.code.execute(ExecutionMapper(vars, self.discr, self))




# discretization --------------------------------------------------------------
class Discretization(hedge.discretization.Discretization):
    def __init__(self, *args, **kwargs):
        hedge.discretization.Discretization.__init__(self, *args, **kwargs)

        plat = kwargs.pop("platform", None)

        if plat is None:
            from codepy.jit import guess_platform
            plat = guess_platform()

        plat = plat.copy()
        
        from codepy.libraries import add_hedge
        add_hedge(plat)

        self.platform = plat

    def compile(self, optemplate):
        from hedge.optemplate import \
                OperatorBinder, \
                InverseMassContractor, \
                BCToFluxRewriter, \
                EmptyFluxKiller

        from hedge.optemplate import CommutativeConstantFoldingMapper

        prepared_optemplate = (
                InverseMassContractor()(
                    EmptyFluxKiller(self)(
                        CommutativeConstantFoldingMapper()(
                            BCToFluxRewriter()(
                                OperatorBinder()(
                                    optemplate))))))

        code = OperatorCompiler(self)(prepared_optemplate)
        #print code
        return Executor(self, code)


