"""Miscellaneous helper facilities."""

from __future__ import division

__copyright__ = "Copyright (C) 2007 Andreas Kloeckner"

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
import hedge._internal

# don't import stuff from here--this is purely for backward-compatibility
from pytools.obj_array import *
from hedge.optemplate.tools import *
from hedge.tools.math import *
from hedge.tools.linalg import *
from hedge.tools.convergence import *
from hedge.tools.flops import *
from hedge.tools.debug import *
from hedge.tools.indexing import *
from hedge.flux.tools import *




AffineMap = hedge._internal.AffineMap
def _affine_map___getinitargs__(self):
    return self.matrix, self.vector

AffineMap.__getinitargs__ = _affine_map___getinitargs__




class Rotation(AffineMap):
    def __init__(self, angle):
        # FIXME: Add axis, make multidimensional
        from math import sin, cos
        AffineMap.__init__(self,
                numpy.array([
                    [cos(angle), sin(angle)],
                    [-sin(angle), cos(angle)]]),
                numpy.zeros((2,)))




class Reflection(AffineMap):
    def __init__(self, axis, dim):
        mat = numpy.identity(dim)
        mat[axis,axis] = -1
        AffineMap.__init__(self, mat, numpy.zeros((dim,)))




# small utilities -------------------------------------------------------------
def is_zero(x):
    return isinstance(x, (int, float)) and x == 0




class Closable(object):
    def __init__(self):
        self.is_closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        if not self.is_closed:
            # even if close attempt fails, consider ourselves closed still
            try:
                self.do_close()
            finally:
                self.is_closed = True




def reverse_lookup_table(lut):
    result = [None] * len(lut)
    for key, value in enumerate(lut):
        result[value] = key
    return result
