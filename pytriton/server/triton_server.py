# Copyright (c) 2020-2023, NVIDIA CORPORATION. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Triton Inference Server class.

Use to start and maintain the Triton Inference Server process.

    Examples of use:

        server = TritonServer(
            path="/path/to/tritonserver/binary",
            libs_path="/path/to/tritonserver/libraries",
            config=TritonServerConfig()
        )
        server.start()

"""
import ctypes.util
import logging
import os
import pathlib
import pkgutil
import signal
import site
import sys
import threading
import traceback
from typing import Callable, Dict, Literal, Optional, Sequence, Union

from pytriton.constants import (
    DEFAULT_GRPC_PORT,
    DEFAULT_HTTP_PORT,
    DEFAULT_METRICS_PORT,
    PYTRITON_CACHE_DIR,
    TRITON_LOCAL_IP,
    TRITON_PYTHON_BACKEND_INTERPRETER_DIRNAME,
)
from pytriton.utils.logging import silence_3rd_party_loggers

from .triton_server_config import TritonServerConfig

LOGGER = logging.getLogger(__name__)
SERVER_OUTPUT_TIMEOUT_SECS = 30
_PROXY_REQUIRED_MODULES = ["numpy", "zmq"]

silence_3rd_party_loggers()


def get_triton_python_backend_python_env() -> pathlib.Path:
    """Get the path to the python environment for the triton python backend.

    Officially built python backend is built with python 3.8 so need to
    use the same python version to run the python backend.

    Also, python environment should contain packages required by the proxy.

    Returns:
        Path to the python environment with python 3.8
    """
    pytriton_started_in_python38 = (3, 8) <= sys.version_info < (3, 9)
    env_path = pathlib.Path(sys.exec_prefix)
    env_site_dirs = site.getsitepackages()
    if not pytriton_started_in_python38:
        venv_path = PYTRITON_CACHE_DIR / TRITON_PYTHON_BACKEND_INTERPRETER_DIRNAME
        if not venv_path.exists():
            raise RuntimeError(
                f"venv for python backend not found at {venv_path}. "
                f"Please run pytriton in python 3.8 environment to create the venv. "
                f"Refer to https://github.com/triton-inference-server/pytriton/blob/main/docs/installation.md for more details."
            )
        env_path = venv_path
        env_site_dirs = [(env_path / "lib" / "python3.8" / "site-packages").as_posix()]

    installed_modules = [module_info.name for module_info in pkgutil.iter_modules(env_site_dirs)]
    missing_modules = list(set(_PROXY_REQUIRED_MODULES) - set(installed_modules))
    if missing_modules:
        raise RuntimeError(
            "Python environment for python backend is missing required packages. "
            f"Ensure that you have {', '.join(_PROXY_REQUIRED_MODULES)} installed in the {env_path} environment. "
            f"Installed modules {', '.join(installed_modules)}. Missing modules {', '.join(missing_modules)}."
        )

    return env_path


class TritonServer:
    """Implementation of TritonServer interface that runs tritonserver locally as subprocess."""

    def __init__(
        self,
        *,
        path: Union[str, pathlib.Path],
        libs_path: Union[str, pathlib.Path],
        config: TritonServerConfig,
        gpus: Optional[Sequence[int]] = None,
        verbose: bool = True,
    ):
        """Triton server constructor.

        Args:
            path: The absolute path to the tritonserver executable
            libs_path: The absolute path to the tritonserver libraries
            config: The config object containing arguments for this server instance
            gpus: sequence of GPUs device ids to attach to process
            verbose: Enable verbose logging of server to STDOUT
        """
        self._server_path = pathlib.Path(path)
        self._server_libs_path = pathlib.Path(libs_path)
        self._server_config = config
        self._gpus = gpus
        self._tritonserver_running_cmd = None
        self._tritonserver_logs = ""
        self._verbose = verbose
        self._on_exit_lock = threading.RLock()
        self._on_exit = []

        assert self._server_config["model-repository"], "Triton Server requires --model-repository argument to be set."

    def start(self) -> None:
        """Starts the tritonserver process.

        The method can be executed multiple times and only single process is started.
        """
        if self.is_alive():
            raise RuntimeError(
                f"You have to stop previously started tritonserver process first "
                f"pid={self._tritonserver_running_cmd.pid}"
            )
        else:
            env = self._get_env()

            tritonserver_cmd, *rest = self._server_path.as_posix().split(" ", 1)

            import sh

            tritonserver_cmd = sh.Command(tritonserver_cmd)
            tritonserver_cmd = tritonserver_cmd.bake(*rest)

            tritonserver_args = self._server_config.to_args_list()

            def _preexec_fn():
                PR_SET_PDEATHSIG = 1  # noqa
                libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
                libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)

            self._tritonserver_logs = ""
            self._tritonserver_running_cmd = tritonserver_cmd(
                *tritonserver_args,
                _env=env,
                _err_to_out=True,
                _out=self._record_logs,
                _out_bufsize=0,
                _err_bufsize=0,
                _bg=True,
                _bg_exc=False,
                _done=self._handle_exit,
                _preexec_fn=_preexec_fn,
            )

    def stop(self) -> None:
        """Send the SIGINT signal to running process and wait until server finished."""
        if self.is_alive():
            LOGGER.debug(
                f"Stopping Triton Inference server - sending SIGINT signal and wait {SERVER_OUTPUT_TIMEOUT_SECS}s"
            )
            self._tritonserver_running_cmd.process.signal(signal.SIGINT)
            try:
                LOGGER.debug("Waiting for process to stop")
                self._tritonserver_running_cmd.wait(timeout=SERVER_OUTPUT_TIMEOUT_SECS)
            except Exception:
                message = traceback.format_exc()
                LOGGER.debug(f"Error message: \n{message}")
                try:
                    if self.is_alive():
                        LOGGER.debug("Timeout waiting for server. Trying to kill process.")
                        self._tritonserver_running_cmd.process.kill()
                        self._tritonserver_running_cmd.wait(timeout=SERVER_OUTPUT_TIMEOUT_SECS)
                except Exception:
                    LOGGER.debug(f"Could not kill triton server pid={self._tritonserver_running_cmd.pid}")
                    message = traceback.format_exc()
                    LOGGER.debug(f"Error message: \n{message}")

    def register_on_exit(self, callback: Callable) -> None:
        """Register callback executed on process exit.

        Args:
            callback: callable to register in callbacks
        """
        with self._on_exit_lock:
            self._on_exit.append(callback)

    def unregister_on_exit(self, callback: Callable) -> None:
        """Unregister callback executed on process exit.

        Args:
            callback: callable to unregister from callbacks
        """
        with self._on_exit_lock:
            self._on_exit.remove(callback)

    def is_alive(self) -> bool:
        """Verify if server is currently running.

        Returns:
            True when server is running, False otherwise
        """
        return self._tritonserver_running_cmd is not None and self._tritonserver_running_cmd.is_alive()

    def logs(self) -> str:
        """Return the server logs of running server.

        Returns:
            String with capture logs
        """
        return self._tritonserver_logs

    def get_endpoint(self, endpoint: Literal["http", "grpc", "metrics"]) -> str:
        """Get endpoint url.

        Args:
            endpoint: endpoint name

        Returns:
            endpoint url in form of {protocol}://{host}:{port}
        """
        protocols = {"http": "http", "grpc": "grpc", "metrics": "http"}
        addresses = {
            "http": self._server_config["http-address"] or TRITON_LOCAL_IP,
            "grpc": self._server_config["grpc-address"] or TRITON_LOCAL_IP,
            "metrics": self._server_config["metrics-address"] or self._server_config["http-address"] or TRITON_LOCAL_IP,
        }
        ports = {
            "http": self._server_config["http-port"] or DEFAULT_HTTP_PORT,
            "grpc": self._server_config["grpc-port"] or DEFAULT_GRPC_PORT,
            "metrics": self._server_config["metrics-port"] or DEFAULT_METRICS_PORT,
        }

        return f"{protocols[endpoint]}://{addresses[endpoint]}:{ports[endpoint]}"

    def _record_logs(self, line: Union[bytes, str]) -> None:
        """Record logs obtained from server process. If verbose logging enabled, print the log into STDOUT.

        Args:
            line: Log line obtained from server
        """
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")

        if self._verbose:
            print(line, end="")  # noqa: T201

        self._tritonserver_logs += line

    def _get_env(self) -> Dict:
        """Create and return environment variables for server execution.

        Returns:
            Dict with environment variables
        """
        env = os.environ.copy()
        if self._gpus and isinstance(self._gpus, (list, tuple)):
            env["CUDA_VISIBLE_DEVICES"] = ",".join([str(gpu) for gpu in self._gpus])

        if "LD_LIBRARY_PATH" in env:
            env["LD_LIBRARY_PATH"] += ":" + self._server_libs_path.as_posix()
        else:
            env["LD_LIBRARY_PATH"] = self._server_libs_path.as_posix()
        env.pop("PYTHONPATH", None)
        python_bin_directory = get_triton_python_backend_python_env() / "bin"
        env["PATH"] = f"{python_bin_directory.as_posix()}:{env['PATH']}"

        return env

    def _handle_exit(self, _, success, exit_code) -> None:
        """Handle exit of server process. Trigger callbacks if provided.

        Args:
            success: Flag indicating if process succeeded or failed
            exit_code: Exit code with which server process finished
        """
        if not success:
            LOGGER.warning("Triton Inference Server exited with failure. Please wait.")
            LOGGER.debug(f"Triton Inference Server exit code {exit_code}")
        else:
            LOGGER.debug("Triton Inference Server stopped")
        with self._on_exit_lock:
            for callback in self._on_exit:
                try:
                    callback(success, exit_code)
                except Exception as e:
                    LOGGER.debug(f"Error during calling on_exit callback; {e}")
