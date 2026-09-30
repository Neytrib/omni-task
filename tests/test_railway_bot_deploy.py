"""Deployment safety tests stub subprocesses: never contact or mutate Railway."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from scripts import deploy_railway_bot as deploy

PROJECT, ENVIRONMENT, SERVICE, OLD, NEW, OTHER = [str(UUID(int=n)) for n in range(1, 7)]
SHA = "a" * 40


@pytest.fixture
def configuration():
    return {
        "RAILWAY_PROJECT_ID": PROJECT,
        "RAILWAY_ENVIRONMENT_ID": ENVIRONMENT,
        "RAILWAY_BOT_SERVICE_ID": SERVICE,
        "RAILWAY_TOKEN": "synthetic-only-never-a-real-token",
        "RAILWAY_TOKEN_AVAILABLE": "true",
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "Neytrib/omni-task",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_SHA": SHA,
    }


@pytest.fixture
def cli(monkeypatch):
    calls = []
    responses = []
    clock = [0]

    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["capture_output"] is True
        assert kwargs["check"] is False
        assert 0 < kwargs["timeout"] <= 180
        expected, response = responses.pop(0)
        assert args[1 : 1 + len(expected)] == expected
        if args[0] == "railway":
            assert args[-6:] == [
                "--project",
                PROJECT,
                "--environment",
                ENVIRONMENT,
                "--service",
                SERVICE,
            ]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, tuple):
            code, output = response
        else:
            code, output = 0, response
        return SimpleNamespace(
            returncode=code, stdout=output, stderr="never print this private data"
        )

    def sleep(seconds):
        assert seconds in {1, 3, 5}
        clock[0] += seconds

    monkeypatch.setattr(deploy.subprocess, "run", run)
    monkeypatch.setattr(deploy.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(deploy.time, "sleep", sleep)

    def listing(**states):
        responses.append(
            (
                ["deployment", "list"],
                json.dumps([{"id": key, "status": state} for key, state in states.items()]),
            )
        )

    return SimpleNamespace(calls=calls, responses=responses, listing=listing)


def test_stop_and_confirm_removed_before_upload_with_failed_latest_history(cli, configuration):
    cli.listing(**{OTHER: "FAILED", OLD: "SUCCESS"})
    cli.responses.append((["down", "--yes"], "suppressed stop output"))
    cli.listing(**{OTHER: "FAILED", OLD: "REMOVING"})
    cli.listing(**{OTHER: "FAILED", OLD: "REMOVED"})
    cli.responses.append((["up", "--ci", "--detach", "--json"], json.dumps({"deploymentId": NEW})))
    cli.listing(**{NEW: "BUILDING", OTHER: "FAILED", OLD: "REMOVED"})
    cli.listing(**{NEW: "SUCCESS", OTHER: "FAILED", OLD: "REMOVED"})
    assert deploy.BotDeployer(configuration).deploy(SHA) == NEW
    assert not cli.responses
    assert cli.calls[4][1] == "up"
    assert configuration["RAILWAY_TOKEN"] not in repr(cli.calls)
    assert cli.calls[4][cli.calls[4].index("--message") + 1] == f"main {SHA}"


def test_initial_deployment_rechecks_empty_service_without_down(cli, configuration):
    cli.listing()
    cli.listing()
    cli.responses.append((["up", "--ci", "--detach", "--json"], json.dumps({"deploymentId": NEW})))
    cli.listing(**{NEW: "SUCCESS"})
    assert deploy.BotDeployer(configuration).deploy(SHA) == NEW
    assert not any("down" in call for call in cli.calls)


def test_read_failure_after_upload_retries_only_status_and_preserves_id(cli, configuration, capsys):
    cli.listing()
    cli.listing()
    cli.responses.append((["up"], json.dumps({"deploymentId": NEW})))
    cli.responses.append((["deployment", "list"], (1, "synthetic private CLI details")))
    cli.listing(**{NEW: "SUCCESS"})
    assert deploy.BotDeployer(configuration).deploy(SHA) == NEW
    assert sum(call[1] == "up" for call in cli.calls) == 1
    output = capsys.readouterr().out
    assert f"Uploaded bot deployment {NEW}" in output
    assert "Retrying Railway deployment status read (2/3)" in output
    assert "private CLI" not in output


def test_status_failure_retries_are_bounded_with_safe_phase_message(cli, configuration, capsys):
    for _ in range(3):
        cli.responses.append((["deployment", "list"], (1, "synthetic credential-bearing output")))
    with pytest.raises(deploy.CommandError, match="deployment status read failed \\(exit 1\\)"):
        deploy.BotDeployer(configuration).deploy(SHA)
    assert len(cli.calls) == 3
    assert "credential-bearing" not in capsys.readouterr().out


@pytest.mark.parametrize("operation", ["down", "up"])
def test_mutation_failure_is_not_retried_and_identifies_phase(cli, configuration, operation):
    if operation == "down":
        cli.listing(**{OLD: "SUCCESS"})
        cli.responses.append((["down", "--yes"], (1, "synthetic secret response")))
        phase = "stop"
    else:
        cli.listing()
        cli.listing()
        cli.responses.append((["up", "--ci"], (1, "synthetic secret response")))
        phase = "upload"
    with pytest.raises(deploy.CommandError, match=f"Railway {phase} request failed"):
        deploy.BotDeployer(configuration).deploy(SHA)
    assert sum(call[1] == operation for call in cli.calls) == 1


@pytest.mark.parametrize(
    "conflict",
    [
        "SUCCESS",
        "QUEUED",
        "BUILDING",
        "DEPLOYING",
        "INITIALIZING",
        "WAITING",
        "REMOVING",
        "NEEDS_APPROVAL",
        "SLEEPING",
        "CRASHED",
        "FUTURE_UNKNOWN",
    ],
)
def test_conflicting_deployments_fail_before_any_mutation(cli, configuration, conflict):
    cli.listing(**{OLD: "SUCCESS", OTHER: conflict})
    with pytest.raises(deploy.DeploymentError):
        deploy.BotDeployer(configuration).deploy(SHA)
    assert len(cli.calls) == 1


def test_removal_timeout_never_uploads(cli, configuration):
    cli.listing(**{OLD: "SUCCESS"})
    cli.responses.append((["down", "--yes"], ""))
    for _ in range(60):
        cli.listing(**{OLD: "REMOVING"})
    with pytest.raises(deploy.DeploymentError, match="REMOVED within"):
        deploy.BotDeployer(configuration).deploy(SHA)
    assert not any("up" in call for call in cli.calls)


@pytest.mark.parametrize("after", [{}, {OLD: "REMOVED", OTHER: "QUEUED"}])
def test_missing_old_or_concurrent_new_deployment_during_removal_aborts(cli, configuration, after):
    cli.listing(**{OLD: "SUCCESS"})
    cli.responses.append((["down", "--yes"], ""))
    cli.listing(**after)
    with pytest.raises(deploy.DeploymentError):
        deploy.BotDeployer(configuration).deploy(SHA)
    assert not any("up" in call for call in cli.calls)


@pytest.mark.parametrize("state", ["FAILED", "CRASHED", "SKIPPED", "REMOVED", "NEEDS_APPROVAL"])
def test_new_deployment_failure_is_not_success_or_automatically_retried(cli, configuration, state):
    cli.listing()
    cli.listing()
    cli.responses.append((["up"], json.dumps({"deploymentId": NEW})))
    cli.listing(**{NEW: state})
    with pytest.raises(deploy.DeploymentError, match=state):
        deploy.BotDeployer(configuration).deploy(SHA)
    assert sum(call[1] == "up" for call in cli.calls) == 1


def test_concurrent_success_does_not_substitute_for_uploaded_id(cli, configuration):
    cli.listing()
    cli.listing()
    cli.responses.append((["up"], json.dumps({"deploymentId": NEW})))
    cli.listing(**{NEW: "BUILDING", OTHER: "SUCCESS"})
    with pytest.raises(deploy.DeploymentError, match="Concurrent"):
        deploy.BotDeployer(configuration).deploy(SHA)


def test_new_deployment_timeout_is_bounded(cli, configuration):
    cli.listing()
    cli.listing()
    cli.responses.append((["up"], json.dumps({"deploymentId": NEW})))
    for _ in range(180):
        cli.listing(**{NEW: "BUILDING"})
    with pytest.raises(deploy.DeploymentError, match="fifteen minutes"):
        deploy.BotDeployer(configuration).deploy(SHA)
    assert not cli.responses


@pytest.mark.parametrize(
    "raw",
    [
        "private non-JSON output",
        "{}",
        '[{"id": "private", "status": "SUCCESS"}]',
        '[{"id": "private", "status": {}}]',
        json.dumps([{"id": OLD, "status": "FAILED"}] * 1000),
    ],
)
def test_unexpected_history_fails_without_exposing_output(cli, configuration, raw):
    cli.responses.append((["deployment", "list"], raw))
    with pytest.raises(deploy.DeploymentError) as error:
        deploy.BotDeployer(configuration).deploy(SHA)
    assert "private" not in str(error.value)
    assert len(cli.calls) == 1


@pytest.mark.parametrize(
    "result",
    [
        (1, "Bearer synthetic-sensitive-data"),
        subprocess.TimeoutExpired("private command", 45, output="synthetic-sensitive-data"),
    ],
)
def test_cli_failure_or_timeout_never_echoes_raw_output(cli, result, capsys):
    cli.responses.append((["down"], result))
    with pytest.raises(deploy.DeploymentError) as error:
        deploy.command(
            [
                "railway",
                "down",
                "--project",
                PROJECT,
                "--environment",
                ENVIRONMENT,
                "--service",
                SERVICE,
            ]
        )
    assert "synthetic-sensitive" not in str(error.value)
    assert not capsys.readouterr().out


@pytest.mark.parametrize(
    "changed",
    [
        {"GITHUB_REF": "refs/pull/1/merge"},
        {"GITHUB_REF": "refs/heads/other"},
        {"GITHUB_EVENT_NAME": "pull_request"},
        {"GITHUB_ACTIONS": "false"},
        {"GITHUB_REPOSITORY": "other/omni-task"},
        {"GITHUB_SHA": "invalid"},
    ],
)
def test_only_trusted_main_push_or_manual_run_is_allowed(cli, configuration, changed):
    with pytest.raises(deploy.DeploymentError):
        deploy.require_main_checkout({**configuration, **changed})
    assert not cli.calls


def test_checkout_requires_exact_commit_and_no_modifications(cli, configuration):
    cli.responses.append((["rev-parse", "HEAD"], "b" * 40))
    with pytest.raises(deploy.DeploymentError, match="does not match"):
        deploy.require_main_checkout(configuration)
    cli.responses.extend(
        [(["rev-parse", "HEAD"], SHA), (["status", "--porcelain"], "?? private-file")]
    )
    with pytest.raises(deploy.DeploymentError, match="modified or untracked"):
        deploy.require_main_checkout(configuration)


def test_missing_configuration_is_explicit_skipped_summary(configuration, tmp_path, capsys):
    summary = tmp_path / "summary"
    assert not deploy.configured(
        {**configuration, "RAILWAY_TOKEN_AVAILABLE": "false", "GITHUB_STEP_SUMMARY": str(summary)},
        check_only=True,
    )
    assert "skipped" in summary.read_text()
    assert "No Railway change" in capsys.readouterr().out
    assert configuration["RAILWAY_TOKEN"] not in summary.read_text()


def test_configuration_uses_uuid_ids_and_token_without_printing_values(configuration):
    assert deploy.configured(configuration)
    with pytest.raises(deploy.DeploymentError):
        deploy.configured({**configuration, "RAILWAY_BOT_SERVICE_ID": "api-not-a-bot-id"})


def test_workflow_serializes_main_only_and_limits_token_to_deploy_step():
    source = (Path(__file__).resolve().parents[1] / ".github/workflows/railway-bot.yml").read_text()
    assert "branches: [main]" in source
    assert "workflow_dispatch:" in source
    assert "pull_request" not in source
    assert "cancel-in-progress: false" in source
    assert "ref: ${{ github.sha }}" in source
    assert "persist-credentials: false" in source
    assert "@railway/cli@5.63.1" in source
    assert source.count("RAILWAY_TOKEN: ${{ secrets.RAILWAY_TOKEN }}") == 1
    assert source.index("Install pinned Railway CLI") < source.index("RAILWAY_TOKEN: ${{")
