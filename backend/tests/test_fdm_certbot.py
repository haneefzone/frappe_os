"""Unit tests for the certbot privilege wrapper ``deploy/fdm-certbot`` (DOO-220).

certbot needs root (it writes ``/etc/letsencrypt``), but a bare sudoers grant of
``/usr/bin/certbot certonly *`` / ``renew *`` is a root-escalation vector: the
``*`` matches arbitrary args and both subcommands accept ``--deploy-hook`` /
``--pre-hook`` / ``--post-hook``, which run an arbitrary shell command AS ROOT. So
the platform routes certbot through this fixed, root-owned wrapper — the NOPASSWD
line targets only the wrapper. These tests source the wrapper (its tail is guarded
by ``BASH_SOURCE == $0`` so sourcing does not run ``main``) and exercise the input
validators and the ``cmd_*`` refusal paths directly, proving that no flag-shaped
argument — above all ``--deploy-hook`` — can ever reach certbot. They must NOT hit
the ``exec certbot`` tail (there is no certbot on the test host and it would touch
the network), so every case here is one the wrapper *rejects* before exec, or a
pure validator call.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HELPER = (Path(__file__).resolve().parents[2] / "deploy" / "fdm-certbot").resolve()

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash required to test the certbot wrapper"
)


def _run(snippet: str) -> subprocess.CompletedProcess[str]:
    """Source the wrapper and run a bash snippet against its functions."""
    script = f'set -euo pipefail\nsource "{HELPER}"\n{snippet}\n'
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )


def test_helper_parses_and_sourcing_does_not_run_main():
    # `bash -n` parse gate, and sourcing must not execute a subcommand.
    assert subprocess.run(["bash", "-n", str(HELPER)]).returncode == 0
    r = _run("echo SOURCED_OK")
    assert r.returncode == 0, r.stderr
    assert "SOURCED_OK" in r.stdout
    assert "unknown subcommand" not in (r.stdout + r.stderr)


# --------------------------------------------------------------------------- #
# The core of DOO-220: an injected hook flag must be refused before exec.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "hook",
    ["--deploy-hook", "--pre-hook", "--post-hook", "--manual-auth-hook"],
)
def test_reject_flaglike_refuses_hook_flags(hook):
    r = _run(f"reject_flaglike '{hook}'")
    assert r.returncode != 0
    assert "flag-shaped" in r.stderr


def test_issue_refuses_deploy_hook_as_extra_arg():
    # The classic RCE shape: extra `--deploy-hook 'cmd'` after the positionals.
    # arg count != 3 → die before certbot is ever invoked.
    r = _run(
        "cmd_issue x.example.com admin@example.com /var/www/acme "
        "--deploy-hook 'id > /root/pwned'"
    )
    assert r.returncode != 0
    assert "issue needs exactly" in r.stderr
    assert "pwned" not in (r.stdout + r.stderr)


def test_issue_refuses_deploy_hook_smuggled_as_webroot():
    # Same attack squeezed into the 3rd positional so the arg count matches:
    # reject_flaglike still catches the leading dash.
    r = _run("cmd_issue x.example.com admin@example.com --deploy-hook")
    assert r.returncode != 0
    assert "flag-shaped" in r.stderr


def test_renew_refuses_flag_argument():
    r = _run("cmd_renew --deploy-hook")
    assert r.returncode != 0
    assert "flag-shaped" in r.stderr


def test_certificates_refuses_any_argument():
    # Read-only subcommand: no way to smuggle a hook in.
    r = _run("cmd_certificates --deploy-hook 'id'")
    assert r.returncode != 0
    assert "takes no arguments" in r.stderr


def test_unknown_subcommand_is_refused():
    r = _run("main install --deploy-hook")
    assert r.returncode != 0
    assert "unknown subcommand" in r.stderr


# --------------------------------------------------------------------------- #
# Positional validators — shape enforcement (belt over the reject_flaglike
# suspenders). These prove a caller cannot smuggle metacharacters either.
# --------------------------------------------------------------------------- #

def test_valid_domain_accepts_hostname():
    assert _run("valid_domain erp.acme.com").returncode == 0


@pytest.mark.parametrize(
    "bad",
    [
        "-d",                       # leading dash (flag-shaped)
        "erp.acme.com;id",          # shell metacharacter
        "erp acme.com",             # space
        "erp",                      # single label (no dot)
        "ERP.ACME.COM",             # uppercase (platform lowercases)
        "a.$(id).com",              # command substitution
    ],
)
def test_valid_domain_refuses_bad(bad):
    assert _run(f"valid_domain '{bad}'").returncode != 0


def test_valid_email_accepts_and_refuses():
    assert _run("valid_email admin@acme.com").returncode == 0
    assert _run("valid_email 'a b@acme.com'").returncode != 0
    assert _run("valid_email 'a@acme.com;id'").returncode != 0


@pytest.mark.parametrize(
    "bad",
    [
        "relative/path",            # not absolute
        "/var/www/../../etc",       # `..` traversal
        "/var/www;rm -rf /",        # shell metacharacter
        "-w",                       # flag-shaped
    ],
)
def test_valid_webroot_refuses_bad(bad):
    assert _run(f"valid_webroot '{bad}'").returncode != 0


def test_valid_webroot_accepts_absolute_challenge_dir():
    assert _run("valid_webroot /home/frappe/frappe-bench/sites/erp/public").returncode == 0


def test_issue_requires_exactly_three_positionals():
    assert _run("cmd_issue only-one.example.com").returncode != 0
    assert _run("cmd_renew").returncode != 0  # renew needs a domain
