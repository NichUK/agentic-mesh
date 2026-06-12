from pathlib import Path

from agentic_mesh_v2.prompt_builder import PromptAssembler
from agentic_mesh_v2.role_service import RoleAssignment


def test_prompt_assembler_renders_xml_sections_from_config_role_project_and_assignment() -> None:
    project_file = Path("examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml")
    assembler = PromptAssembler(
        project_file=project_file,
        config_root=Path.cwd(),
    )

    rendered = assembler.render(
        RoleAssignment(
            role_id="product-manager",
            role_instance_id="agentic-mesh-dev.product-manager.1",
            work_item_id="work-prompt-1",
            title="Shape prompt assembly",
            summary="Build XML-section worker prompts.",
            assignment_id="assignment-prompt-1",
            assignment_type="work_item_handoff",
            source_ref="queue-prompt-1",
            visibility_scope="project",
            payload={
                "current_flow_state": "product_definition",
                "source_documents": ["docs/product/prompt-assembly.md"],
                "target_outputs": ["020-product-definition.md"],
            },
            conversation_context=("Sponsor asked for real role prompts.",),
            memory_context=("Previous v1 prompts were too JSON-shaped.",),
        )
    )

    prompt = rendered.prompt_text
    assert prompt.startswith('<agentic-mesh-worker-prompt version="v2">')
    assert "<system-security>" in prompt
    assert "<safe-outputs>" in prompt
    assert "<available-tools>" in prompt
    assert "<tool>status.reply</tool>" in prompt
    assert "<role>" in prompt
    assert "<role-profile>" in prompt
    assert "product accountability owner" in prompt
    assert "<project>" in prompt
    assert "Build Agentic Mesh into an open-core" in prompt
    assert "<assignment>" in prompt
    assert "Shape prompt assembly" in prompt
    assert "<current-flow-state>" in prompt
    assert "product_definition" in prompt
    assert "<conversation-context>" in prompt
    assert "Sponsor asked for real role prompts." in prompt
    assert "<memory-context>" in prompt
    assert rendered.component_manifest["safe_output_tools"]
