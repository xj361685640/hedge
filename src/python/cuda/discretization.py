"""Interface with Nvidia CUDA."""

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



import numpy
import numpy.linalg as la
import hedge.discretization
import pycuda.driver as cuda
import pycuda.gpuarray as gpuarray
from pytools import memoize_method




# gpu data organization -------------------------------------------------------
class GPUBlock(object):
    """Describes what data is local to each thread block on the GPU.

    @ivar number: The global number of this block.
    @ivar local_discretization: The L{hedge.element.Element} instance used
      for elements in this block.
    @ivar cpu_slices: A list of slices describing the CPU-side
      storage locations for the block's elements.
    @ivar elements: A list of L{hedge.mesh.Element} instances representing the
      elements in this block.
    @ivar ext_faces_from_me: A list of L{GPUInteriorFaceStorage} instances representing
      faces native to this block that are duplicated in other blocks.
      This points to faces native to this block.
    @ivar ext_faces_to_me: A list of L{GPUFaceStorage} instances representing faces
      native to other blocks that are duplicated in this block.
      This points to faces native to other blocks.
    """
    __slots__ = ["number", "local_discretization", "cpu_slices", "elements", 
            "ext_faces_from_me", "ext_faces_to_me"]

    def __init__(self, number, local_discretization, cpu_slices, elements):
        self.number = number
        self.local_discretization = local_discretization
        self.cpu_slices = cpu_slices
        self.elements = elements
        self.ext_faces_from_me = []
        self.ext_faces_to_me = []

    def get_el_index(self, sought_el):
        from pytools import one
        return one(i for i, el in enumerate(self.elements)
                if el == sought_el)

    def register_ext_face_to_me(self, face):
        result = len(self.ext_faces_to_me)
        self.ext_faces_to_me.append(face)
        return result

    def register_ext_face_from_me(self, face):
        self.ext_faces_from_me.append(face)




class GPUFaceStorage(object):
    """Describes where the dofs of an element face are stored.

    @ivar opposite: the L{GPUFacestorage} instance for the face
      oposite to this one.
    """
    __slots__ = ["opposite"]

    def __init__(self):
        self.opposite = None

    def set_opposite(self, opp):
        if self.opposite is None:
            self.opposite = opp
        else:
            assert self.opposite is opp

class GPUInteriorFaceStorage(GPUFaceStorage):
    """Describes storage locations for a face local to an element in a block.

    @ivar el_face: a tuple C{(element, face_number)}.
    @ivar cpu_slice: the base index of the element in CPU numbering.
    @ivar native_index_list_id: 
    @ivar opp_write_index_list_id:
    @ivar dup_int_flux_index_list_id:
    @ivar dup_ext_flux_index_list_id:
    @ivar native_block: block in which element is to be found.
    @ivar native_block_el_num: number of this element in the C{native_block}.
    @ivar face_pair_side:
    """
    __slots__ = [
            "el_face", "cpu_slice", 
            "native_index_list_id", "opp_write_index_list_id",
            "global_int_flux_index_list_id", "global_ext_flux_index_list_id",
            "dup_int_flux_index_list_id", "dup_ext_flux_index_list_id",
            "dup_block", "dup_ext_face_number",
            "native_block", "native_block_el_num",
            "face_pair_side"]

    def __init__(self, el_face, cpu_slice, native_index_list_id,
            native_block, native_block_el_num, face_pair_side):
        GPUFaceStorage.__init__(self)
        self.el_face = el_face
        self.cpu_slice = cpu_slice
        self.native_index_list_id = native_index_list_id
        self.native_block = native_block
        self.native_block_el_num = native_block_el_num
        self.face_pair_side = face_pair_side

class GPUBoundaryFaceStorage(GPUFaceStorage):
    """Describes storage locations for a boundary face.

    @ivar cpu_bdry_index_in_floats: this face's starting index 
      in the CPU-based TAG_ALL boundary array [floats].
    @ivar gpu_bdry_index_in_floats: this face's starting index 
      in the GPU-based TAG_ALL boundary array [floats].
    @ivar face_pair_side:
    """
    __slots__ = [
            "cpu_bdry_index_in_floats", 
            "gpu_bdry_index_in_floats", 
            "face_pair_side",
            "dup_block", "dup_ext_face_number"]

    def __init__(self, 
            cpu_bdry_index_in_floats,
            gpu_bdry_index_in_floats,
            face_pair_side
            ):
        GPUFaceStorage.__init__(self)
        self.cpu_bdry_index_in_floats = cpu_bdry_index_in_floats
        self.gpu_bdry_index_in_floats = gpu_bdry_index_in_floats
        self.face_pair_side = face_pair_side




# GPU discretization ----------------------------------------------------------
class Discretization(hedge.discretization.Discretization):
    def _make_plan(self, ldis, mesh, float_type):
        from hedge.cuda.plan import \
                ExecutionPlan, \
                ExecutionPlanWithFluxTemp, \
                Parallelism

        def generate_valid_plans():
            for pe in range(2,32):
                for se in range(1,256):
                    flux_par = Parallelism(pe, se)
                    plan = ExecutionPlanWithFluxTemp(self.devdata, ldis, flux_par,
                            float_type=float_type)
                    if plan.invalid_reason() is None:
                        yield plan

        plans = list(generate_valid_plans())

        if not plans:
            raise RuntimeError, "no valid CUDA execution plans found"

        desired_occup = max(plan.flux_occupancy_record().occupancy for plan in plans)
        if desired_occup > 0.66:
            # see http://forums.nvidia.com/lofiversion/index.php?t67766.html
            desired_occup = 0.66

        good_plans = [p for p in plans
                if p.flux_occupancy_record().occupancy >= desired_occup - 1e-10
                ]

        from pytools import argmax2
        return argmax2((p, p.elements_per_block()) for p in good_plans)




    def _partition_mesh(self, mesh, plan):
        # search for mesh partition that matches plan
        from pymetis import part_graph
        orig_part_count = part_count = len(mesh.elements)//plan.flux_par.total()+1
        while True:
            cuts, partition = part_graph(part_count,
                    mesh.element_adjacency_graph(),
                    vweights=[1000]*len(mesh.elements))

            # prepare a mapping:  block# -> # of external interfaces
            block2extifaces = {}

            for (e1, f1), (e2, f2) in mesh.both_interfaces():
                b1 = partition[e1.id]
                b2 = partition[e2.id]

                if b1 != b2:
                    block2extifaces[b1] = block2extifaces.get(b1, 0) + 1

            for el, face_nbr in mesh.tag_to_boundary[hedge.mesh.TAG_ALL]:
                b1 = partition[el.id]
                block2extifaces[b1] = block2extifaces.get(b1, 0) + 1

            blocks = {}
            for el_id, block in enumerate(partition):
                blocks.setdefault(block, []).append(el_id)

            from hedge.cuda.tools import int_ceiling
            block_elements = max(len(block_els) for block_els in blocks.itervalues())
            flux_par_s = int_ceiling(block_elements/plan.flux_par.p)

            from hedge.cuda.plan import Parallelism
            actual_plan = plan.copy(
                    max_ext_faces=max(block2extifaces.itervalues()),
                    max_faces=max(
                        len(blocks[b])*plan.faces_per_el()
                        + block2extifaces[b]
                        for b in range(len(blocks))),
                    flux_par=Parallelism(plan.flux_par.p, flux_par_s))
            assert actual_plan.max_faces % 2 == 0

            if (flux_par_s <= plan.flux_par.s and
                    abs(plan.flux_occupancy_record().occupancy -
                        actual_plan.flux_occupancy_record().occupancy) < 1e-10):
                break

            part_count += 1

        print "blocks: theoretical:%d practical:%d" % (orig_part_count, part_count)

        if False:
            from matplotlib.pylab import hist, show
            print plan.get_extface_count()
            hist(block2extifaces.values())
            show()
            hist([len(block_els) for block_els in blocks.itervalues()])
            show()
            
        return actual_plan, partition




    def __init__(self, mesh, local_discretization=None, 
            order=None, plan=None, init_cuda=True, debug=False, 
            dev=None, default_scalar_type=numpy.float32):
        ldis = self.get_local_discretization(mesh, local_discretization, order)

        if init_cuda:
            cuda.init()

        if dev is None:
            assert cuda.Device.count() >= 1
            dev = cuda.Device(0)
        if isinstance(dev, int):
            dev = cuda.Device(dev)
        if init_cuda:
            self.cuda_context = dev.make_context()

        self.device = dev
        from hedge.cuda.tools import DeviceData
        self.devdata = DeviceData(dev)

        # make preliminary plan
        if plan is None:
            plan = self._make_plan(ldis, mesh, default_scalar_type)
        print "projected:", plan

        # partition mesh, obtain updated plan
        self.plan, self.partition = self._partition_mesh(mesh, plan)
        del plan
        print "actual:", self.plan

        # initialize superclass
        hedge.discretization.Discretization.__init__(self, mesh, ldis, debug=debug,
                default_scalar_type=default_scalar_type)

        # build our own data structures
        from hedge.cuda.tools import exact_div
        self.int_dof_count = exact_div(self.plan.int_dof_smem(), self.plan.float_size)
        self.ext_dof_count = exact_div(self.plan.ext_dof_smem(), self.plan.float_size)

        self.blocks = self._build_blocks()
        self.face_storage_map = self._build_face_storage_map()

        # check the ext_dof_smem estimate
        assert (self.devdata.align(
            max(len(block.ext_faces_to_me)*self.plan.dofs_per_face()
                for block in self.blocks)*self.plan.float_size)
            == self.plan.ext_dof_smem())

        from hedge.discr_precompiled import Discretization
        self.test_discr = Discretization(mesh, ldis)

    def _build_blocks(self):
        block_el_numbers = {}
        for el_id, block in enumerate(self.partition):
            block_el_numbers.setdefault(block, []).append(el_id)

        block_count = len(block_el_numbers)

        def make_block(block_num):
            elements = [self.mesh.elements[ben] for ben in block_el_numbers[block_num]]

            eg, = self.element_groups
            return GPUBlock(block_num, 
                    local_discretization=eg.local_discretization,
                    cpu_slices=[self.find_el_range(el.id) for el in elements], 
                    elements=elements)

        return [make_block(block_num) for block_num in range(block_count)]

    


    def _build_face_storage_map(self):
        # Side effects:
        # - fill in GPUBlock.extfaces
        # - set self.aligned_boundary_floats
        fsm = {}

        face_index_list_register = {}
        self.index_lists = []
        def get_face_index_list_number(il):
            il_tup = tuple(il)
            try:
                return face_index_list_register[il_tup]
            except KeyError:
                self.index_lists.append(numpy.array(il))
                return face_index_list_register.setdefault(
                        tuple(il), len(face_index_list_register))

        def make_int_face(face_pair_side):
            el = self.mesh.elements[face_pair_side.element_id]
            elface = (el, face_pair_side.face_id)

            block = self.blocks[self.partition[el.id]]
            iln = get_face_index_list_number(
                    ldis.face_indices()[face_pair_side.face_id])
            result = GPUInteriorFaceStorage(
                elface, 
                cpu_slice=self.find_el_range(el.id), 
                native_index_list_id=iln,
                native_block=block, 
                native_block_el_num=block.get_el_index(el), 
                face_pair_side=face_pair_side
                )

            assert elface not in fsm
            fsm[elface] = result
            return result


        def narrow_ilist(in_el_ilist, native_el_ilist):
            return get_read_from_map_from_permutation

            el_dof_to_face_dof = dict(
                    (el_dof, i)
                    for i, el_dof in enumerate(native_el_ilist))
            return tuple(el_dof_to_face_dof[el_dof]
                    for el_dof in in_el_ilist)

        block_dofs = self.int_dof_count

        int_fg, = self.face_groups
        ldis = int_fg.ldis_loc
        assert ldis == int_fg.ldis_opp

        id_face_index_list = tuple(xrange(ldis.face_node_count()))
        id_face_index_list_number = get_face_index_list_number(id_face_index_list)
        assert id_face_index_list_number == 0

        from pytools import single_valued
        for fp in int_fg.face_pairs:
            face1 = make_int_face(fp.loc)
            face2 = make_int_face(fp.opp)
            face1.opposite = face2
            face2.opposite = face1

            def apply_write_map(wmap, sequence):
                result = [None] * len(sequence)
                for wm_i, seq_i in zip(wmap, sequence):
                    result[wm_i] = seq_i
                assert None not in result
                return tuple(result)

            f_ind = ldis.face_indices()
            face1_in_el_ilist = tuple(int_fg.index_lists[
                fp.loc.face_index_list_number])
            face2_in_el_ilist = tuple(int_fg.index_lists[
                fp.opp.face_index_list_number])
            opp_write_map = tuple(int_fg.index_lists[fp.opp_native_write_map])
                
            gfiln = get_face_index_list_number
            face1.global_int_flux_index_list_id = gfiln(face1_in_el_ilist)
            face1.global_ext_flux_index_list_id = gfiln(face2_in_el_ilist)

            face2.global_int_flux_index_list_id = gfiln(
                    apply_write_map(opp_write_map, face2_in_el_ilist))
            face2.global_ext_flux_index_list_id = gfiln(
                    apply_write_map(opp_write_map, face1_in_el_ilist))

            from pytools import get_write_to_map_from_permutation as gwtm
            assert gwtm(face2_in_el_ilist, f_ind[fp.opp.face_id]) == opp_write_map
            face1.opp_write_index_list_id = gfiln(
                    gwtm(face2_in_el_ilist, f_ind[fp.opp.face_id]))
            face2.opp_write_index_list_id = gfiln(
                    gwtm(
                        apply_write_map(opp_write_map, face1_in_el_ilist),
                        f_ind[fp.loc.face_id]))

            if face1.native_block != face2.native_block:
                # allocate resources for duplicated face
                for loc_face, opp_face in [(face1, face2), (face2, face1)]:
                    loc_face.dup_block = opp_face.native_block
                    loc_face.dup_ext_face_number = \
                            loc_face.dup_block.register_ext_face_to_me(loc_face)
                    loc_face.native_block.register_ext_face_from_me(loc_face)

                # The existing index lists refer to their
                # locations within full elements. Because only the
                # dofs that are actually on the face are duplicated,
                # and not the whole element, we need to narrow down
                # the index list to only the face dofs.

                # In addition, while the "loc" part of the facepair is already
                # in its native face order, the "opp" side has been permuted
                # to match its face dof order. This needs to be reversed for
                # face2.

                from pytools import get_read_from_map_from_permutation as grfm
                face1_in_face_ilist = grfm(
                        f_ind[fp.loc.face_id], face1_in_el_ilist)
                face2_in_face_ilist = grfm(
                        f_ind[fp.opp.face_id], face2_in_el_ilist)

                face1.dup_int_flux_index_list_id = gfiln(face1_in_el_ilist)
                face1.dup_ext_flux_index_list_id = gfiln(face2_in_face_ilist)

                face2.dup_int_flux_index_list_id = gfiln(
                        apply_write_map(opp_write_map, face2_in_el_ilist))
                face2.dup_ext_flux_index_list_id = gfiln(
                        apply_write_map(opp_write_map, face1_in_face_ilist))
            else:
                # Both faces in the same block. They retain
                # their respective index lists.

                gfiln = get_face_index_list_number
                face1.dup_int_flux_index_list_id = gfiln(face1_in_el_ilist)
                face1.dup_ext_flux_index_list_id = gfiln(face2_in_el_ilist)

                face2.dup_int_flux_index_list_id = gfiln(
                        apply_write_map(opp_write_map, face2_in_el_ilist))
                face2.dup_ext_flux_index_list_id = gfiln(
                        apply_write_map(opp_write_map, face1_in_el_ilist))



        self.aligned_boundary_floats = 0
        from hedge.mesh import TAG_ALL
        for bdry_fg in self.get_boundary(TAG_ALL).face_groups:
            assert ldis == bdry_fg.ldis_loc
            aligned_fnc = self.devdata.align_dtype(ldis.face_node_count(), 
                    self.plan.float_size)
            for fp in bdry_fg.face_pairs:
                assert fp.opp.element_id == hedge._internal.INVALID_ELEMENT
                assert (tuple(bdry_fg.index_lists[fp.opp.face_index_list_number]) 
                        == id_face_index_list)

                face1 = make_int_face(fp.loc)
                face2 = GPUBoundaryFaceStorage(
                        fp.opp.el_base_index,
                        self.aligned_boundary_floats,
                        fp.opp
                        )
                self.aligned_boundary_floats += aligned_fnc
                face1.opposite = face2
                face2.opposite = face1
                face2.dup_block = face1.native_block
                face2.dup_ext_face_number = face1.native_block.register_ext_face_to_me(face2)
                face1.native_block.register_ext_face_from_me(face1)

                face1.global_int_flux_index_list_id = \
                        face1.dup_int_flux_index_list_id = \
                        get_face_index_list_number(
                                tuple(bdry_fg.index_lists[
                                    fp.loc.face_index_list_number]))
                face1.global_ext_flux_index_list_id = \
                        face1.dup_ext_flux_index_list_id = \
                        get_face_index_list_number(
                                tuple(bdry_fg.index_lists[
                                    fp.opp.face_index_list_number]))

        for block in self.blocks:
            assert len(block.ext_faces_from_me) == len(block.ext_faces_to_me)

        return fsm




    def gpu_dof_count(self):
        return self.int_dof_count * len(self.blocks)

    def volume_to_gpu(self, field):
        from hedge.tools import log_shape
        ls = log_shape(field)
        if ls != ():
            result = numpy.array(ls, dtype=object)

            from pytools import indices_in_shape

            for i in indices_in_shape(ls):
                result[i] = self.volume_to_gpu(field[i])
            return result
        else:
            copy_vec = numpy.empty((self.gpu_dof_count(),), dtype=numpy.float32)

            block_offset = 0
            for block in self.blocks:
                face_length = block.local_discretization.face_node_count()
                el_length = block.local_discretization.node_count()

                el_offset = block_offset
                for cpu_slice in block.cpu_slices:
                    copy_vec[el_offset:el_offset+el_length] = field[cpu_slice]
                    el_offset += el_length

                block_offset += self.int_dof_count

            return gpuarray.to_gpu(copy_vec)

    def volume_from_gpu(self, field):
        from hedge.tools import log_shape
        ls = log_shape(field)
        if ls != ():
            result = numpy.array(ls, dtype=object)

            from pytools import indices_in_shape

            for i in indices_in_shape(ls):
                result[i] = self.from_gpu(field[i])
            return result
        else:
            copied_vec = field.get(pagelocked=True)
            result = numpy.empty(shape=(len(self),), dtype=copied_vec.dtype)

            block_offset = 0
            for block in self.blocks:
                el_length = block.local_discretization.node_count()

                el_offset = block_offset
                for cpu_slice in block.cpu_slices:
                    result[cpu_slice] = copied_vec[el_offset:el_offset+el_length]
                    el_offset += el_length

                block_offset += self.int_dof_count

            return result

    @memoize_method
    def gpu_boundary_embedding(self, tag):
        """Return an array of indices embedding a CPU boundary
        field for C{tag} into the GPU boundary field."""

        bdry = self.get_boundary(tag)
        result = numpy.empty(
                (len(bdry.nodes),),
                dtype=numpy.intp)
        result.fill(-1)

        cpu_base = 0
        for elface in self.mesh.tag_to_boundary.get(tag, []):
            face_stor = self.face_storage_map[elface]
            bdry_stor = face_stor.opposite
            assert isinstance(bdry_stor, GPUBoundaryFaceStorage)

            face_len = (bdry_stor.opposite.native_block
                    .local_discretization.face_node_count())
            gpu_base = bdry_stor.gpu_bdry_index_in_floats
            result[cpu_base:cpu_base+face_len] = \
                    numpy.arange(gpu_base, gpu_base+face_len)
            cpu_base += face_len

        assert (result>=0).all()
        return result

    def boundary_to_gpu(self, tag, field):
        from hedge.tools import log_shape
        ls = log_shape(field)
        if ls != ():
            from pytools import indices_in_shape
            result = numpy.array(ls, dtype=object)
            for i in indices_in_shape(shape):
                result[i] = self.boundary_to_gpu(field[i], tag)
            return result
        else:
            result = cuda.pagelocked_empty(
                    (self.aligned_boundary_floats,),
                    dtype=field.dtype)

            # The boundary cannot be completely uninitialized,
            # because it might contain NaNs. If a certain part of the
            # boundary is to be ignored, it is simply multiplied by
            # zero in the kernel, which won't make the NaNs disappear.

            # Therefore, as a second best solution, fill the boundary
            # with a bogus value so that we can tell if it actually
            # enters the computation.

            result.fill(17) 
            result[self.gpu_boundary_embedding(tag)] = field
            return gpuarray.to_gpu(result)

    # vector construction -----------------------------------------------------
    def volume_empty(self, shape=(), dtype=None):
        if dtype is None:
            dtype = self.plan.float_type

        return gpuarray.empty(shape+(self.gpu_dof_count(),), dtype=dtype)

    def volume_zeros(self, shape=()):
        result = self.volume_empty(shape)
        result.fill(0)
        return result

    def interpolate_volume_function(self, f, dtype=None):
        s = hedge.discretization.Discretization

        def tgt_factory(shape, dtype):
            return s.volume_empty(self, shape, dtype)

        return self.volume_to_gpu(
                s.interpolate_volume_function(self, f, tgt_factory))

    def _new_bdry(self, tag, shape, create_func, dtype):
        if dtype is None:
            dtype = self.default_scalar_type

        if shape == ():
            return create_func((self.aligned_boundary_floats), dtype=dtype)

        result = numpy.empty(shape, dtype=object)
        from pytools import indices_in_shape
        bdry = self.get_boundary(TAG_ALL)
        for i in indices_in_shape(shape):
            result[i] = create_func((self.aligned_boundary_floats), dtype=dtype)
        return result
    
    def boundary_empty(self, tag=hedge.mesh.TAG_ALL, shape=(), dtype=None):
        return self._new_bdry(tag, shape, gpuarray.empty, dtype)

    def boundary_zeros(self, tag=hedge.mesh.TAG_ALL, shape=(), dtype=None):
        return self._new_bdry(tag, shape, gpuarray.zeros, dtype)

    def interpolate_boundary_function(self, f, tag=hedge.mesh.TAG_ALL):
        s = hedge.discretization.Discretization
        def tgt_factory(shape, tag, dtype):
            return s.boundary_zeros(self, shape, tag, dtype)

        return self.boundary_to_gpu(tag,
                s.interpolate_boundary_function(self, f, tag, tgt_factory))

    def boundary_normals(self, tag=hedge.mesh.TAG_ALL):
        raise NotImplementedError

    def volumize_boundary_field(self, bfield, tag=hedge.mesh.TAG_ALL):
        raise NotImplementedError

    def boundarize_volume_field(self, field, tag=hedge.mesh.TAG_ALL):
        raise NotImplementedError

    # host vector construction ------------------------------------------------
    s = hedge.discretization.Discretization
    host_volume_empty = s.volume_empty
    host_volume_zeros = s.volume_zeros 
    del s

    # optemplate processing ---------------------------------------------------
    def compile(self, optemplate):
        from hedge.cuda.execute import OpTemplateWithEnvironment
        return OpTemplateWithEnvironment(self, optemplate)