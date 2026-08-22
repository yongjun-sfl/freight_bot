from pydantic import BaseModel, Field
from typing import Optional

class TripLogExtraction(BaseModel):
    origin: Optional[str] = Field(None, description="Starting city.")
    destination: Optional[str] = Field(None, description="Ending city.")
    time_info: Optional[str] = Field(None, description="Time markers.")

class BolVisionExtraction(BaseModel):
    bol_number: Optional[str] = Field(None, description="BOL ID reference number.")
    trailer_number: Optional[str] = Field(None, description="Trailer container code.")
    shipper_signed: bool = Field(False, description="True if a signature/stamp is visible in shipper box.")
    receiver_signed: bool = Field(False, description="True if a signature/stamp is visible in receiver box.")
