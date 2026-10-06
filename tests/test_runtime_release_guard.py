"""Release-freeze checks use disposable repositories and fake process/health boundaries."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pc2-deploy-watchdog.py"


@pytest.fixture
def helper():
    spec = importlib.util.spec_from_file_location("isolated_release_guard", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    local_app_data = tmp_path / "app-data"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for name, value in (
        ("user.name", "Isolated Release Fixture"),
        ("user.email", "fixture@example.invalid"),
        ("core.autocrlf", "false"),
        ("core.hooksPath", str(root / "no-hooks")),
    ):
        subprocess.run(["git", "config", name, value], cwd=root, check=True)
    (root / "no-hooks").mkdir()
    package = root / "chat_watchdog"
    package.mkdir()
    (package / "registry.py").write_text("RELEASE_SENTINEL = 'current-generation'\n", encoding="utf-8")
    (package / "observation_client.py").write_text("OBSERVATION_SENTINEL = 'sidecar-only'\n", encoding="utf-8")
    subprocess.run(["git", "add", "chat_watchdog"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "isolated current module"], cwd=root, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    return root, local_app_data / "chat-watchdog", commit


@pytest.mark.parametrize("dirty", ["tracked", "staged", "untracked"])
def test_prepare_refuses_uncommitted_functional_source_before_creating_release(helper, source, dirty):
    root, runtime, _commit = source
    if dirty == "untracked":
        (root / "chat_watchdog" / "new_behavior.py").write_text("NEW_BEHAVIOR = True\n")
    else:
        (root / "chat_watchdog" / "registry.py").write_text("UNREVIEWED_BEHAVIOR = True\n")
        if dirty == "staged":
            subprocess.run(["git", "add", "chat_watchdog/registry.py"], cwd=root, check=True)
    with pytest.raises((subprocess.CalledProcessError, RuntimeError)):
        helper.prepare(root)
    assert not (runtime / "releases").exists()


def test_prepare_exports_exact_clean_commit_and_current_modules(helper, source):
    root, runtime, expected_commit = source
    commit, target = helper.prepare(root)
    assert commit == expected_commit
    assert target == runtime / "releases" / expected_commit
    assert not (target / ".git").exists()
    for name in ("registry.py", "observation_client.py"):
        expected = subprocess.check_output(
            ["git", "show", f"{commit}:chat_watchdog/{name}"], cwd=root,
        )
        assert (target / "chat_watchdog" / name).read_bytes() == expected
    assert helper.prepare(root) == (commit, target)
    # Dirty source after export must not silently inherit the release's SHA.
    (root / "chat_watchdog" / "registry.py").write_text("LATER_CHANGE = True\n")
    with pytest.raises(subprocess.CalledProcessError):
        helper.prepare(root)
    assert (target / "chat_watchdog" / "registry.py").read_text() == (
        "RELEASE_SENTINEL = 'current-generation'\n"
    )


@pytest.mark.parametrize("tamper", ["registry", "observation", "missing_observation"])
def test_existing_release_with_modified_bytes_is_not_accepted_as_immutable_commit(helper, source, tamper):
    root, _runtime, _commit = source
    _commit, target = helper.prepare(root)
    if tamper == "missing_observation":
        (target / "chat_watchdog" / "observation_client.py").unlink()
    else:
        name = "registry.py" if tamper == "registry" else "observation_client.py"
        (target / "chat_watchdog" / name).write_text("HOT_MODIFIED_RELEASE = True\n")
    with pytest.raises(RuntimeError, match="export|release|immutable|mismatch|modified"):
        helper.prepare(root)


def test_generated_bytecode_does_not_invalidate_unchanged_release(helper, source):
    root, _runtime, _commit = source
    commit, target = helper.prepare(root)
    cache = target / "chat_watchdog" / "__pycache__"
    cache.mkdir()
    (cache / "registry.cpython-fixture.pyc").write_bytes(b"isolated generated cache")
    assert helper.prepare(root) == (commit, target)


def activation_fakes(helper, source, monkeypatch, health_value):
    root, runtime, commit = source
    _, target = helper.prepare(root)
    launcher = runtime / "start-current-watchdog.cmd"
    launcher.write_text("@echo off\noriginal exact launcher\n", encoding="utf-8")
    launches = []
    ticks = iter((0.0, 0.0, 21.0))
    replies = iter((None, health_value))

    def fake_launch(*args, **kwargs):
        launches.append((args, kwargs))
        return SimpleNamespace(pid=100)

    monkeypatch.setattr(helper, "prepare", lambda _root: (commit, target))
    monkeypatch.setattr(helper, "health", lambda: next(replies))
    monkeypatch.setattr(helper, "time", SimpleNamespace(
        monotonic=lambda: next(ticks), sleep=lambda _seconds: None,
    ))
    monkeypatch.setattr(helper, "subprocess", SimpleNamespace(
        Popen=fake_launch, DEVNULL=-3, DETACHED_PROCESS=8, CREATE_NEW_PROCESS_GROUP=512, CREATE_NO_WINDOW=0x08000000,
    ))
    return root, runtime, commit, target, launches


@pytest.mark.parametrize("ready", [False, None, "true", 1])
def test_activate_never_reports_success_for_matching_but_unready_runtime(helper, source, monkeypatch, ready):
    _root, runtime, commit = source
    health_value = {
        "module_path": str(runtime / "releases" / commit / "chat_watchdog" / "registry.py"),
        "store_path": str(runtime / "registry-v2.sqlite3"), "ready": ready,
    }
    root, runtime, commit, target, launches = activation_fakes(
        helper, source, monkeypatch, health_value,
    )
    with pytest.raises(RuntimeError, match="did not become ready"):
        helper.activate(root)
    assert len(launches) == 1
    assert (runtime / f"start-current-watchdog.before-{commit}.cmd").read_text() == (
        "@echo off\noriginal exact launcher\n"
    )
    assert str(target) in (runtime / "start-current-watchdog.cmd").read_text()


def test_activate_success_requires_exact_runtime_store_and_true_readiness(helper, source, monkeypatch):
    _root, runtime, commit = source
    health_value = {
        "module_path": str(runtime / "releases" / commit / "chat_watchdog" / "registry.py"),
        "store_path": str(runtime / "registry-v2.sqlite3"), "ready": True,
    }
    root, _runtime, commit, target, launches = activation_fakes(
        helper, source, monkeypatch, health_value,
    )
    result = helper.activate(root)
    assert result == {"commit": commit, "runtime": str(target), "health": health_value}
    assert len(launches) == 1


def test_activation_launches_background_runtime_without_allocating_console(helper, source, monkeypatch):
    _root, runtime, commit = source
    health_value = {
        "module_path": str(runtime / "releases" / commit / "chat_watchdog" / "registry.py"),
        "store_path": str(runtime / "registry-v2.sqlite3"), "ready": True,
    }
    root, runtime, _commit, target, launches = activation_fakes(helper, source, monkeypatch, health_value)
    helper.activate(root)
    args, kwargs = launches[0]
    assert Path(args[0][0]).name == "pythonw.exe"
    assert args[0][1:3] == ["-m", "chat_watchdog"]
    assert "--fixed-action-scheduler" in args[0]
    assert kwargs["cwd"] == target
    assert kwargs["creationflags"] & 0x08000000
    assert not kwargs["creationflags"] & 8
    assert Path(kwargs["stdout"].name) == runtime / "quiet-watchdog.stdout.log"
    assert Path(kwargs["stderr"].name) == runtime / "quiet-watchdog.stderr.log"
    launcher = (runtime / "start-current-watchdog.cmd").read_text()
    assert 'start "" /b ' in launcher
    assert "pythonw.exe" in launcher
    assert "--fixed-action-scheduler" in launcher


@pytest.mark.parametrize("mismatch", ["module_path", "store_path"])
def test_activate_rejects_ready_listener_from_other_runtime_or_store(helper, source, monkeypatch, mismatch):
    _root, runtime, commit = source
    health_value = {
        "module_path": str(runtime / "releases" / commit / "chat_watchdog" / "registry.py"),
        "store_path": str(runtime / "registry-v2.sqlite3"), "ready": True,
    }
    health_value[mismatch] = str(runtime / "different-instance" / "registry.py")
    root, _runtime, _commit, _target, _launches = activation_fakes(
        helper, source, monkeypatch, health_value,
    )
    with pytest.raises(RuntimeError, match="different runtime|different registry"):
        helper.activate(root)
