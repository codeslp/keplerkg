import pytest

from codegraphcontext_ext.daemon.serve import serve


def test_daemon_serve_is_a_phase0_placeholder():
    with pytest.raises(NotImplementedError):
        serve()
