"""Small validated planning vocabulary, never SQL or arbitrary tool execution."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field

MAX_ID = 2**31 - 1
Section = Literal["", "tickets", "board", "users", "templates", "articles", "categories",
                  "close_reasons", "teams", "settings", "ai_settings", "overview", "reports",
                  "ai", "integrations", "operators", "roles"]


class ToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: Literal["search_tickets", "analyze_tickets", "ticket", "workload", "section"]
    query: str = Field(default="", max_length=160)
    ticket_id: int | None = Field(default=None, ge=1, le=MAX_ID, strict=True)
    section: Section = ""
    status: Literal["active", "closed", "all"] = "active"
    stage: Literal["", "new", "in_progress", "waiting"] = ""
    channel: Literal["", "email", "slack", "telegram", "widget"] = ""
    unassigned: bool = False
    unanswered: bool = False
    days: int = Field(default=30, ge=1, le=90, strict=True)
    offset: int = Field(default=0, ge=0, le=10000, strict=True)
    team_id: int | None = Field(default=None, ge=1, le=MAX_ID, strict=True)
    ticket_ids: list[Annotated[int, Field(ge=1, le=MAX_ID, strict=True)]] = Field(default_factory=list, max_length=20)


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["stage", "assign", "close"]
    ticket_id: int = Field(ge=1, le=MAX_ID, strict=True)
    stage: Literal["", "new", "in_progress", "waiting"] = ""
    assignee: str = Field(default="", max_length=120)


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tools: list[ToolRequest] = Field(default_factory=list, max_length=3)
    action: ActionRequest | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=2000)
    context: str = Field(default="", max_length=200)
    # Previous user questions only. Tool results are always fetched afresh.
    previous_questions: list[Annotated[str, Field(max_length=2000)]] = Field(default_factory=list, max_length=3)
    ticket_id: int | None = Field(default=None, ge=1, le=MAX_ID, strict=True)
    conversation_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    request_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    attachment_ids: list[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]] = Field(default_factory=list, max_length=3)
