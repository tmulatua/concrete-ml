"""PyTest configuration file"""
import json
import random
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import numpy
import pytest
import torch
from concrete.common.compilation import CompilationConfiguration
from concrete.common.fhe_circuit import FHECircuit
from concrete.common.mlir.utils import (
    ACCEPTABLE_MAXIMAL_BITWIDTH_FROM_CONCRETE_LIB,
    get_op_graph_max_bit_width_and_nodes_over_bit_width_limit,
)
from concrete.numpy import compile as compile_


def pytest_addoption(parser):
    """Options for pytest"""

    parser.addoption(
        "--global-coverage-infos-json",
        action="store",
        default=None,
        type=str,
        help="To dump pytest-cov term report to a text file.",
    )

    parser.addoption(
        "--keyring-dir",
        action="store",
        default=None,
        type=str,
        help="Specify the dir to use to store key cache",
    )


DEFAULT_KEYRING_PATH = Path.home().resolve() / ".cache/concrete-ml_pytest"


def get_keyring_dir_from_session_or_default(
    session: Optional[pytest.Session] = None,
) -> Optional[Path]:
    """Get keyring dir from test session."""
    if session is None:
        return DEFAULT_KEYRING_PATH

    keyring_dir = session.config.getoption("--keyring-dir", default=None)
    if keyring_dir is not None:
        if keyring_dir.lower() == "disable":
            return None
        keyring_dir = Path(keyring_dir).expanduser().resolve()
    else:
        keyring_dir = DEFAULT_KEYRING_PATH
    return keyring_dir


@pytest.fixture
def default_keyring_path():
    """Fixture to get test keyring dir."""
    return DEFAULT_KEYRING_PATH


# This is only for doctests where we currently cannot make use of fixtures
original_compilation_config_init = CompilationConfiguration.__init__


def monkeypatched_compilation_configuration_init_for_codeblocks(
    self: CompilationConfiguration, *args, **kwargs
):
    """Monkeypatched compilation configuration init for codeblocks tests."""
    original_compilation_config_init(self, *args, **kwargs)
    self.dump_artifacts_on_unexpected_failures = False
    self.enable_unsafe_features = True  # This is for our tests only, never use that in prod
    self.treat_warnings_as_errors = True
    self.use_insecure_key_cache = True  # This is for our tests only, never use that in prod


def pytest_sessionstart(session: pytest.Session):
    """Handle keyring for session and codeblocks CompilationConfiguration if needed."""
    if session.config.getoption("--codeblocks", default=False):
        # setattr to avoid mypy complaining
        # Disable the flake8 bug bear warning for the mypy fix
        setattr(  # noqa: B010
            CompilationConfiguration,
            "__init__",
            monkeypatched_compilation_configuration_init_for_codeblocks,
        )

    keyring_dir = get_keyring_dir_from_session_or_default(session)
    if keyring_dir is None:
        return
    keyring_dir.mkdir(parents=True, exist_ok=True)
    keyring_dir_as_str = str(keyring_dir)
    print(f"Using {keyring_dir_as_str} as key cache dir")
    compile_._COMPILE_FHE_INSECURE_KEY_CACHE_DIR = (  # pylint: disable=protected-access
        keyring_dir_as_str
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus):  # pylint: disable=unused-argument
    """Pytest callback when testing ends."""
    # Hacked together from the source code, they don't have an option to export to file and it's too
    # much work to get a PR in for such a little thing
    # https://github.com/pytest-dev/pytest-cov/blob/
    # ec344d8adf2d78238d8f07cb20ed2463d7536970/src/pytest_cov/plugin.py#L329
    if session.config.pluginmanager.hasplugin("_cov"):
        global_coverage_file = session.config.getoption(
            "--global-coverage-infos-json", default=None
        )
        if global_coverage_file is not None:
            cov_plugin = session.config.pluginmanager.getplugin("_cov")
            coverage_txt = cov_plugin.cov_report.getvalue()
            coverage_status = 0
            if (
                cov_plugin.options.cov_fail_under is not None
                and cov_plugin.options.cov_fail_under > 0
            ):
                failed = cov_plugin.cov_total < cov_plugin.options.cov_fail_under
                # If failed is False coverage_status is 0, if True it's 1
                coverage_status = int(failed)
            global_coverage_file_path = Path(global_coverage_file).resolve()
            with open(global_coverage_file_path, "w", encoding="utf-8") as f:
                json.dump({"exit_code": coverage_status, "content": coverage_txt}, f)

    keyring_dir = get_keyring_dir_from_session_or_default(session)
    if keyring_dir is not None:
        # Remove incomplete keys
        for incomplete_keys in keyring_dir.glob("**/*incomplete*"):
            shutil.rmtree(incomplete_keys, ignore_errors=True)


@pytest.fixture
def default_compilation_configuration():
    """Return the default test compilation configuration"""
    return CompilationConfiguration(
        dump_artifacts_on_unexpected_failures=False,
        enable_unsafe_features=True,  # This is for our tests only, never use that in prod
        treat_warnings_as_errors=True,
        use_insecure_key_cache=True,  # This is for our tests only, never use that in prod
    )


REMOVE_COLOR_CODES_RE = re.compile(r"\x1b[^m]*m")


@pytest.fixture
def remove_color_codes():
    """Return the re object to remove color codes"""
    return lambda x: REMOVE_COLOR_CODES_RE.sub("", x)


def function_to_seed_torch():
    """Function to seed torch"""

    # Seed torch with something which is seed by pytest-randomly
    torch.manual_seed(random.randint(0, 2 ** 64 - 1))
    torch.use_deterministic_algorithms(True)


@pytest.fixture
def seed_torch():
    """Fixture to seed torch"""

    return function_to_seed_torch


def check_is_good_execution_impl(
    fhe_circuit: FHECircuit,
    function: Callable,
    args: Iterable[Any],
    preprocess_input_func: Callable[[Any], Any] = lambda x: x,
    postprocess_output_func: Callable[[Any], Any] = lambda x: x,
    check_function: Callable[[Any, Any], bool] = numpy.equal,
    verbose: bool = True,
):
    """Run several times the check compiler_engine.run(*args) == function(*args). If always wrong,
    return an error. One can set the expected probability of success of one execution and the
    number of tests, to finetune the probability of bad luck, ie that we run several times the
    check and always have a wrong result."""
    max_bit_width, _ = get_op_graph_max_bit_width_and_nodes_over_bit_width_limit(
        fhe_circuit.op_graph
    )

    # Allow tests to pass if cells of the output result are good at least once over the nb_tries
    # Enabled only when we have a circuit that's using the maximum possible bit width
    # >= if there are 8 bits signed integers
    allow_relaxed_tests_passing = max_bit_width >= ACCEPTABLE_MAXIMAL_BITWIDTH_FROM_CONCRETE_LIB

    # FIXME: https://github.com/zama-ai/concrete-numpy-internal/issues/1255
    # Increased with compiler accuracy which dropped, make sure to remove once accuracy improves
    nb_tries = 10

    # Prepare the bool array to record if cells were properly computed
    preprocessed_args = tuple(preprocess_input_func(val) for val in args)
    cells_were_properly_computed = numpy.zeros_like(function(*preprocessed_args), dtype=bool)

    for i in range(1, nb_tries + 1):
        preprocessed_args = tuple(preprocess_input_func(val) for val in args)
        last_engine_result = postprocess_output_func(fhe_circuit.run(*preprocessed_args))
        last_function_result = postprocess_output_func(function(*preprocessed_args))

        ok_execution = check_function(last_engine_result, last_function_result)
        if isinstance(ok_execution, numpy.ndarray):
            # Record the cells that were well computed
            cells_were_properly_computed = numpy.logical_or(
                cells_were_properly_computed, ok_execution
            )

            # Get a boolean for the execution
            ok_execution = ok_execution.all()

        if ok_execution:
            # Good computation after i tries
            if verbose:
                print(f"Good computation after {i} tries")
            return
        # FIXME: https://github.com/zama-ai/concrete-numpy-internal/issues/1264
        # Remove the relaxed tests once accuracy is good again for 7 bits
        if allow_relaxed_tests_passing and cells_were_properly_computed.all():
            print(
                "Computation was never good for all output cells at the same time, "
                f"however each was evaluated properly at least once, stopped after {i} tries"
            )
            return

    raise AssertionError(
        f"bad computation after {nb_tries} tries.\nLast engine result:\n{last_engine_result}\n"
        f"Last function result:\n{last_function_result}"
    )


@pytest.fixture
def check_is_good_execution():
    """Fixture to seed torch"""

    return check_is_good_execution_impl


def check_array_equality_impl(actual: Any, expected: Any, verbose: bool = True):
    """Assert that `actual` is equal to `expected`."""

    assert numpy.array_equal(actual, expected), (
        ""
        if not verbose
        else f"""

Expected Output
===============
{expected}

Actual Output
=============
{actual}

        """
    )


@pytest.fixture
def check_array_equality():
    """Fixture to check array equality"""

    return check_array_equality_impl
