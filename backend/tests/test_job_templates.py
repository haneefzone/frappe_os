"""Golden rule 1: the command template registry must make shell injection
impossible and mask secrets everywhere they could surface."""

import pytest

from app.core.commands import MASK, RenderError, get_template, render
from app.core.commands.templates import CommandTemplate, ParamSpec

ECHO = get_template("system.echo_demo")

# Constructed here (not registered) so we can exercise secret/enum handling
# without polluting the production registry.
SECRET_TMPL = CommandTemplate(
    action_name="test.secret",
    argv=("mysql", "--password={password}", "status"),
    cwd=None,
    params=(ParamSpec("password", regex=r".{1,64}", secret=True),),
    action_class=object,
    idempotent=True,
    requires_lock=False,
    required_permission="server:manage",
)

ENUM_TMPL = CommandTemplate(
    action_name="test.enum",
    argv=("systemctl", "{action}", "nginx"),
    cwd=None,
    params=(ParamSpec("action", enum=("start", "stop", "restart")),),
    action_class=object,
    idempotent=True,
    requires_lock=False,
    required_permission="server:manage",
)


@pytest.mark.parametrize(
    "payload",
    [
        "; rm -rf /",
        "`whoami`",
        "$(whoami)",
        "line1\nline2",
        "a && reboot",
        "a | tee x",
        "a > /etc/passwd",
        'say "hi"',
        "cost $5",
        "back\\slash",
    ],
)
def test_render_rejects_shell_metacharacters(payload):
    with pytest.raises(RenderError):
        render(ECHO, {"message": payload})


def test_render_accepts_safe_message_as_single_argv_element():
    rendered = render(ECHO, {"message": "Deploy 1.2-3 to prod:web/01"})
    # The message is one argv element — never spliced into a shell string.
    assert rendered.argv == ["echo", "Deploy 1.2-3 to prod:web/01"]
    assert rendered.cwd is None


def test_unknown_parameter_is_rejected():
    with pytest.raises(RenderError):
        render(ECHO, {"message": "ok", "surprise": "x"})


def test_missing_required_parameter_is_rejected():
    with pytest.raises(RenderError):
        render(ECHO, {})


def test_secret_is_masked_in_display_and_sanitized_params_but_real_in_argv():
    rendered = render(SECRET_TMPL, {"password": "hunter2"})
    # The real argv keeps the plaintext (in memory only, to actually run).
    assert rendered.argv == ["mysql", "--password=hunter2", "status"]
    # Everything persisted or displayed is masked (rule 6).
    assert "hunter2" not in rendered.display
    assert MASK in rendered.display
    assert rendered.params_sanitized == {"password": MASK}
    # The redactor gets the plaintext so it can scrub it from logs.
    assert rendered.secret_values == ("hunter2",)


def test_render_from_sanitized_refuses_secret_bearing_template():
    # DOO-94 #1: re-rendering from a masked params_sanitized map (worker /
    # retry) must never run a secret param as `••••`. Fail loud instead.
    from app.core.commands import SecretParamUnresolved

    with pytest.raises(SecretParamUnresolved) as exc:
        render(SECRET_TMPL, {"password": MASK}, from_sanitized=True)
    # Names the offending param, and never leaks a value.
    assert "password" in str(exc.value)
    assert MASK not in str(exc.value)
    # SecretParamUnresolved is a RenderError, so routes map it to 422.
    assert isinstance(exc.value, RenderError)


def test_render_from_sanitized_is_a_noop_for_secretless_templates():
    # The guard only trips on secret params; the real re-render paths (echo /
    # detect_tools, no secrets) keep working unchanged.
    assert render(ECHO, {"message": "hi"}, from_sanitized=True).argv == ["echo", "hi"]
    assert render(get_template("server.detect_tools"), {}, from_sanitized=True).argv == [
        "true"
    ]


def test_render_error_never_echoes_the_offending_value():
    try:
        render(ECHO, {"message": "; rm -rf /"})
    except RenderError as exc:
        assert "rm -rf" not in str(exc)
    else:  # pragma: no cover
        pytest.fail("expected RenderError")


def test_enum_rejects_out_of_set_and_accepts_member():
    with pytest.raises(RenderError):
        render(ENUM_TMPL, {"action": "obliterate"})
    assert render(ENUM_TMPL, {"action": "restart"}).argv == ["systemctl", "restart", "nginx"]


def test_detect_tools_takes_no_params():
    rendered = render(get_template("server.detect_tools"), {})
    assert rendered.params_sanitized == {}
