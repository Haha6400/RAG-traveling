from pydantic import BaseModel, Field

from travelrag.places.domain import Category


class ExtractedMention(BaseModel):
    name: str = Field(description="Place name as said or shown in the video")
    category: Category
    area: str | None = Field(description="District / city mentioned, e.g. 'Hoàn Kiếm, Hà Nội'")
    address_hint: str | None = Field(
        description="Street address as said or shown on screen, e.g. '39 Hàng Cót', or a "
        "landmark if no address is given"
    )
    price_min: int | None = Field(description="Lowest price mentioned, as an integer amount")
    price_max: int | None = Field(description="Highest price mentioned, as an integer amount")
    currency: str | None = Field(description="ISO currency code, e.g. VND, JPY")
    price_unit: str | None = Field(description="What the price is for, e.g. 'per bowl'")
    opening_hours_text: str | None = Field(description="Opening hours exactly as stated")
    opening_hours_osm: str | None = Field(
        description="The same hours in OpenStreetMap syntax, e.g. 'Mo-Su 06:00-22:00', "
        "or null if they cannot be expressed exactly"
    )
    best_time: str | None = Field(description="When the reviewer says it is best to go")
    pros: list[str] = Field(description="Strengths mentioned, short phrases")
    cons: list[str] = Field(description="Weaknesses mentioned, short phrases")
    recommendation: int | None = Field(
        description="How strongly the reviewer recommends it, 1 (avoid) to 5 (must go)"
    )
    chunk_numbers: list[int] = Field(description="Numbers of the chunks that talk about it")
    confidence: float = Field(description="0-1: how sure you are this is a real, named place")


class ExtractionOutput(BaseModel):
    mentions: list[ExtractedMention]
