import importlib.util
import signal
import socket
import sys
from types import SimpleNamespace

import httpx
import pytest

from tests.support.paths import REPO_ROOT as ROOT

spec = importlib.util.spec_from_file_location("ontweft_experience", ROOT / "scripts/start_local_demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


@pytest.mark.parametrize("case", ["fresh", "restart", "seed_failure", "worker_exit", "health_timeout"])
def test_experience_owns_children_and_only_reports_ready_after_sample(tmp_path, monkeypatch, capsys, case):
    events, spawned, requests = [], [], []
    monkeypatch.setattr(demo, "check_experience_port", lambda *args: events.append("port"))
    complete = case == "restart"
    state = {"status": "complete", "project_id": "sample-project", "objects": 20, "relations": 24}

    def ensure_sample(client, data):
        events.append("sample")
        assert data == tmp_path
        if case == "seed_failure":
            raise RuntimeError("synthetic seed failure")
        return state

    monkeypatch.setitem(
        sys.modules,
        "experience_sample",
        SimpleNamespace(ensure_sample=ensure_sample, read_sample_state=lambda _: state if complete else None),
    )
    monkeypatch.setattr(demo, "inspect_directory", lambda _: complete)
    passwords = iter(["Local-Test-Only-2026", "Local-Test-Only-2026"])

    def password(_):
        assert not complete, "A complete sample restart must not ask for credentials"
        return next(passwords)

    monkeypatch.setattr(demo.getpass, "getpass", password)
    monkeypatch.setattr(
        demo,
        "create_demo_app",
        lambda *args, **kwargs: SimpleNamespace(
            state=SimpleNamespace(
                db=SimpleNamespace(engine=SimpleNamespace(dispose=lambda: events.append("dispose")))
            )
        ),
    )

    class Child:
        def __init__(self, command, options):
            self.command, self.options = command, options
            self.pid = len(spawned) + 100

        def poll(self):
            return 9 if case == "worker_exit" and "worker" in self.command else None

    def popen(command, **options):
        child = Child(command, options)
        spawned.append(child)
        return child

    monkeypatch.setattr(demo.subprocess, "Popen", popen)
    monkeypatch.setenv("ONTOLOGY_DATABASE_URL", "postgresql://never-connect.invalid/test")
    monkeypatch.setattr(
        demo, "stop_experience_children", lambda children: events.append(("stop", list(children)))
    )

    class Client:
        def __init__(self, **options):
            assert options["base_url"] == "http://127.0.0.1:8031/api/"
            assert options["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, path, **options):
            requests.append(path)
            assert path == "health"
            return httpx.Response(503 if case == "health_timeout" else 200)

        def post(self, path, **options):
            requests.append(path)
            if path == "auth/login":
                assert options["json"] == {"username": "owner", "password": "Local-Test-Only-2026"}
            return httpx.Response(200, request=httpx.Request("POST", "http://127.0.0.1/api/" + path))

    monkeypatch.setattr(demo.httpx, "Client", Client)
    if case == "health_timeout":
        times = iter([0, 1, 61])
        monkeypatch.setattr(demo.time, "monotonic", lambda: next(times))

    def sleep(seconds):
        if seconds == 0.5:
            raise KeyboardInterrupt

    monkeypatch.setattr(demo.time, "sleep", sleep)
    previous_term = signal.getsignal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt if case in {"fresh", "restart"} else RuntimeError):
        demo.run_experience(tmp_path, port=8031, container=case == "fresh")
    assert signal.getsignal(signal.SIGTERM) == previous_term
    assert len(spawned) == 2
    assert [child.command[2] for child in spawned] == ["serve", "worker"]
    assert ("--container" in spawned[0].command) == (case == "fresh")
    assert all(not any(key.startswith("ONTOLOGY_") for key in child.options["env"]) for child in spawned)
    assert all("Local-Test-Only-2026" not in child.command for child in spawned)
    assert events[-1] == ("stop", spawned)
    assert events[0] == "port"
    output = capsys.readouterr().out
    if case in {"fresh", "restart"}:
        assert "体验已就绪：http://127.0.0.1:8031/#graph?project=sample-project" in output
    else:
        assert "体验已就绪" not in output
    if complete:
        assert "sample" not in events and requests == ["health"]
    elif case == "fresh":
        assert events.index("dispose") < events.index("sample")
        assert requests == ["health", "auth/login", "auth/logout"]


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_experience_shutdown_targets_only_its_live_processes(monkeypatch, platform):
    commands, waits = [], []

    class Child:
        def __init__(self, pid, finished=False):
            self.pid, self.finished = pid, finished

        def poll(self):
            return 0 if self.finished else None

        def wait(self, timeout=None):
            waits.append((self.pid, timeout))
            return 0

    children = [Child(101), Child(202, finished=True)]
    monkeypatch.setattr(
        demo, "os", SimpleNamespace(name=platform, killpg=lambda *args: commands.append(args))
    )
    monkeypatch.setattr(demo.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(demo.subprocess, "run", lambda command, **kwargs: commands.append(command))
    demo.stop_experience_children(children)
    assert waits == [(101, 10)]
    assert commands == (
        [["taskkill", "/PID", "101", "/T", "/F"]] if platform == "nt" else [(101, signal.SIGTERM)]
    )


def test_experience_termination_uses_the_same_cleanup_path():
    with pytest.raises(KeyboardInterrupt):
        demo.interrupt_experience(signal.SIGTERM, None)


def test_experience_refuses_an_occupied_port_before_initializing(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))

    def unexpected_password():
        raise AssertionError("An occupied port must be rejected before account initialization")

    monkeypatch.setattr(demo, "new_password", unexpected_password)
    with socket.socket() as existing_server:
        existing_server.bind(("127.0.0.1", 0))
        existing_server.listen()
        port = existing_server.getsockname()[1]
        with pytest.raises(ValueError, match="端口"):
            demo.run_experience(tmp_path, port=port)
    assert not list(tmp_path.iterdir())
