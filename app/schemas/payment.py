from pydantic import BaseModel, ConfigDict
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional
from app.models.enums import PaymentMethod, PaymentType, PaymentStatus, InvoiceStatus, InvoiceType

class PaymentBase(BaseModel):
    amount: Decimal
    discount_amount: Decimal = Decimal("0")
    payment_method: PaymentMethod
    payment_type: PaymentType
    notes: Optional[str] = None

class PaymentCreate(PaymentBase):
    member_id: uuid.UUID
    subscription_id: Optional[uuid.UUID] = None

class PaymentResponse(PaymentBase):
    id: uuid.UUID
    gym_id: uuid.UUID
    status: PaymentStatus
    payment_date: datetime
    transaction_reference: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

class InvoiceResponse(BaseModel):
    id: uuid.UUID
    invoice_number: str
    subtotal: Decimal
    discount_amount: Decimal
    tax_amount: Decimal
    tax_rate: Decimal
    total_amount: Decimal
    invoice_type: InvoiceType
    status: InvoiceStatus
    pdf_url: Optional[str] = None
    issued_at: datetime
    model_config = ConfigDict(from_attributes=True)
