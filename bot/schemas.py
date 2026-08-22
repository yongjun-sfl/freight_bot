from pydantic import BaseModel, Field
from typing import Optional

class TripLogExtraction(BaseModel):
    origin: Optional[str] = Field(None, description="Starting city.")
    destination: Optional[str] = Field(None, description="Ending city.")
    time_info: Optional[str] = Field(None, description="Time or date markers.")

class BolVisionExtraction(BaseModel):
    bol_number: Optional[str] = Field(None, description="BOL reference number string.")
    trailer_number: Optional[str] = Field(None, description="Trailer container identification code.")
    shipper_signed: bool = Field(False, description="True if a signature or stamp is inside shipper box.")
    receiver_signed: bool = Field(False, description="True if a signature or stamp is inside receiver box.")
