"""Create one synthetic reviewer for local multi-user browser validation."""

import json
import secrets
from pathlib import Path

import httpx


def main():
    directory = Path(__file__).resolve().parents[1] / "var"
    credentials = json.loads((directory / "development-login.json").read_text(encoding="utf-8"))
    destination = directory / "development-reviewer-login.json"
    if destination.exists():
        print("Existing reviewer credentials retained.")
        return
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=20) as client:
        client.post(
            "/api/auth/login", json={key: credentials[key] for key in ("tenant_slug", "email", "password")}
        ).raise_for_status()
        email = "reviewer-browser@example.test"
        users = client.get("/api/users")
        users.raise_for_status()
        if any(user["email"] == email for user in users.json()["items"]):
            raise RuntimeError("Reviewer already exists without local credentials; recover rather than reset")
        password = secrets.token_urlsafe(30)
        response = client.post(
            "/api/users", json={"email": email, "display_name": "浏览器联调审核员", "password": password}
        )
        response.raise_for_status()
        user_id = response.json()["id"]
        saved = {
            "tenant_slug": credentials["tenant_slug"],
            "email": email,
            "password": password,
            "project_id": credentials["project_id"],
            "user_id": user_id,
        }
        with destination.open("x", encoding="utf-8") as output:
            json.dump(saved, output, ensure_ascii=False)
        client.post(
            "/api/members",
            json={"project_id": credentials["project_id"], "user_id": user_id, "role": "reviewer"},
        ).raise_for_status()
    print("Synthetic reviewer created; credentials retained only in ignored var file.")


if __name__ == "__main__":
    main()
