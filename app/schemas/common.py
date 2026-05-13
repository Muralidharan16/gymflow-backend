from pydantic import BaseModel, ConfigDict
from typing import TypeVar, Generic, Any

T = TypeVar("T")

class Response(BaseModel, Generic[T]):
    status: str = "success"
    data: T | None = None
    message: str = ""
    model_config = ConfigDict(from_attributes=True)

class ErrorResponse(BaseModel):
    success: bool = False
    error: str
    message: str
    model_config = ConfigDict(from_attributes=True)

class PaginatedResponse(BaseModel, Generic[T]):
    success: bool = True
    data: list[T]
    total: int
    page: int
    size: int
    pages: int
    model_config = ConfigDict(from_attributes=True)

class MessageResponse(BaseModel):
    message: str
    model_config = ConfigDict(from_attributes=True)
