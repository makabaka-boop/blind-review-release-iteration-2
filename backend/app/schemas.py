from pydantic import BaseModel, Field, field_validator


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class CreateRoundIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class CreateReviewerIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class ManuscriptIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    author: str = Field(min_length=1, max_length=200)


class ManuscriptsIn(BaseModel):
    manuscripts: list[ManuscriptIn] = Field(min_length=1)


class ReviewerIdsIn(BaseModel):
    reviewer_ids: list[int] = Field(min_length=1)


class ConflictIn(BaseModel):
    manuscript_id: int
    reviewer_id: int


class AssignmentIn(BaseModel):
    manuscript_id: int
    reviewer_id: int


class DraftIn(BaseModel):
    content: str = Field(max_length=100_000)
    expected_revision: int = Field(ge=0)


class RecusalIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value.strip()


class RecusalDecisionIn(BaseModel):
    # Present on approval: the reviewer taking over the manuscript.
    replacement_reviewer_id: int | None = None
