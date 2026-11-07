import json
from pathlib import Path

from contracts.v1 import (
    AgentBudget,
    AgentBundle,
    AgentDefinition,
    AgentRun,
    AgentRunBundle,
    AgentSkillSnapshot,
    AgentVersion,
    ApiCredential,
    ApprovalPolicy,
    ApprovalRequest,
    Artifact,
    ArtifactProvenance,
    AuditEvent,
    CapabilitySpec,
    ConversationThread,
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationExecution,
    EvaluationReport,
    EvaluationReportRecord,
    EvaluationSuite,
    EvaluationSuiteRecord,
    ExecutionJob,
    ModelMessage,
    ModelProfile,
    ModelProfileBinding,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    Organization,
    PluginDescriptor,
    PluginInstallation,
    PluginInstallationBinding,
    Principal,
    Role,
    RoleBinding,
    SkillDefinition,
    SkillPublication,
    SkillRevocation,
    SkillVersion,
    Task,
    TaskMessage,
    TaskTurn,
    ToolInvocation,
    User,
    WorkflowBundle,
    WorkflowDefinition,
    WorkflowParallelGroup,
    WorkflowRun,
    WorkflowSchedule,
    WorkflowStepCondition,
    WorkflowStepDependency,
    WorkflowTrigger,
    WorkflowVersion,
    Workspace,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "schemas" / "product" / "v1"
CONTRACTS = {
    "execution-job": ExecutionJob,
    "evaluation-case": EvaluationCase,
    "evaluation-case-result": EvaluationCaseResult,
    "evaluation-execution": EvaluationExecution,
    "evaluation-report": EvaluationReport,
    "evaluation-report-record": EvaluationReportRecord,
    "evaluation-suite": EvaluationSuite,
    "evaluation-suite-record": EvaluationSuiteRecord,
    "api-credential": ApiCredential,
    "organization": Organization,
    "workspace": Workspace,
    "user": User,
    "principal": Principal,
    "role": Role,
    "role-binding": RoleBinding,
    "skill-definition": SkillDefinition,
    "skill-version": SkillVersion,
    "skill-publication": SkillPublication,
    "skill-revocation": SkillRevocation,
    "model-provider": ModelProvider,
    "model-profile": ModelProfile,
    "model-profile-binding": ModelProfileBinding,
    "model-message": ModelMessage,
    "model-request": ModelRequest,
    "model-response": ModelResponse,
    "model-usage": ModelUsage,
    "agent-definition": AgentDefinition,
    "agent-version": AgentVersion,
    "agent-skill-snapshot": AgentSkillSnapshot,
    "agent-budget": AgentBudget,
    "agent-bundle": AgentBundle,
    "capability-spec": CapabilitySpec,
    "conversation-thread": ConversationThread,
    "task": Task,
    "task-message": TaskMessage,
    "task-turn": TaskTurn,
    "plugin-descriptor": PluginDescriptor,
    "plugin-installation": PluginInstallation,
    "plugin-installation-binding": PluginInstallationBinding,
    "workflow-definition": WorkflowDefinition,
    "workflow-version": WorkflowVersion,
    "workflow-step-condition": WorkflowStepCondition,
    "workflow-parallel-group": WorkflowParallelGroup,
    "workflow-step-dependency": WorkflowStepDependency,
    "workflow-bundle": WorkflowBundle,
    "workflow-run": WorkflowRun,
    "workflow-trigger": WorkflowTrigger,
    "workflow-schedule": WorkflowSchedule,
    "agent-run": AgentRun,
    "agent-run-bundle": AgentRunBundle,
    "tool-invocation": ToolInvocation,
    "artifact-provenance": ArtifactProvenance,
    "artifact": Artifact,
    "approval-policy": ApprovalPolicy,
    "approval-request": ApprovalRequest,
    "audit-event": AuditEvent,
}


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    expected_paths = {OUTPUT / f"{name}.schema.json" for name in CONTRACTS}
    for stale_path in OUTPUT.glob("*.schema.json"):
        if stale_path not in expected_paths:
            stale_path.unlink()
    for name, contract in CONTRACTS.items():
        path = OUTPUT / f"{name}.schema.json"
        path.write_text(
            json.dumps(contract.model_json_schema(), indent=2, sort_keys=True) + "\n"
        )
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
