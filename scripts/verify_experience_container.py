"""Exercise the published experience image in a disposable, synthetic workspace."""

import argparse
import http.cookiejar
import json
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from uuid import uuid4


def docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", check=check)


class API:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}/api/"
        self.http = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def call(self, path, data=None, method=None, content_type="application/json"):
        payload = json.dumps(data).encode() if isinstance(data, dict) else data
        request = urllib.request.Request(
            self.base + path, data=payload, method=method, headers={"Content-Type": content_type}
        )
        with self.http.open(request, timeout=10) as response:
            return json.load(response)


def wait_ready(process, log_path, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Experience exited before ready:\n" + log_path.read_text(encoding="utf-8"))
        if "体验已就绪" in log_path.read_text(encoding="utf-8", errors="replace"):
            return
        time.sleep(0.25)
    raise TimeoutError("Experience did not finish sample initialization within 180 seconds")


def verify(image):
    identity = "ontweft-smoke-" + uuid4().hex[:12]
    volume, container = identity + "-data", identity
    password = secrets.token_urlsafe(24)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    info = json.loads(docker("image", "inspect", image).stdout)[0]
    assert info["Architecture"] == "amd64" and info["Os"] == "linux"
    result = {"image": image, "image_id": info["Id"], "platform": "linux/amd64", "checks": []}
    docker("volume", "create", volume)
    process = None
    with tempfile.TemporaryDirectory(prefix="ontweft-container-smoke-") as temporary:
        try:
            for iteration in range(2):
                log_path = Path(temporary) / f"start-{iteration}.log"
                started = time.monotonic()
                with log_path.open("wb") as log:
                    process = subprocess.Popen(
                        [
                            "docker",
                            "run",
                            "--name",
                            container,
                            "-i",
                            "-p",
                            f"127.0.0.1:{port}:8020",
                            "-v",
                            f"{volume}:/data",
                            image,
                        ],
                        stdin=subprocess.PIPE,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                    if iteration == 0:
                        process.stdin.write(f"{password}\n{password}\n".encode())
                        process.stdin.flush()
                    process.stdin.close()
                    wait_ready(process, log_path)
                    result["first_start_seconds" if iteration == 0 else "restart_seconds"] = round(
                        time.monotonic() - started, 2
                    )
                    state = json.loads(
                        docker("exec", container, "cat", "/data/.ontweft-product-inventory.json").stdout
                    )
                    api = API(port)
                    api.call("auth/login", {"username": "owner", "password": password})
                    project = state["project_id"]
                    assert api.call(f"objects?project_id={project}")["total"] == 20
                    assert api.call(f"relations?project_id={project}")["total"] == 24
                    relation_id = state["relation_ids"]["suits:product-0:scenario-0"]
                    relation = api.call(f"relations/{relation_id}")
                    assert relation["evidence"][0]["parse_id"]
                    if iteration == 0:
                        original_condition = relation["properties"]["condition"]
                        updated = api.call(
                            f"relations/{relation_id}",
                            {
                                "expected_revision": relation["revision"],
                                "expected_model_release_id": state["release_id"],
                                "properties": {"condition": "Synthetic condition retained after restart"},
                            },
                            "PATCH",
                        )
                        assert updated["revision"] > relation["revision"]
                        history = api.call(f"relations/{relation_id}/history")["items"]
                        assert any(
                            row["properties"].get("condition") == original_condition for row in history
                        )
                        # A second upload must be parsed by the running worker, not by this test.
                        boundary = "ontweft-" + uuid4().hex
                        body = (
                            f'--{boundary}\r\nContent-Disposition: form-data; name="project_id"\r\n\r\n{project}\r\n'
                            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="smoke.md"\r\n'
                            f"Content-Type: text/markdown\r\n\r\n# Synthetic worker check\nNo customer data.\r\n--{boundary}--\r\n"
                        ).encode()
                        uploaded = api.call(
                            "documents/upload", body, content_type=f"multipart/form-data; boundary={boundary}"
                        )
                        deadline = time.monotonic() + 60
                        while time.monotonic() < deadline:
                            document = api.call("documents/" + uploaded["document"]["id"])
                            if document["state"] in {"ready", "failed"}:
                                break
                            time.sleep(0.25)
                        assert document["state"] == "ready", "Existing worker did not parse a fresh upload"
                        result["checks"].extend(
                            [
                                "20 objects / 24 relations",
                                "fixed document evidence",
                                "relation edit and history",
                                "worker parses fresh upload",
                            ]
                        )
                    else:
                        assert (
                            relation["properties"]["condition"]
                            == "Synthetic condition retained after restart"
                        )
                        assert "设置密码" not in log_path.read_text(encoding="utf-8", errors="replace")
                        result["checks"].append(
                            "restart preserves edits without reseeding or password prompt"
                        )
                    projects = api.call("projects")["items"]
                    other = next(row for row in projects if row["id"] != project)
                    assert api.call(f"objects?project_id={other['id']}")["total"] == 0
                    with api.http.open(f"http://127.0.0.1:{port}/", timeout=10) as response:
                        assert b"<html" in response.read().lower()
                    bindings = json.loads(docker("inspect", container).stdout)[0]["NetworkSettings"]["Ports"][
                        "8020/tcp"
                    ]
                    assert all(binding["HostIp"] == "127.0.0.1" for binding in bindings)
                    stopped = time.monotonic()
                    docker("stop", "--time", "20", container)
                    assert process.wait(timeout=25) == 0
                    status = json.loads(docker("inspect", container).stdout)[0]["State"]
                    assert not status["Running"] and status["ExitCode"] == 0
                    result[f"stop_{iteration + 1}_seconds"] = round(time.monotonic() - stopped, 2)
                docker("rm", container)
            result["checks"].extend(
                [
                    "blank project stays empty",
                    "compiled UI served",
                    "loopback-only host port",
                    "API and worker stop cleanly",
                ]
            )
        finally:
            docker("rm", "-f", container, check=False)
            if process is not None and process.poll() is None:
                process.wait(timeout=30)
            docker("volume", "rm", volume, check=False)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = verify(args.image)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
