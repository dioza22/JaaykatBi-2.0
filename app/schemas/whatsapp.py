"""Pydantic models for the Meta WhatsApp Cloud API webhook payload.

Shape: WebhookPayload -> entry[] -> changes[] -> value{metadata, contacts[],
messages[], statuses[]}. Only the fields JaaykatBi actually reads are typed;
everything else is ignored (`model_config = extra="ignore"`).
"""

from pydantic import BaseModel, ConfigDict, Field


class Profile(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str | None = None


class WAContact(BaseModel):
    model_config = ConfigDict(extra="ignore")
    wa_id: str
    profile: Profile | None = None


class TextContent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    body: str


class InteractiveReply(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str | None = None
    title: str | None = None


class InteractiveContent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str | None = None
    button_reply: InteractiveReply | None = None
    list_reply: InteractiveReply | None = None


class ImageContent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str | None = None
    caption: str | None = None


class LocationContent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    latitude: float | None = None
    longitude: float | None = None
    name: str | None = None
    address: str | None = None


class WAMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    from_: str = Field(alias="from")
    timestamp: str
    type: str
    text: TextContent | None = None
    interactive: InteractiveContent | None = None
    image: ImageContent | None = None
    location: LocationContent | None = None


class WAStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    status: str
    timestamp: str
    recipient_id: str


class Metadata(BaseModel):
    model_config = ConfigDict(extra="ignore")
    display_phone_number: str
    phone_number_id: str


class WAValue(BaseModel):
    """`value`'s shape depends on `WAChange.field` — a `messages` change has
    metadata/contacts/messages/statuses, but Meta also sends webhooks for
    other subscribed fields (account alerts, template status, business
    capability updates, etc.) with a completely different shape. Everything
    here is optional so those don't fail validation for the whole payload —
    webhook_parser.py filters to `field == "messages"` and skips anything
    without metadata."""

    model_config = ConfigDict(extra="ignore")
    messaging_product: str | None = None
    metadata: Metadata | None = None
    contacts: list[WAContact] = Field(default_factory=list)
    messages: list[WAMessage] = Field(default_factory=list)
    statuses: list[WAStatus] = Field(default_factory=list)


class WAChange(BaseModel):
    model_config = ConfigDict(extra="ignore")
    field: str
    value: WAValue


class WAEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    changes: list[WAChange] = Field(default_factory=list)


class WhatsAppWebhookPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    object: str
    entry: list[WAEntry] = Field(default_factory=list)


class InboundMessage(BaseModel):
    """Flattened, business-relevant view of one inbound WhatsApp message —
    what webhook_parser.py hands to the conversation engine."""

    business_whatsapp_number: str
    phone_number_id: str
    wa_id: str
    contact_name: str | None
    whatsapp_message_id: str
    message_type: str
    text: str
