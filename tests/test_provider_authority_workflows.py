from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str) -> tuple[str, dict[str, object]]:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    return text, yaml.safe_load(text)


def _dispatch_inputs(workflow: dict[str, object]) -> dict[str, object]:
    # PyYAML parses the workflow trigger key ``on`` as the YAML 1.1 boolean.
    trigger = workflow.get("on", workflow.get(True))
    assert isinstance(trigger, dict)
    dispatch = trigger["workflow_dispatch"]
    assert isinstance(dispatch, dict)
    inputs = dispatch["inputs"]
    assert isinstance(inputs, dict)
    return inputs


def test_registry_custodian_uses_pinned_external_inputs_and_has_no_private_key() -> None:
    text, workflow = _workflow("provider-registry-custodian.yml")
    jobs = workflow["jobs"]

    assert set(jobs) == {"enroll-publisher"}
    job = jobs["enroll-publisher"]
    assert job["environment"] == "stockdata-provider-registry-custodian"
    assert workflow["permissions"] == {"contents": "read"}

    inputs = _dispatch_inputs(workflow)
    assert set(inputs) == {
        "root_public_key_url",
        "root_public_key_sha256",
        "enrollment_url",
        "enrollment_sha256",
        "registry_version",
    }
    for name, value in inputs.items():
        assert value["required"] is True
        assert value["type"] == "string"

    assert "STOCKDATA_TRUST_ROOT_PRIVATE_KEY_B64" not in text
    assert "STOCKDATA_PROVIDER_PRIVATE_KEY_B64" not in text
    assert "generate_private_key" not in text
    assert "Ed25519PrivateKey.generate" not in text

    assert "python -m stockdata.provider_authority_publisher build-registry" in text
    for required_flag in (
        "--root-public-key",
        "--enrollment",
        "--output",
        "--registry-version",
    ):
        assert required_flag in text

    for unsupported_flag in (
        "--publisher-public-key-base64",
        "--component-roles-json",
        "--source-receipts",
    ):
        assert unsupported_flag not in text

    assert "curl --proto '=https'" in text
    assert "sha256sum --check --strict" in text


def test_component_publisher_uses_real_publish_envelope_interface() -> None:
    text, workflow = _workflow("provider-component-authority.yml")
    jobs = workflow["jobs"]

    assert set(jobs) == {"publish-component"}
    job = jobs["publish-component"]
    assert job["environment"] == "stockdata-provider-component-publisher"
    assert workflow["permissions"] == {"contents": "read"}

    # The provider signing secret must not be exposed at the job level.
    assert "env" not in job

    inputs = _dispatch_inputs(workflow)
    assert set(inputs) == {
        "component",
        "artifact_url",
        "artifact_sha256",
        "source_receipt_urls",
        "source_receipt_sha256s",
        "registry_url",
        "registry_sha256",
        "effective_at",
        "available_at",
        "publisher_key_id",
        "decision_cutoffs",
    }
    assert inputs["component"]["required"] is True
    assert inputs["component"]["type"] == "choice"
    assert inputs["component"]["options"] == [
        "trading_calendar",
        "universe",
        "instrument_status",
        "corporate_actions",
        "market_rules",
    ]
    for name, value in inputs.items():
        if name == "component":
            continue
        assert value["type"] == "string"
        assert value["required"] is True

    assert "python -m stockdata.provider_authority_publisher publish-envelope" in text
    assert "python -m stockdata.provider_authority_publisher sign-component" not in text

    assert "STOCKDATA_PROVIDER_PRIVATE_KEY_B64" in text
    assert "STOCKDATA_TRUST_ROOT_PRIVATE_KEY_B64" not in text
    assert "generate_private_key" not in text
    assert "Ed25519PrivateKey.generate" not in text

    # Receipts are passed as repeated --source-receipt arguments built in a
    # loop and expanded into publish-envelope.
    assert 'source_receipt_args+=(--source-receipt "$receipt_file")' in text
    assert '"${source_receipt_args[@]}"' in text

    # The signing key is addressed by environment variable and supplied as
    # STOCKDATA_PROVIDER_PRIVATE_KEY_B64 only in the signing step environment.
    signing_steps = [
        step
        for step in job["steps"]
        if step.get("name") == "Sign and production-verify component authority"
    ]
    assert len(signing_steps) == 1
    signing_step = signing_steps[0]
    assert signing_step["env"]["STOCKDATA_PROVIDER_PRIVATE_KEY_B64"] == (
        "${{ secrets.STOCKDATA_PROVIDER_PRIVATE_KEY_B64 }}"
    )
    for step in job["steps"]:
        if step is signing_step:
            continue
        assert "STOCKDATA_PROVIDER_PRIVATE_KEY_B64" not in step.get("env", {})

    assert "SIGNER_PRIVATE_KEY_ENV: STOCKDATA_PROVIDER_PRIVATE_KEY_B64" in text
    assert "PUBLISHER_KEY_ID: ${{ inputs.publisher_key_id }}" in text
    assert "--publisher-key-id \"$PUBLISHER_KEY_ID\"" in text
    assert "[[ \"$PUBLISHER_KEY_ID\" =~ ^[0-9a-f]{64}$ ]]" in text
    assert '--signer-private-key-env "$SIGNER_PRIVATE_KEY_ENV"' in text

    # The required decision cutoff input must be parsed into repeated
    # --decision-cutoff arguments and expanded into publish-envelope.
    assert "DECISION_CUTOFFS: ${{ inputs.decision_cutoffs }}" in text
    assert "decision_cutoff_entries=()" in text
    assert "read -r -a decision_cutoff_entries <<< \"$DECISION_CUTOFFS\"" in text
    assert "[[ \"${#decision_cutoff_entries[@]}\" -gt 0 ]]" in text
    assert "decision_cutoff_args=()" in text
    assert "decision_cutoff_args+=(--decision-cutoff \"$cutoff\")" in text
    assert '"${decision_cutoff_args[@]}"' in text

    for required_flag in (
        "--source-receipt",
        "--signer-private-key-env",
        "--effective-at",
        "--available-at",
        "--publisher-key-id",
    ):
        assert required_flag in text

    for unsupported_flag in (
        "--source-receipts",
        "--publisher-public-key-base64",
        "--component-roles-json",
    ):
        assert unsupported_flag not in text

    assert "curl --proto '=https'" in text
    assert "sha256sum --check --strict" in text


def test_workflows_pin_every_downloaded_input_and_upload_only_outputs() -> None:
    registry_text, _ = _workflow("provider-registry-custodian.yml")
    component_text, _ = _workflow("provider-component-authority.yml")

    for text in (registry_text, component_text):
        assert "workflow_dispatch:" in text
        assert "sha256sum" in text
        assert "actions/upload-artifact@" in text
        assert "retention-days: 30" in text
        assert "^[0-9a-f]{64}$" in text

    assert "root_public_key_sha256" in registry_text
    assert "enrollment_sha256" in registry_text
    assert "registry_version" in registry_text
    assert "enrolled-trust-registry.json.sha256" in registry_text

    assert "artifact_sha256" in component_text
    assert "source_receipt_sha256s" in component_text
    assert "registry_sha256" in component_text
    assert "effective_at" in component_text
    assert "available_at" in component_text
    assert "${COMPONENT}-authority-envelope.json.sha256" in component_text
