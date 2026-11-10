def upload(client, project_id, content=b"# Evidence\nVerified source paragraph.", filename="source.md"):
    response = client.post(
        "/api/documents/upload", data={"project_id": project_id}, files={"file": (filename, content)}
    )
    assert response.status_code == 201, response.text
    return response.json()
