"""
Sabrina CrewAI JSON Schema Engine
==================================
Strict Pydantic v2 models for CrewAI's JSON-first configuration format.
Covers:
  - AgentConfig   (agents/<name>.jsonc)
  - TaskConfig    (crew.jsonc task entry)
  - CrewConfig    (crew.jsonc root)
  - FlowStepConfig and FlowConfig (Python-declarative Flows)

Also exposes:
  - validate_crew_json(data) → (valid: bool, errors: list[str])
  - generate_empty_crew_template() → dict
  - render_crew_jsonc(crew: CrewConfig) → str
  - test_sample_schema() — self-test for smoke validation

Usage:
    from tools.crewai_json_schema import (
        AgentConfig, TaskConfig, CrewConfig,
        validate_crew_json, render_crew_jsonc, generate_empty_crew_template
    )
"""

from __future__ import annotations

import json
import textwrap
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Enumerations ─────────────────────────────────────────────────────────────

class ProcessType(str, Enum):
    SEQUENTIAL   = "sequential"
    HIERARCHICAL = "hierarchical"


class VerbosityLevel(int, Enum):
    OFF     = 0
    NORMAL  = 1
    DEBUG   = 2


# ── Agent Settings ────────────────────────────────────────────────────────────

class AgentSettings(BaseModel):
    """Optional behaviour overrides for an agent."""
    verbose: Optional[Union[bool, VerbosityLevel]] = Field(
        None, description="Enable verbose output from this agent."
    )
    allow_delegation: Optional[bool] = Field(
        None, description="Allow this agent to delegate tasks to other agents."
    )
    max_iter: Optional[int] = Field(
        None, ge=1, le=200,
        description="Maximum reasoning iterations before returning best answer."
    )
    max_rpm: Optional[int] = Field(
        None, ge=1, description="Max requests per minute to avoid rate limits."
    )
    max_execution_time: Optional[int] = Field(
        None, ge=1, description="Hard timeout in seconds for task execution."
    )
    max_retry_limit: Optional[int] = Field(
        None, ge=0, description="Number of retries on parsing failure."
    )
    cache: Optional[bool] = Field(
        None, description="Cache tool call results for this agent."
    )
    memory: Optional[bool] = Field(
        None, description="Enable memory for this agent."
    )
    reasoning: Optional[bool] = Field(
        None, description="Enable extended thinking/reasoning mode."
    )


# ── Agent Configuration ───────────────────────────────────────────────────────

class AgentConfig(BaseModel):
    """
    JSON-first agent definition. Corresponds to agents/<name>.jsonc in a CrewAI project.

    Example:
        {
          "role": "Senior Research Specialist for {topic}",
          "goal": "Find comprehensive information about {topic}",
          "backstory": "An experienced research specialist...",
          "llm": "ollama/qwen2.5:7b",
          "tools": ["SerperDevTool"],
          "settings": {
            "verbose": true,
            "allow_delegation": false
          }
        }
    """
    role:      str = Field(..., min_length=1, description="The agent's role / function title.")
    goal:      str = Field(..., min_length=1, description="The agent's guiding objective.")
    backstory: str = Field(..., min_length=1, description="Context, persona, and expertise narrative.")
    llm:       Optional[str] = Field(
        None,
        description="LLM identifier: 'ollama/qwen2.5:7b', 'openai/gpt-4o', 'anthropic/claude-sonnet-4-6', etc.",
        examples=["ollama/qwen2.5:7b", "openai/gpt-4o", "anthropic/claude-sonnet-4-6"]
    )
    function_calling_llm: Optional[str] = Field(
        None, description="Separate LLM to use for tool/function calling."
    )
    tools: Optional[List[str]] = Field(
        default_factory=list,
        description="Tool class names available to this agent."
    )
    knowledge_sources: Optional[List[str]] = Field(
        None, description="Knowledge source identifiers for agent-level RAG."
    )
    settings: Optional[AgentSettings] = Field(
        None, description="Behavioural overrides."
    )
    system_template: Optional[str] = Field(
        None, description="Custom Jinja2 system prompt template override."
    )
    prompt_template: Optional[str] = Field(
        None, description="Custom Jinja2 human-turn prompt template override."
    )
    response_template: Optional[str] = Field(
        None, description="Custom Jinja2 response template override."
    )
    step_callback: Optional[str] = Field(
        None, description="Python dotted path to a callback invoked on each agent step."
    )

    @field_validator("tools", mode="before")
    @classmethod
    def coerce_tools(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return list(v)


# ── Task Output ───────────────────────────────────────────────────────────────

class TaskOutputSchema(BaseModel):
    """Pydantic schema reference for structured task output (output_pydantic)."""
    schema_class: str = Field(
        ...,
        description="Python dotted import path to the Pydantic model class.",
        examples=["mymodels.VulnReport", "schemas.CrewTaskResult"],
    )


# ── Task Configuration ────────────────────────────────────────────────────────

class TaskConfig(BaseModel):
    """
    JSON-first task definition. Embedded in the 'tasks' array of crew.jsonc.

    Example:
        {
          "name": "research_task",
          "description": "Conduct research on {topic}.",
          "expected_output": "A comprehensive research document.",
          "agent": "researcher",
          "context": ["prior_task_name"],
          "output_file": "output/report.md",
          "markdown": true
        }
    """
    name:            str  = Field(..., min_length=1, description="Unique task name (reference key).")
    description:     str  = Field(..., min_length=1, description="Detailed task instructions.")
    expected_output: str  = Field(..., min_length=1, description="Description of expected output format.")
    agent:           str  = Field(..., min_length=1, description="Agent name (must match an agent in 'agents' list).")
    context:         Optional[List[str]] = Field(
        None, description="List of prior task names whose outputs are passed as context."
    )
    output_file:     Optional[str] = Field(
        None, description="File path to write final output to."
    )
    output_json:     Optional[Dict[str, Any]] = Field(
        None, description="JSON Schema for structured output. Mutually exclusive with output_pydantic."
    )
    output_pydantic: Optional[TaskOutputSchema] = Field(
        None, description="Pydantic model reference for structured output."
    )
    markdown:        Optional[bool] = Field(
        None, description="If true, interprets output as markdown."
    )
    async_execution: Optional[bool] = Field(
        None, description="Run task asynchronously (parallel with other async tasks)."
    )
    callback:        Optional[str] = Field(
        None, description="Python dotted path to a callback function invoked after the task."
    )
    human_input:     Optional[bool] = Field(
        None, description="Pause and request human input to validate the output."
    )

    @model_validator(mode="after")
    def check_output_exclusivity(self) -> "TaskConfig":
        if self.output_json and self.output_pydantic:
            raise ValueError("output_json and output_pydantic are mutually exclusive.")
        return self


# ── Crew Configuration ────────────────────────────────────────────────────────

class CrewConfig(BaseModel):
    """
    Root crew.jsonc definition for a JSON-first CrewAI project.

    Example:
        {
          "name": "Security Recon Crew",
          "agents": ["researcher", "analyst"],
          "tasks": [ { "name": "...", ... } ],
          "process": "sequential",
          "verbose": true,
          "memory": true,
          "inputs": { "target": "example.com" }
        }
    """
    name:         str               = Field(..., min_length=1, description="Human-readable crew name.")
    agents:       List[str]         = Field(..., min_items=1, description="Ordered list of agent names.")
    tasks:        List[TaskConfig]  = Field(..., min_items=1, description="Ordered list of task definitions.")
    process:      ProcessType       = Field(ProcessType.SEQUENTIAL, description="Execution order strategy.")
    verbose:      Optional[bool]    = Field(None, description="Enable verbose crew-level logging.")
    memory:       Optional[bool]    = Field(None, description="Enable crew-level memory.")
    cache:        Optional[bool]    = Field(None, description="Cache tool results crew-wide.")
    full_output:  Optional[bool]    = Field(None, description="Return all task outputs (not just last).")
    manager_llm:  Optional[str]     = Field(None, description="LLM for the manager in hierarchical mode.")
    manager_agent:Optional[str]     = Field(None, description="Named agent to act as manager (hierarchical).")
    planning:     Optional[bool]    = Field(None, description="Enable pre-execution planning phase.")
    planning_llm: Optional[str]     = Field(None, description="LLM to use for planning.")
    max_rpm:      Optional[int]     = Field(None, ge=1, description="Rate limit crew-wide.")
    max_execution_time: Optional[int] = Field(None, ge=1, description="Hard timeout for the entire crew.")
    inputs:       Optional[Dict[str, Any]] = Field(
        None, description="Default input values for template variables."
    )
    knowledge_sources: Optional[List[str]] = Field(
        None, description="Crew-level knowledge sources."
    )
    output_log_file: Optional[str] = Field(
        None, description="File path to write execution logs."
    )
    embedder: Optional[Dict[str, Any]] = Field(
        None, description="Embedder config for crew memory (provider, model)."
    )

    @model_validator(mode="after")
    def validate_hierarchical_manager(self) -> "CrewConfig":
        if self.process == ProcessType.HIERARCHICAL:
            if not self.manager_llm and not self.manager_agent:
                raise ValueError(
                    "Hierarchical process requires either 'manager_llm' or 'manager_agent'."
                )
        return self

    @model_validator(mode="after")
    def validate_task_agents(self) -> "CrewConfig":
        agent_set = set(self.agents)
        for task in self.tasks:
            if task.agent not in agent_set:
                raise ValueError(
                    f"Task '{task.name}' references agent '{task.agent}' "
                    f"which is not in the crew agents list {self.agents}."
                )
        return self

    @model_validator(mode="after")
    def validate_task_context_refs(self) -> "CrewConfig":
        task_names = {t.name for t in self.tasks}
        for task in self.tasks:
            for ref in (task.context or []):
                if ref not in task_names:
                    raise ValueError(
                        f"Task '{task.name}' has context reference '{ref}' "
                        f"which is not a defined task name."
                    )
        return self


# ── Flow Configuration ────────────────────────────────────────────────────────

class FlowStateField(BaseModel):
    """A single field in a Flow's Pydantic state model."""
    name:         str  = Field(..., description="Field name.")
    type_hint:    str  = Field("str", description="Python type hint as string (e.g. 'str', 'int', 'List[str]').")
    default:      Optional[Any] = Field(None, description="Default value.")
    description:  Optional[str] = Field(None)


class FlowStep(BaseModel):
    """Describes one step/method in a Flow."""
    name:        str  = Field(..., description="Method name.")
    is_start:    bool = Field(False, description="If True, this is decorated with @start.")
    listens_to:  Optional[List[str]] = Field(None, description="Method names this step @listen(s) to.")
    router_for:  Optional[str]       = Field(None, description="Method name this step @router decorates.")
    description: Optional[str]       = Field(None)


class FlowConfig(BaseModel):
    """
    High-level schema for a CrewAI Flow definition.
    Maps to the Python class that inherits from Flow, with @start / @listen / @router decorators.
    """
    flow_name:    str             = Field(..., description="Python class name for the Flow.")
    state_model:  str             = Field("FlowState", description="Pydantic state model class name.")
    state_fields: List[FlowStateField] = Field(default_factory=list)
    steps:        List[FlowStep]  = Field(default_factory=list)
    description:  Optional[str]   = Field(None)


# ── Validation Helpers ────────────────────────────────────────────────────────

def validate_crew_json(data: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Validate a raw dict against CrewConfig schema.
    Returns (is_valid: bool, errors: list[str]).
    """
    from pydantic import ValidationError
    try:
        CrewConfig.model_validate(data)
        return True, []
    except ValidationError as exc:
        errors = []
        for err in exc.errors():
            loc = " → ".join(str(l) for l in err["loc"])
            errors.append(f"[{loc}] {err['msg']}")
        return False, errors


def validate_agent_json(data: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Validate a raw dict against AgentConfig schema."""
    from pydantic import ValidationError
    try:
        AgentConfig.model_validate(data)
        return True, []
    except ValidationError as exc:
        errors = []
        for err in exc.errors():
            loc = " → ".join(str(l) for l in err["loc"])
            errors.append(f"[{loc}] {err['msg']}")
        return False, errors


def generate_empty_crew_template(
    agent_names: Optional[List[str]] = None,
    task_count: int = 2,
) -> Dict[str, Any]:
    """Generate a minimal empty crew.jsonc template dict."""
    names = agent_names or ["researcher", "analyst"]
    tasks = []
    for i, agent in enumerate(names):
        t: Dict[str, Any] = {
            "name": f"task_{i + 1}",
            "description": f"Describe what task_{i + 1} should accomplish.",
            "expected_output": "Describe the format and content of the expected output.",
            "agent": agent,
        }
        if i > 0:
            t["context"] = [f"task_{i}"]
        tasks.append(t)

    return {
        "name": "My CrewAI Crew",
        "agents": names,
        "tasks": tasks[:task_count],
        "process": "sequential",
        "verbose": True,
        "memory": False,
        "inputs": {},
    }


def generate_empty_agent_template(agent_name: str = "my_agent") -> Dict[str, Any]:
    """Generate a minimal empty agent.jsonc template dict."""
    return {
        "role": f"Expert {agent_name.replace('_', ' ').title()}",
        "goal": "Accomplish the task assigned to you with precision and accuracy.",
        "backstory": "You are a highly skilled specialist with deep domain expertise.",
        "llm": "ollama/qwen2.5:7b",
        "tools": [],
        "settings": {
            "verbose": True,
            "allow_delegation": False,
        },
    }


def render_crew_jsonc(crew: CrewConfig, indent: int = 2) -> str:
    """Serialize a CrewConfig to a pretty-printed JSON string."""
    data = crew.model_dump(exclude_none=True)
    # Convert enum values to their string representation
    if "process" in data:
        data["process"] = data["process"] if isinstance(data["process"], str) else data["process"].value
    return json.dumps(data, indent=indent, ensure_ascii=False)


def render_agent_jsonc(agent: AgentConfig, indent: int = 2) -> str:
    """Serialize an AgentConfig to a pretty-printed JSON string."""
    data = agent.model_dump(exclude_none=True)
    return json.dumps(data, indent=indent, ensure_ascii=False)


# ── Self-Test ─────────────────────────────────────────────────────────────────

def test_sample_schema() -> None:
    """Smoke test: build a valid CrewConfig and validate it."""
    print("=== CrewAI JSON Schema Self-Test ===\n")

    # --- Valid agent ---
    researcher_data = {
        "role": "Senior Research Specialist for {topic}",
        "goal": "Find comprehensive and accurate information about {topic}.",
        "backstory": "An experienced research specialist who turns raw data into useful insights.",
        "llm": "ollama/qwen2.5:7b",
        "tools": ["SerperDevTool"],
        "settings": {"verbose": True, "allow_delegation": False, "max_iter": 15},
    }
    valid, errors = validate_agent_json(researcher_data)
    print(f"[AgentConfig] Valid: {valid}")
    if errors:
        print(f"  Errors: {errors}")

    # --- Valid crew ---
    crew_data = {
        "name": "Research Crew",
        "agents": ["researcher", "analyst"],
        "tasks": [
            {
                "name": "research_task",
                "description": "Conduct thorough research on {topic}.",
                "expected_output": "A comprehensive research document.",
                "agent": "researcher",
            },
            {
                "name": "analysis_task",
                "description": "Analyze the research and write a polished report on {topic}.",
                "expected_output": "A professional markdown report.",
                "agent": "analyst",
                "context": ["research_task"],
                "output_file": "output/report.md",
                "markdown": True,
            },
        ],
        "process": "sequential",
        "verbose": True,
        "memory": True,
        "inputs": {"topic": "CrewAI Framework Architecture"},
    }
    valid, errors = validate_crew_json(crew_data)
    print(f"[CrewConfig]  Valid: {valid}")
    if errors:
        print(f"  Errors: {errors}")

    # --- Invalid crew (unknown agent reference) ---
    bad_data = dict(crew_data)
    bad_data["tasks"] = [
        {
            "name": "bad_task",
            "description": "This task references a non-existent agent.",
            "expected_output": "Irrelevant.",
            "agent": "ghost_agent",  # Not in agents list
        }
    ]
    valid, errors = validate_crew_json(bad_data)
    print(f"\n[CrewConfig INVALID]  Valid: {valid}  (expected False)")
    print(f"  Errors: {errors}")

    # --- Render template ---
    template = generate_empty_crew_template(["recon_agent", "exploit_agent"])
    print("\n[Empty Crew Template JSON]:")
    print(json.dumps(template, indent=2))

    # --- Render hierarchical crew ---
    hier_data = dict(crew_data)
    hier_data["process"] = "hierarchical"
    hier_data["manager_llm"] = "ollama/qwen2.5:7b"
    valid, errors = validate_crew_json(hier_data)
    print(f"\n[Hierarchical CrewConfig]  Valid: {valid}")
    if errors:
        print(f"  Errors: {errors}")

    print("\n=== Self-Test Complete ===")


if __name__ == "__main__":
    test_sample_schema()
