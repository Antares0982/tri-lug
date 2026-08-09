"""Matrix adapter: which outbound failures are the homeserver being flaky
(one WARNING line) versus a real bug (let the Router's traceback through)."""

import aiohttp
from mautrix.errors import MatrixUnknownRequestError, MForbidden

from modules.tri_lug_utils.matrix_adapter import _is_transient


def test_is_transient():
    # The Cloudflare 522 in front of the homeserver, HTML body and all.
    assert _is_transient(MatrixUnknownRequestError(522, "<!DOCTYPE html>..."))
    assert _is_transient(MatrixUnknownRequestError(502, "bad gateway"))
    assert _is_transient(aiohttp.ClientConnectionError("nope"))
    assert _is_transient(TimeoutError())

    # Bridge-side mistakes must keep their traceback.
    assert not _is_transient(MForbidden(403, "not in room"))
    assert not _is_transient(MatrixUnknownRequestError(400, "bad json"))
    assert not _is_transient(ValueError("bug"))
