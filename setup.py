# Copyright (c) Meta Platforms, Inc. and affiliates.
# Copyright 2024 Arm Limited and/or its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# Part of this code is from pybind11 cmake_example:
# https://github.com/pybind/cmake_example/blob/master/setup.py so attach the
# license below.

# Copyright (c) 2016 The Pybind Development Team, All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its contributors
#    may be used to endorse or promote products derived from this software
#    without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# You are under no obligation whatsoever to provide any bug fixes, patches, or
# upgrades to the features, functionality or performance of the source code
# ("Enhancements") to anyone; however, if you choose to make your Enhancements
# available either publicly, or directly to the author of this software, without
# imposing a separate written license agreement for such Enhancements, then you
# hereby grant the following license: a non-exclusive, royalty-free perpetual
# license to install, use, modify, prepare derivative works, incorporate into
# other computer software, distribute, and sublicense such enhancements or
# derivative works thereof, in binary and source code form.

import contextlib
import os
import platform
import re
import site
import sys

# Import this before distutils so that setuptools can intercept the distuils
# imports.
import setuptools  # noqa: F401 # usort: skip
import subprocess

from distutils import log
from distutils.sysconfig import get_python_lib
from pathlib import Path
from typing import List, Optional

from setuptools import Extension, setup
from setuptools.command.build import build
from setuptools.command.build_ext import build_ext
from setuptools.command.build_py import build_py


class ShouldBuild:
    """Indicates whether to build various components."""

    @staticmethod
    def _is_env_enabled(env_var: str, default: bool = False) -> bool:
        val = os.environ.get(env_var, None)
        if val is None:
            return default
        if val in ("OFF", "0", ""):
            return False
        return True

    @classmethod
    def pybindings(cls) -> bool:
        return cls._is_env_enabled("EXECUTORCH_BUILD_PYBIND", default=False)

    @classmethod
    def training(cls) -> bool:
        return cls._is_env_enabled("EXECUTORCH_BUILD_TRAINING", default=False)

    @classmethod
    def llama_custom_ops(cls) -> bool:
        return cls._is_env_enabled("EXECUTORCH_BUILD_KERNELS_CUSTOM_AOT", default=True)

    @classmethod
    def flatc(cls) -> bool:
        return cls._is_env_enabled("EXECUTORCH_BUILD_FLATC", default=True)


class Version:
    """Static strings that describe the version of the pip package."""

    # Cached values returned by the properties.
    __root_dir_attr: Optional[str] = None
    __string_attr: Optional[str] = None
    __git_hash_attr: Optional[str] = None

    @classmethod
    def _root_dir(cls) -> str:
        """The path to the root of the git repo."""
        if cls.__root_dir_attr is None:
            # This setup.py file lives in the root of the repo.
            cls.__root_dir_attr = str(Path(__file__).parent.resolve())
        return str(cls.__root_dir_attr)

    @classmethod
    def git_hash(cls) -> Optional[str]:
        """The current git hash, if known."""
        if cls.__git_hash_attr is None:
            import subprocess

            try:
                cls.__git_hash_attr = (
                    subprocess.check_output(
                        ["git", "rev-parse", "HEAD"], cwd=cls._root_dir()
                    )
                    .decode("ascii")
                    .strip()
                )
            except subprocess.CalledProcessError:
                cls.__git_hash_attr = ""  # Non-None but empty.
        # A non-None but empty value indicates that we don't know it.
        return cls.__git_hash_attr if cls.__git_hash_attr else None

    @classmethod
    def string(cls) -> str:
        """The version string."""
        if cls.__string_attr is None:
            # If set, BUILD_VERSION should override any local version
            # information. CI will use this to manage, e.g., release vs. nightly
            # versions.
            version = os.getenv("BUILD_VERSION", "").strip()
            if not version:
                # Otherwise, read the version from a local file and add the git
                # commit if available.
                version = (
                    open(os.path.join(cls._root_dir(), "version.txt")).read().strip()
                )
                if cls.git_hash():
                    version += "+" + cls.git_hash()[:7]  # type: ignore[index]
            cls.__string_attr = version
        return cls.__string_attr

    @classmethod
    def write_to_python_file(cls, path: str) -> None:
        """Creates a file similar to PyTorch core's `torch/version.py`."""
        lines = [
            "from typing import Optional",
            '__all__ = ["__version__", "git_version"]',
            f'__version__ = "{cls.string()}"',
            # A string or None.
            f"git_version: Optional[str] = {repr(cls.git_hash())}",
        ]
        with open(path, "w") as fp:
            fp.write("\n".join(lines) + "\n")


# The build type is determined by the DEBUG environment variable. If DEBUG is
# set to a non-empty value, the build type is Debug. Otherwise, the build type
# is Release.
def get_build_type(is_debug=None) -> str:
    debug = int(os.environ.get("DEBUG", 0) or 0) if is_debug is None else is_debug
    cfg = "Debug" if debug else "Release"
    return cfg


def get_dynamic_lib_name(name: str) -> str:
    if platform.system() == "Windows":
        return name + ".dll"
    elif platform.system() == "Darwin":
        return "lib" + name + ".dylib"
    else:
        return "lib" + name + ".so"


def get_executable_name(name: str) -> str:
    if platform.system() == "Windows":
        return name + ".exe"
    else:
        return name


class _BaseExtension(Extension):
    """A base class that maps an abstract source to an abstract destination."""

    def __init__(self, src: str, dst: str, name: str):
        # Source path; semantics defined by the subclass.
        self.src: str = src

        # Destination path relative to a namespace defined elsewhere. If this ends
        # in "/", it is treated as a directory. If this is "", it is treated as the
        # root of the namespace.
        # Destination path; semantics defined by the subclass.
        self.dst: str = dst

        # Other parts of setuptools expects .name to exist. For actual extensions
        # this can be the module path, but otherwise it should be somehing unique
        # that doesn't look like a module path.
        self.name: str = name

        super().__init__(name=self.name, sources=[])

    def src_path(self, installer: "InstallerBuildExt") -> Path:
        """Returns the path to the source file, resolving globs.

        Args:
            installer: The InstallerBuildExt instance that is installing the
                file.
        """
        # Share the cmake-out location with CustomBuild.
        build_cmd = installer.get_finalized_command("build")
        if "%CMAKE_CACHE_DIR%" in self.src:
            if not hasattr(build_cmd, "cmake_cache_dir"):
                raise RuntimeError(
                    f"Extension {self.name} has a src {self.src} that contains"
                    " %CMAKE_CACHE_DIR% but CMake does not run in the `build` "
                    "command. Please double check if the command is correct."
                )
            else:
                build_dir = Path(build_cmd.cmake_cache_dir)
        else:
            # If the src path doesn't contain %CMAKE_CACHE_DIR% placeholder,
            # try to find it under the current directory.
            build_dir = Path(".")

        src_path = self.src.replace("%CMAKE_CACHE_DIR%/", "")

        cfg = get_build_type(installer.debug)

        if os.name == "nt":
            # Replace %BUILD_TYPE% with the current build type.
            src_path = src_path.replace("%BUILD_TYPE%", cfg)
        else:
            # Remove %BUILD_TYPE% from the path.
            src_path = src_path.replace("/%BUILD_TYPE%", "")

        # Construct the full source path, resolving globs. If there are no glob
        # pattern characters, this will just ensure that the source file exists.
        srcs = tuple(build_dir.glob(src_path))
        if len(srcs) != 1:
            raise ValueError(
                f"Expecting exactly 1 file matching {self.src} in {build_dir}, "
                f"found {repr(srcs)}. Resolved src pattern: {src_path}."
            )
        return srcs[0]

    def inplace_dir(self, installer: "InstallerBuildExt") -> Path:
        """Returns the path of this file to be installed to, under inplace mode.

        It will be a relative path to the project root directory. For more info
        related to inplace/editable mode, please checkout this doc:
        https://setuptools.pypa.io/en/latest/userguide/development_mode.html
        """
        raise NotImplementedError()


class BuiltFile(_BaseExtension):
    """An extension that installs a single file that was built by cmake.

    This isn't technically a `build_ext` style python extension, but there's no
    dedicated command for installing arbitrary data. It's convenient to use
    this, though, because it lets us manage the files to install as entries in
    `ext_modules`.
    """

    def __init__(
        self,
        src_dir: str,
        src_name: str,
        dst: str,
        is_executable: bool = False,
        is_dynamic_lib: bool = False,
    ):
        """Initializes a BuiltFile.

        Args:
            src_dir: The directory of the file to install, relative to the cmake-out
                directory. A placeholder %BUILD_TYPE% will be replaced with the build
                type for multi-config generators (like Visual Studio) where the build
                output is in a subdirectory named after the build type. For single-
                config generators (like Makefile Generators or Ninja), this placeholder
                will be removed.
            src_name: The name of the file to install
            dst: The path to install to, relative to the root of the pip
                package. If dst ends in "/", it is treated as a directory.
                Otherwise it is treated as a filename.
            is_executable: If True, the file is an executable. This is used to
                determine the destination filename for executable.
            is_dynamic_lib: If True, the file is a dynamic library. This is used
                to determine the destination filename for dynamic library.
        """
        if is_executable and is_dynamic_lib:
            raise ValueError("is_executable and is_dynamic_lib cannot be both True.")
        if is_executable:
            src_name = get_executable_name(src_name)
        elif is_dynamic_lib:
            src_name = get_dynamic_lib_name(src_name)
        src = os.path.join(src_dir, src_name)
        # This is not a real extension, so use a unique name that doesn't look
        # like a module path. Some of setuptools's autodiscovery will look for
        # extension names with prefixes that match certain module paths.
        super().__init__(src=src, dst=dst, name=f"@EXECUTORCH_BuiltFile_{src}:{dst}")

    def dst_path(self, installer: "InstallerBuildExt") -> Path:
        """Returns the path to the destination file.

        Args:
            installer: The InstallerBuildExt instance that is installing the
                file.
        """
        dst_root = Path(installer.build_lib).resolve()

        if self.dst.endswith("/"):
            # Destination looks like a directory. Use the basename of the source
            # file for its final component.
            return dst_root / Path(self.dst) / self.src_path(installer).name
        else:
            # Destination looks like a file.
            return dst_root / Path(self.dst)

    def inplace_dir(self, installer: "InstallerBuildExt") -> Path:
        """For a `BuiltFile`, we use self.dst as its inplace directory path.
        Need to handle directory vs file.
        """
        # HACK: get rid of the leading "executorch" in ext.dst.
        # This is because we don't have a root level "executorch" module.
        package_dir = self.dst.removeprefix("executorch/")
        # If dst is a file, use it's directory
        if not package_dir.endswith("/"):
            package_dir = os.path.dirname(package_dir)
        return Path(package_dir)


class BuiltExtension(_BaseExtension):
    """An extension that installs a python extension that was built by cmake."""

    def __init__(self, src: str, modpath: str):
        """Initializes a BuiltExtension.

        Args:
            src: The path to the file to install (typically a shared library),
                relative to the cmake-out directory. May be an fnmatch-style
                glob that matches exactly one file. If the path ends in `.so`,
                this class will also look for similarly-named `.dylib` files.
            modpath: The dotted path of the python module that maps to the
                extension.
        """
        assert (
            "/" not in modpath
        ), f"modpath must be a dotted python module path: saw '{modpath}'"
        # This is a real extension, so use the modpath as the name.
        super().__init__(src=f"%CMAKE_CACHE_DIR%/{src}", dst=modpath, name=modpath)

    def src_path(self, installer: "InstallerBuildExt") -> Path:
        """Returns the path to the source file, resolving globs.

        Args:
            installer: The InstallerBuildExt instance that is installing the
                file.
        """
        try:
            return super().src_path(installer)
        except ValueError:
            # Probably couldn't find the file. If the path ends with .so, try
            # looking for a .dylib file instead, in case we're running on macos.
            if self.src.endswith(".so"):
                dylib_src = re.sub(r"\.so$", ".dylib", self.src)
                return BuiltExtension(src=dylib_src, modpath=self.dst).src_path(
                    installer
                )
            else:
                raise

    def dst_path(self, installer: "InstallerBuildExt") -> Path:
        """Returns the path to the destination file.

        Args:
            installer: The InstallerBuildExt instance that is installing the
                file.
        """
        # Our destination is a dotted module path. get_ext_fullpath() returns
        # the relative path to the .so/.dylib/etc. file that maps to the module
        # path: that's the file we're creating.
        return Path(installer.get_ext_fullpath(self.dst))

    def inplace_dir(self, installer: "InstallerBuildExt") -> Path:
        """For BuiltExtension, deduce inplace dir path from extension name."""
        build_py = installer.get_finalized_command("build_py")
        modpath = self.name.split(".")
        package = ".".join(modpath[:-1])
        package_dir = os.path.abspath(build_py.get_package_dir(package))

        return Path(package_dir)


class InstallerBuildExt(build_ext):
    """Installs files that were built by cmake."""

    def __init__(self, *args, **kwargs):
        self._ran_build = False
        super().__init__(*args, **kwargs)

    def run(self):
        # Run the build command first in editable mode. Since `build` command
        # will also trigger `build_ext` command, only run this once.
        if self._ran_build:
            return

        if self.editable_mode:
            self._ran_build = True
            self.run_command("build")
        super().run()

    def copy_extensions_to_source(self) -> None:
        """For each extension in `ext_modules`, we need to copy the extension
        file from the build directory to the correct location in the local
        directory.

        This should only be triggered when inplace mode (editable mode) is enabled.

        Args:

        Returns:
        """
        for ext in self.extensions:
            package_dir = ext.inplace_dir(self)

            # Ensure that the destination directory exists.
            self.mkpath(os.fspath(package_dir))

            regular_file = ext.src_path(self)
            inplace_file = os.path.join(
                package_dir, os.path.basename(ext.dst_path(self))
            )

            # Always copy, even if source is older than destination, to ensure
            # that the right extensions for the current Python/platform are
            # used.
            if os.path.exists(regular_file) or not ext.optional:
                self.copy_file(regular_file, inplace_file, level=self.verbose)

            if ext._needs_stub:
                inplace_stub = self._get_equivalent_stub(ext, inplace_file)
                self._write_stub_file(inplace_stub, ext, compile=True)
                # Always compile stub and remove the original (leave the cache behind)
                # (this behaviour was observed in previous iterations of the code)

    # TODO(dbort): Depend on the "build" command to ensure it runs first

    def build_extension(self, ext: _BaseExtension) -> None:
        src_file: Path = ext.src_path(self)
        dst_file: Path = ext.dst_path(self)

        # Ensure that the destination directory exists.
        if not dst_file.parent.exists():
            self.mkpath(os.fspath(dst_file.parent))

        # Copy the file.
        self.copy_file(os.fspath(src_file), os.fspath(dst_file))

        # Ensure that the destination file is writable, even if the source was
        # not. build_py does this by passing preserve_mode=False to copy_file,
        # but that would clobber the X bit on any executables. TODO(dbort): This
        # probably won't work on Windows.
        if not os.access(src_file, os.W_OK):
            # Make the file writable. This should respect the umask.
            os.chmod(src_file, os.stat(src_file).st_mode | 0o222)


class CustomBuildPy(build_py):
    """Copies platform-independent files from the source tree into the output
    package directory.

    Override it so we can copy some files to locations that don't match their
    original relative locations.

    Standard setuptools features like package_data and MANIFEST.in can only
    include or exclude a file in the source tree; they don't have a way to map
    a file to a different relative location under the output package directory.
    """

    def run(self):
        # Copy python files to the output directory. This set of files is
        # defined by the py_module list and package_data patterns.
        build_py.run(self)

        # dst_root is the root of the `executorch` module in the output package
        # directory. build_lib is the platform-independent root of the output
        # package, and will look like `pip-out/lib`. It can contain multiple
        # python packages, so be sure to copy the files into the `executorch`
        # package subdirectory.
        if self.editable_mode:
            # In editable mode, the package directory is the original source directory
            dst_root = self.get_package_dir(".")
        else:
            dst_root = os.path.join(self.build_lib, self.get_package_dir("executorch"))

        # Create the version file.
        Version.write_to_python_file(os.path.join(dst_root, "version.py"))

        # Manually copy files into the output package directory. These are
        # typically python "resource" files that will live alongside the python
        # code that uses them.
        src_to_dst = [
            # TODO(dbort): See if we can add a custom pyproject.toml section for
            # these, instead of hard-coding them here. See
            # https://setuptools.pypa.io/en/latest/userguide/extension.html
            ("schema/scalar_type.fbs", "exir/_serialize/scalar_type.fbs"),
            ("schema/program.fbs", "exir/_serialize/program.fbs"),
            (
                "devtools/bundled_program/schema/bundled_program_schema.fbs",
                "devtools/bundled_program/serialize/bundled_program_schema.fbs",
            ),
            (
                "devtools/bundled_program/schema/scalar_type.fbs",
                "devtools/bundled_program/serialize/scalar_type.fbs",
            ),
            # Install executorch-wheel-config.cmake to pip package.
            (
                "build/executorch-wheel-config.cmake",
                "share/cmake/executorch-config.cmake",
            ),
        ]
        # Copy all the necessary headers into include/executorch/ so that they can
        # be found in the pip package. This is the subset of headers that are
        # essential for building custom ops extensions.
        # TODO: Use cmake to gather the headers instead of hard-coding them here.
        # For example: https://discourse.cmake.org/t/installing-headers-the-modern-
        # way-regurgitated-and-revisited/3238/3
        for include_dir in [
            "runtime/core/",
            "runtime/kernel/",
            "runtime/platform/",
            "extension/kernel_util/",
            "extension/tensor/",
            "extension/threadpool/",
        ]:
            src_list = Path(include_dir).rglob("*.h")
            for src in src_list:
                src_to_dst.append(
                    (str(src), os.path.join("include/executorch", str(src)))
                )
        for src, dst in src_to_dst:
            dst = os.path.join(dst_root, dst)

            # When modifying the filesystem, use the self.* methods defined by
            # Command to benefit from the same logging and dry_run logic as
            # setuptools.

            # Ensure that the destination directory exists.
            self.mkpath(os.path.dirname(dst))
            # Follow the example of the base build_py class by not preserving
            # the mode. This ensures that the output file is read/write even if
            # the input file is read-only.
            self.copy_file(src, dst, preserve_mode=False)


class Buck2EnvironmentFixer(contextlib.AbstractContextManager):
    """Removes HOME from the environment when running as root.

    This script is sometimes run as root in docker containers. buck2 doesn't
    allow running as root unless $HOME is owned by root or is not set.

    TODO(pytorch/test-infra#5091): Remove this once the CI jobs stop running as
    root.
    """

    def __init__(self):
        self.saved_env = {}

    def __enter__(self):
        if os.name != "nt" and os.geteuid() == 0 and "HOME" in os.environ:
            log.info("temporarily unsetting HOME while running as root")
            self.saved_env["HOME"] = os.environ.pop("HOME")
        return self

    def __exit__(self, *args, **kwargs):
        if "HOME" in self.saved_env:
            log.info("restored HOME")
            os.environ["HOME"] = self.saved_env["HOME"]


# TODO(dbort): For editable wheels, may need to update get_source_files(),
# get_outputs(), and get_output_mapping() to satisfy
# https://setuptools.pypa.io/en/latest/userguide/extension.html#setuptools.command.build.SubCommand.get_output_mapping


class CustomBuild(build):
    def initialize_options(self):
        super().initialize_options()
        # The default build_base directory is called "build", but we have a
        # top-level directory with that name. Setting build_base in setup()
        # doesn't affect this, so override the core build command.
        #
        # See build.initialize_options() in
        # setuptools/_distutils/command/build.py for the default.
        self.build_base = "pip-out"

        # Default build parallelism based on number of cores, but allow
        # overriding through the environment.
        default_parallel = str(os.cpu_count() - 1)
        self.parallel = os.environ.get("CMAKE_BUILD_PARALLEL_LEVEL", default_parallel)

    def run(self):
        self.dump_options()

        cfg = get_build_type(self.debug)

        # get_python_lib() typically returns the path to site-packages, where
        # all pip packages in the environment are installed.
        cmake_prefix_path = os.environ.get("CMAKE_PREFIX_PATH", get_python_lib())

        # The root of the repo should be the current working directory. Get
        # the absolute path.
        repo_root = os.fspath(Path.cwd())

        # If blank, the cmake build system will find an appropriate binary.
        buck2 = os.environ.get(
            "BUCK2_EXECUTABLE", os.environ.get("BUCK2", os.environ.get("BUCK", ""))
        )

        cmake_args = [
            f"-DBUCK2={buck2}",
            f"-DPYTHON_EXECUTABLE={sys.executable}",
            # Let cmake calls like `find_package(Torch)` find cmake config files
            # like `TorchConfig.cmake` that are provided by pip packages.
            f"-DCMAKE_PREFIX_PATH={cmake_prefix_path}",
            f"-DCMAKE_BUILD_TYPE={cfg}",
            # Enable logging even when in release mode. We are building for
            # desktop, where saving a few kB is less important than showing
            # useful error information to users.
            "-DEXECUTORCH_ENABLE_LOGGING=ON",
            "-DEXECUTORCH_LOG_LEVEL=Info",
            "-DCMAKE_OSX_DEPLOYMENT_TARGET=10.15",
            # The separate host project is only required when cross-compiling,
            # and it can cause build race conditions (libflatcc.a errors) when
            # enabled. TODO(dbort): Remove this override once this option is
            # managed by cmake itself.
            "-DEXECUTORCH_SEPARATE_FLATCC_HOST_PROJECT=OFF",
        ]

        build_args = [f"-j{self.parallel}"]

        if ShouldBuild.pybindings():
            cmake_args += [
                "-DEXECUTORCH_BUILD_PYBIND=ON",
                "-DEXECUTORCH_BUILD_KERNELS_QUANTIZED=ON",  # add quantized ops to pybindings.
                "-DEXECUTORCH_BUILD_KERNELS_QUANTIZED_AOT=ON",
            ]
            if ShouldBuild.training():
                cmake_args += [
                    "-DEXECUTORCH_BUILD_EXTENSION_TRAINING=ON",
                ]
                build_args += ["--target", "_training_lib"]
            build_args += ["--target", "portable_lib"]
            # To link backends into the portable_lib target, callers should
            # add entries like `-DEXECUTORCH_BUILD_XNNPACK=ON` to the CMAKE_ARGS
            # environment variable.

        if ShouldBuild.llama_custom_ops():
            cmake_args += [
                "-DEXECUTORCH_BUILD_KERNELS_CUSTOM=ON",  # add llama sdpa ops to pybindings.
                "-DEXECUTORCH_BUILD_KERNELS_CUSTOM_AOT=ON",
                "-DEXECUTORCH_BUILD_KERNELS_QUANTIZED=ON",  # add quantized ops to pybindings.
                "-DEXECUTORCH_BUILD_KERNELS_QUANTIZED_AOT=ON",
            ]
            build_args += ["--target", "custom_ops_aot_lib"]
            build_args += ["--target", "quantized_ops_aot_lib"]
        # Allow adding extra cmake args through the environment. Used by some
        # tests and demos to expand the set of targets included in the pip
        # package.
        if "CMAKE_ARGS" in os.environ:
            cmake_args += [item for item in os.environ["CMAKE_ARGS"].split(" ") if item]

        # Allow adding extra build args through the environment. Used by some
        # tests and demos to expand the set of targets included in the pip
        # package.
        if "CMAKE_BUILD_ARGS" in os.environ:
            build_args += [
                item for item in os.environ["CMAKE_BUILD_ARGS"].split(" ") if item
            ]

        # CMAKE_BUILD_TYPE variable specifies the build type (configuration) for
        # single-configuration generators (e.g., Makefile Generators or Ninja).
        # For multi-config generators (like Visual Studio), CMAKE_BUILD_TYPE
        # isn’t directly applicable.
        # During the build step, --config specifies the configuration to build
        # for multi-config generators.
        build_args += ["--config", cfg]

        # Put the cmake cache under the temp directory, like
        # "pip-out/temp.<plat>/cmake-out".
        cmake_cache_dir = os.path.join(repo_root, self.build_temp, "cmake-out")
        self.mkpath(cmake_cache_dir)

        # Generate the cmake cache from scratch to ensure that the cache state
        # is predictable.
        cmake_cache_file = Path(cmake_cache_dir) / "CMakeCache.txt"
        log.info(f"deleting {cmake_cache_file}")
        if not self.dry_run:
            # Dry run should log the command but not actually run it.
            (Path(cmake_cache_dir) / "CMakeCache.txt").unlink(missing_ok=True)
        # Set PYTHONPATH to the location of the pip package.
        os.environ["PYTHONPATH"] = (
            site.getsitepackages()[0] + ";" + os.environ.get("PYTHONPATH", "")
        )
        with Buck2EnvironmentFixer():
            # The context manager may patch the environment while running this
            # cmake command, which happens to run buck2 to get some source
            # lists.

            # Generate the build system files.
            try:
                subprocess.run(
                    ["cmake", "-S", repo_root, "-B", cmake_cache_dir, *cmake_args],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                    text=True,
                )
            except subprocess.CalledProcessError as e:
                error = str(e.stderr)
                # Our educated guesses from parsing the error message.
                # Missing source file, could be related to git submodules not synced or cmake cache is outdated
                additional_log = ""
                if "Cannot find source file" in error:
                    additional_log = (
                        "\033[31;1mEither CMake cache is outdated or git submodules are not synced.\n"
                        "Please run the following before retry:\033[0m\n"
                        "    \033[32;1m./install_executorch.sh --clean\033[0m\n"
                        "    \033[32;1mgit submodule sync\033[0m\n"
                        "    \033[32;1mgit submodule update --init\033[0m\n"
                    )
                raise Exception(error + "\n" + additional_log) from e

        # Build the system.
        self.spawn(["cmake", "--build", cmake_cache_dir, *build_args])

        # Share the cmake-out location with _BaseExtension.
        self.cmake_cache_dir = cmake_cache_dir

        # Finally, run the underlying subcommands like build_py, build_ext.
        build.run(self)


def get_ext_modules() -> List[Extension]:
    """Returns the set of extension modules to build."""
    ext_modules = []
    if ShouldBuild.flatc():
        ext_modules.extend(
            [
                BuiltFile(
                    src_dir="%CMAKE_CACHE_DIR%/third-party/flatbuffers/%BUILD_TYPE%/",
                    src_name="flatc",
                    dst="executorch/data/bin/",
                    is_executable=True,
                ),
                BuiltFile(
                    src_dir="build/",
                    src_name="pip_data_bin_init.py.in",
                    dst="executorch/data/bin/__init__.py",
                ),
            ]
        )

    if ShouldBuild.pybindings():
        ext_modules.append(
            # Install the prebuilt pybindings extension wrapper for the runtime,
            # portable kernels, and a selection of backends. This lets users
            # load and execute .pte files from python.
            BuiltExtension(
                "_portable_lib.*", "executorch.extension.pybindings._portable_lib"
            )
        )
        if ShouldBuild.training():
            ext_modules.append(
                # Install the prebuilt pybindings extension wrapper for training
                BuiltExtension(
                    "_training_lib.*",
                    "executorch.extension.training.pybindings._training_lib",
                )
            )
    if ShouldBuild.llama_custom_ops():
        ext_modules.append(
            BuiltFile(
                src_dir="%CMAKE_CACHE_DIR%/extension/llm/custom_ops/%BUILD_TYPE%/",
                src_name="custom_ops_aot_lib",
                dst="executorch/extension/llm/custom_ops/",
                is_dynamic_lib=True,
            )
        )
        ext_modules.append(
            # Install the prebuilt library for quantized ops required by custom ops.
            BuiltFile(
                src_dir="%CMAKE_CACHE_DIR%/kernels/quantized/%BUILD_TYPE%/",
                src_name="quantized_ops_aot_lib",
                dst="executorch/kernels/quantized/",
                is_dynamic_lib=True,
            )
        )

    # Note that setuptools uses the presence of ext_modules as the main signal
    # that a wheel is platform-specific. If we install any platform-specific
    # files, this list must be non-empty. Therefore, we should always install
    # platform-specific files using InstallerBuildExt.
    return ext_modules


setup(
    version=Version.string(),
    cmdclass={
        "build": CustomBuild,
        "build_ext": InstallerBuildExt,
        "build_py": CustomBuildPy,
    },
    ext_modules=get_ext_modules(),
)
