def publish(client, project_id, definition):
    baseline = client.get("/api/ontology/definition", params={"project_id": project_id}).json()["release_id"]
    response = client.post(
        "/api/changesets",
        json={
            "project_id": project_id,
            "title": "初始模型",
            "definition": definition,
            "expected_baseline_release_id": baseline,
        },
    )
    assert response.status_code == 201, response.text
    row = response.json()
    for action in ("validate", "approve", "publish"):
        response = client.post(
            f"/api/changesets/{row['id']}/{action}", json={"expected_revision": row["revision"]}
        )
        assert response.status_code == 200, response.text
        row = response.json()
    return row
