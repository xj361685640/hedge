#!/usr/bin/env python
# -*- coding: latin-1 -*-

# Hedge - the Hybrid'n'Easy DG Environment
# Copyright (C) 2007 Andreas Kloeckner
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import glob
import os
import os.path
import sys

try:
    execfile("siteconf.py")
except IOError:
    print "*** Please run configure first."
    sys.exit(1)

from distutils.core import setup,Extension

def non_matching_config():
    print "*** The version of your configuration template does not match"
    print "*** the version of the setup script. Please re-run configure."
    sys.exit(1)

try:
    HEDGE_CONF_TEMPLATE_VERSION
except NameError:
    non_matching_config()

if HEDGE_CONF_TEMPLATE_VERSION != 3:
    non_matching_config()

INCLUDE_DIRS = ["src/cpp"] \
        + BOOST_MATH_TOOLKIT_INCLUDE_DIRS \
        + BOOST_BINDINGS_INCLUDE_DIRS \
        + BOOST_INCLUDE_DIRS

LIBRARY_DIRS = BOOST_LIBRARY_DIRS
LIBRARIES = BPL_LIBRARIES

EXTRA_DEFINES = {}
EXTRA_INCLUDE_DIRS = []
EXTRA_LIBRARY_DIRS = []
EXTRA_LIBRARIES = []

BLAS_INCLUDE_DIRS = []
USE_BLAS = HAVE_BLAS

def handle_component(comp):
    if globals()["USE_"+comp]:
        globals()["EXTRA_DEFINES"]["USE_"+comp] = 1
        globals()["EXTRA_INCLUDE_DIRS"] += globals()[comp+"_INCLUDE_DIRS"]
        globals()["EXTRA_LIBRARY_DIRS"] += globals()[comp+"_LIBRARY_DIRS"]
        globals()["EXTRA_LIBRARIES"] += globals()[comp+"_LIBRARIES"]

handle_component("BLAS")
handle_component("SILO")

ext_modules=[
        Extension("_internal", 
            ["src/wrapper/wrap_main.cpp", 
                "src/wrapper/wrap_base.cpp", 
                "src/wrapper/wrap_special_function.cpp", 
                "src/wrapper/wrap_flux.cpp", 
                "src/wrapper/wrap_op_target.cpp", 
                "src/wrapper/wrap_volume_operators.cpp", 
                "src/wrapper/wrap_face_operators.cpp", 
                "src/wrapper/wrap_index_subset.cpp", 
                ],
            include_dirs=INCLUDE_DIRS + EXTRA_INCLUDE_DIRS,
            library_dirs=LIBRARY_DIRS + EXTRA_LIBRARY_DIRS,
            libraries=LIBRARIES + EXTRA_LIBRARIES,
            extra_compile_args=EXTRA_COMPILE_ARGS,
            define_macros=list(EXTRA_DEFINES.iteritems()),
            )]

if USE_SILO:
    ext_modules.append(
            Extension("_silo", 
                ["src/wrapper/wrap_silo.cpp", 
                    ],
                include_dirs=INCLUDE_DIRS + EXTRA_INCLUDE_DIRS,
                library_dirs=LIBRARY_DIRS + EXTRA_LIBRARY_DIRS,
                libraries=LIBRARIES + EXTRA_LIBRARIES,
                extra_compile_args=EXTRA_COMPILE_ARGS,
                define_macros=list(EXTRA_DEFINES.iteritems()),
                ))




setup(name="hedge",
      version="0.90",
      description="The Hybrid-and-Easy Discontinuous Galerkin Environment",
      author=u"Andreas Kloeckner",
      author_email="inform@tiker.net",
      license = "GPLv3",
      url="http://news.tiker.net/software/hedge",
      packages=["hedge"],
      package_dir={"hedge": "src/python"},
      ext_package="hedge",
      ext_modules=ext_modules,
      data_files=[("include/hedge", glob.glob("src/cpp/*.hpp"))],
     )
