"""Shared pytest fixtures for the AgentCrash test suite."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

# Make the package and the examples/ demo agent importable.
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
_EXAMPLES = os.path.join(_REPO, "examples")
if _EXAMPLES not in sys.path:
    sys.path.insert(0, _EXAMPLES)


@pytest.fixture
def tmp_storage():
    """A fresh Storage in a temp dir. Closed on teardown to release the DB."""
    from agentcrash.storage import Storage

    d = tempfile.mkdtemp(prefix="agentcrash_test_")
    st = Storage(os.path.join(d, "ac.db"))
    yield st
    st.close()


@pytest.fixture
def tracer(tmp_storage):
    from agentcrash.sdk import CrashTracer

    return CrashTracer(tmp_storage, integration="test", framework="agentcrash-tests")


@pytest.fixture
def demo():
    """The demo agent module (buggy_agent, fixed_agent, DEMO_REQUEST, ...)."""
    import demo_agent

    return demo_agent