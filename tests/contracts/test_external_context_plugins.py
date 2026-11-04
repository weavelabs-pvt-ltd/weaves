import json

import httpx

from tests.support.runtime import LocalPlatformRuntime


def _invoke(runtime, capability_id, installation_id, token_query):
    version = runtime.agent_versions.get("local-assistant-v1").model_copy(
        update={
            "allowed_capability_ids": (
                *runtime.agent_versions.get(
                    "local-assistant-v1"
                ).allowed_capability_ids,
                capability_id,
            )
        }
    )
    return runtime.plugin_gateway.invoke(
        org_id="local-org",
        workspace_id="local-workspace",
        installation_id=installation_id,
        agent_version=version,
        capability_id=capability_id,
        payload={"query": token_query},
    )


def test_slack_search_stays_in_the_configured_channel():
    runtime = LocalPlatformRuntime()
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": {
                        "matches": [
                            {
                                "channel": {"name": "engineering"},
                                "ts": "100.25",
                                "username": "sawarn",
                                "text": "Release checklist is ready.",
                                "permalink": "https://slack.example/message/100",
                            },
                            {
                                "channel": {"name": "general"},
                                "ts": "100.26",
                                "username": "someone",
                                "text": "Out of scope.",
                            },
                        ]
                    },
                },
            )
        )
    )
    runtime.slack_plugin._client = client
    try:
        runtime.bootstrap()
        installation = (
            runtime.integration_connection_service.create_slack_oauth_installation(
                display_name="Engineering Slack",
                token_bundle=json.dumps(
                    {
                        "weaves_oauth": True,
                        "provider": "slack",
                        "access_token": "xoxp-test-token",
                    }
                ),
                channels=("engineering",),
            )
        )

        result = _invoke(
            runtime,
            "slack.messages.search",
            installation.plugin_installation_id,
            "release checklist",
        )

        assert len(result.output["documents"]) == 1
        assert result.output["documents"][0]["source_id"] == "slack:engineering:100.25"
    finally:
        client.close()
        runtime.close()


def test_slack_search_can_include_replies_from_one_allowlisted_thread():
    methods = []

    def respond(request):
        methods.append(request.url.path.rsplit("/", 1)[-1])
        if request.url.path.endswith("/search.messages"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": {
                        "matches": [
                            {
                                "channel": {
                                    "id": "C-ENGINEERING",
                                    "name": "engineering",
                                },
                                "ts": "200.25",
                                "thread_ts": "200.00",
                                "reply_count": 2,
                                "username": "sawarn",
                                "text": "Release checklist is ready.",
                                "permalink": "https://slack.example/message/200",
                            }
                        ]
                    },
                },
            )
        assert request.url.path.endswith("/conversations.replies")
        assert request.url.params["channel"] == "C-ENGINEERING"
        assert request.url.params["ts"] == "200.00"
        assert request.url.params["limit"] == "10"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {"user": "U1", "text": "Release checklist is ready."},
                    {"user": "U2", "text": "QA has completed the smoke pass."},
                    {"user": "U3", "text": "The rollout starts at 14:00 UTC."},
                ],
            },
        )

    runtime = LocalPlatformRuntime()
    client = httpx.Client(transport=httpx.MockTransport(respond))
    runtime.slack_plugin._client = client
    try:
        runtime.bootstrap()
        installation = (
            runtime.integration_connection_service.create_slack_oauth_installation(
                display_name="Engineering Slack with threads",
                token_bundle=json.dumps(
                    {
                        "weaves_oauth": True,
                        "provider": "slack",
                        "access_token": "xoxp-test-token",
                    }
                ),
                channels=("engineering",),
                include_thread_replies=True,
            )
        )

        result = _invoke(
            runtime,
            "slack.messages.search",
            installation.plugin_installation_id,
            "release checklist",
        )

        assert methods == ["search.messages", "conversations.replies"]
        assert len(result.output["documents"]) == 1
        document = result.output["documents"][0]
        assert document["source_id"] == "slack:engineering:200.25"
        assert "U2: QA has completed the smoke pass." in document["text"]
        assert "U3: The rollout starts at 14:00 UTC." in document["text"]
    finally:
        client.close()
        runtime.close()


def test_jira_search_carries_the_project_allowlist_into_jql():
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/project/WEAVES"):
            return httpx.Response(200, json={"key": "WEAVES"})
        body = json.loads(request.content)
        assert body["jql"].startswith("project in (WEAVES) AND text ~")
        assert body["maxResults"] == 5
        return httpx.Response(
            200,
            json={
                "issues": [
                    {
                        "key": "WEAVES-17",
                        "fields": {
                            "project": {"key": "WEAVES"},
                            "summary": "Release checklist",
                            "status": {"name": "In Progress"},
                            "issuetype": {"name": "Task"},
                            "description": {
                                "type": "doc",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [{"type": "text", "text": "Ready."}],
                                    }
                                ],
                            },
                        },
                    }
                ]
            },
        )

    runtime = LocalPlatformRuntime()
    client = httpx.Client(transport=httpx.MockTransport(respond))
    runtime.jira_plugin._client = client
    try:
        runtime.bootstrap()
        installation = (
            runtime.integration_connection_service.create_jira_oauth_installation(
                display_name="Weaves Jira",
                token_bundle=json.dumps(
                    {
                        "weaves_oauth": True,
                        "provider": "jira",
                        "access_token": "jira-test-token",
                    }
                ),
                site_url="https://example.atlassian.net",
                cloud_id="cloud-weaves",
                project_keys=("WEAVES",),
            )
        )

        result = _invoke(
            runtime,
            "jira.issues.search",
            installation.plugin_installation_id,
            "release checklist",
        )

        assert len(result.output["documents"]) == 1
        assert result.output["documents"][0]["source_id"] == "jira:WEAVES-17"
        assert result.output["documents"][0]["text"].endswith("Ready.")
        assert len(requests) == 1
    finally:
        client.close()
        runtime.close()
