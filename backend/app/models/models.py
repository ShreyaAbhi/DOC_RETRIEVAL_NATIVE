from datetime import datetime
from sqlalchemy import (Column, String, Text, Boolean, Integer, Numeric,
                        DateTime, Date, Time, ForeignKey, JSON)
from sqlalchemy.orm import relationship, DeclarativeBase
import uuid


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id           = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email        = Column(String(200), unique=True, nullable=False, index=True)
    full_name    = Column(String(200))
    hashed_password = Column(String(200), nullable=False)
    role         = Column(String(50), nullable=False, default='reviewer')
    is_active    = Column(Boolean, default=True)
    created_at   = Column(DateTime, default=datetime.now)
    last_login   = Column(DateTime)
    created_by   = Column(String(200), default='system')


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    id         = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id    = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(64), unique=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used       = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)


class MaterialMaster(Base):
    __tablename__ = "material_master"
    id              = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    material_number = Column(String(50), unique=True, nullable=False)
    description     = Column(Text, nullable=False)
    unit_of_measure = Column(String(20), default="EA")
    created_at      = Column(DateTime, default=datetime.now)
    updated_at      = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    created_by      = Column(String(100), default="system")
    is_active       = Column(Boolean, default=True)


class Order(Base):
    __tablename__ = "orders"
    id                        = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    customer_order_number     = Column(String(100), nullable=False)
    my_delivery_number        = Column(String(100))
    warehouse_delivery_number = Column(String(100))
    sales_order_number        = Column(String(100))
    invoice_number            = Column(String(100))
    customer_name             = Column(String(200))
    customer_email            = Column(String(200))
    status                    = Column(String(50), default="active")
    created_at                = Column(DateTime, default=datetime.now)
    updated_at                = Column(DateTime, default=datetime.now)
    lines                     = relationship("OrderLine", back_populates="order", cascade="all, delete-orphan")


class OrderLine(Base):
    __tablename__ = "order_lines"
    id                   = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    order_id             = Column(String(36), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    line_number          = Column(Integer, nullable=False)
    material_id          = Column(String(36), ForeignKey("material_master.id"))
    material_number      = Column(String(50))
    material_description = Column(Text)
    lot_number           = Column(String(100))
    quantity             = Column(Numeric(18, 4))
    unit_of_measure      = Column(String(20))
    tracking_number      = Column(String(100))
    carrier              = Column(String(50))
    created_at           = Column(DateTime, default=datetime.now)
    updated_at           = Column(DateTime, default=datetime.now)
    order                = relationship("Order", back_populates="lines")


class PodDocument(Base):
    __tablename__ = "pod_documents"
    id                = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    order_id          = Column(String(36), ForeignKey("orders.id"))
    tracking_number   = Column(String(100))
    carrier           = Column(String(50), default="UPS")
    file_name         = Column(String(255), nullable=False)
    file_path         = Column(Text, nullable=False)
    file_hash         = Column(String(64))
    delivery_date     = Column(Date)
    delivery_time     = Column(Time)
    signed_by         = Column(String(200))
    delivery_location = Column(String(200))
    raw_api_response  = Column(JSON)
    source            = Column(String(50), default="ups_api")
    created_at        = Column(DateTime, default=datetime.now)


class PackingSlipDocument(Base):
    __tablename__ = "packing_slip_documents"
    id              = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    order_id        = Column(String(36), ForeignKey("orders.id"))
    delivery_number = Column(String(100))
    file_name       = Column(String(255), nullable=False)
    file_path       = Column(Text, nullable=False)
    file_hash       = Column(String(64))
    source          = Column(String(50), default="folder_scan")
    created_at      = Column(DateTime, default=datetime.now)


class InvoiceDocument(Base):
    __tablename__ = "invoice_documents"
    id             = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    order_id       = Column(String(36), ForeignKey("orders.id"))
    invoice_number = Column(String(100))
    file_name      = Column(String(255), nullable=False)
    file_path      = Column(Text, nullable=False)
    file_hash      = Column(String(64))
    source         = Column(String(50), default="folder_scan")
    created_at     = Column(DateTime, default=datetime.now)


class EmailRequest(Base):
    __tablename__ = "email_requests"
    id                       = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    reference_number         = Column(String(30), unique=True, nullable=False)
    from_email               = Column(String(200), nullable=False)
    from_name                = Column(String(200))
    subject                  = Column(Text, nullable=False)
    body                     = Column(Text, nullable=False)
    received_at              = Column(DateTime, default=datetime.now)
    status                   = Column(String(50), default='received')
    is_pod_request           = Column(Boolean)
    confidence_score         = Column(Numeric(5, 2))
    intent                   = Column(String(50))
    extracted_order_id       = Column(String(100))
    extracted_tracking       = Column(String(100))
    classification_raw       = Column(JSON)
    order_id                 = Column(String(36), ForeignKey("orders.id"))
    pod_document_id          = Column(String(36), ForeignKey("pod_documents.id"))
    packing_slip_document_id = Column(String(36), ForeignKey("packing_slip_documents.id"))
    invoice_document_id      = Column(String(36), ForeignKey("invoice_documents.id"))
    response_subject         = Column(Text)
    response_body            = Column(Text)
    response_sent_at         = Column(DateTime)
    requires_guidance        = Column(Boolean, default=False)
    guidance_reason          = Column(Text)
    completed_at             = Column(DateTime)
    error_message            = Column(Text)
    imap_message_id          = Column(String(500), unique=True, nullable=True)
    imap_in_reply_to         = Column(String(500), nullable=True)
    smtp_message_id          = Column(String(500), nullable=True)
    line_deletion_flag       = Column(Boolean, default=False)
    approval                 = relationship("ApprovalQueue", back_populates="request", uselist=False)
    guidance                 = relationship("GuidanceQueue", back_populates="request", uselist=False)
    audit_logs               = relationship("AuditLog", back_populates="request")


class ApprovalQueue(Base):
    __tablename__ = "approval_queue"
    id                      = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    request_id              = Column(String(36), ForeignKey("email_requests.id"), nullable=False)
    status                  = Column(String(50), default='pending')
    draft_subject           = Column(Text)
    draft_body              = Column(Text)
    draft_attachment        = Column(String(255))   # POD filename (primary order)
    packing_slip_attachment = Column(String(255))   # Packing slip filename (primary order)
    invoice_attachment      = Column(String(255))   # Invoice filename (primary order)
    attachments_json        = Column(JSON)           # All attachment filenames (multi-order)
    reviewed_by             = Column(String(100))
    reviewed_at             = Column(DateTime)
    reviewer_notes          = Column(Text)
    expires_at              = Column(DateTime)
    created_at              = Column(DateTime, default=datetime.now)
    line_deletion_flag      = Column(Boolean, default=False)
    request                 = relationship("EmailRequest", back_populates="approval")


class GuidanceQueue(Base):
    __tablename__ = "guidance_queue"
    id             = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    request_id     = Column(String(36), ForeignKey("email_requests.id"), nullable=False)
    status         = Column(String(50), default='pending')
    reason         = Column(Text, nullable=False)
    confidence     = Column(Numeric(5, 2))
    agent_question = Column(Text)
    human_guidance = Column(Text)
    provided_by    = Column(String(100))
    provided_at    = Column(DateTime)
    created_at         = Column(DateTime, default=datetime.now)
    line_deletion_flag = Column(Boolean, default=False)
    request            = relationship("EmailRequest", back_populates="guidance")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    request_id   = Column(String(36), ForeignKey("email_requests.id"))
    action       = Column(String(50), nullable=False)
    actor        = Column(String(100), default="system")
    summary      = Column(Text, nullable=False)
    detail       = Column(JSON)
    duration_ms  = Column(Integer)
    success      = Column(Boolean, default=True)
    error_detail = Column(Text)
    ip_address   = Column(String(45))
    created_at   = Column(DateTime, default=datetime.now)
    request      = relationship("EmailRequest", back_populates="audit_logs")


class MonitoredEmail(Base):
    __tablename__ = "monitored_emails"
    id                     = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email                  = Column(String(200), unique=True, nullable=False, index=True)
    display_name           = Column(String(200))
    status                 = Column(String(50), default='pending')  # pending, active, disabled, error, reauth_required
    notes                  = Column(Text)
    # Invite token
    setup_token            = Column(String(100), unique=True, index=True)
    token_expires_at       = Column(DateTime)
    # IMAP settings (filled by the email owner via the setup link)
    imap_host              = Column(String(200))
    imap_port              = Column(Integer, default=993)
    imap_user              = Column(String(200))
    imap_password          = Column(Text)   # Fernet-encrypted
    use_ssl                = Column(Boolean, default=True)
    mailbox_folder         = Column(String(100), default='INBOX')
    check_interval_minutes = Column(Integer, default=5)
    # Authentication mode: 'password' (IMAP basic auth) or 'oauth_microsoft'
    auth_type              = Column(String(30), default='password')
    oauth_access_token     = Column(Text)   # Fernet-encrypted
    oauth_refresh_token    = Column(Text)   # Fernet-encrypted
    oauth_token_expires_at = Column(DateTime)
    oauth_scope            = Column(Text)
    last_reauth_reminder_at = Column(DateTime)
    # Meta
    created_at             = Column(DateTime, default=datetime.now)
    configured_at          = Column(DateTime)
    created_by             = Column(String(200), default='system')
    last_checked_at        = Column(DateTime)
    last_error             = Column(Text)


class OAuthPendingState(Base):
    """Short-lived state used to correlate an OAuth2 authorization redirect
    with the monitored-email setup token that initiated it (CSRF protection)."""
    __tablename__ = "oauth_pending_states"
    state               = Column(String(100), primary_key=True)
    provider            = Column(String(30), nullable=False)   # 'microsoft'
    setup_token         = Column(String(100), nullable=False)
    monitored_email_id  = Column(String(36))
    created_at          = Column(DateTime, default=datetime.now)
    expires_at          = Column(DateTime, nullable=False)


class SystemConfig(Base):
    __tablename__ = "system_config"
    key         = Column(String(100), primary_key=True)
    value       = Column(Text)
    description = Column(Text)
    updated_at  = Column(DateTime, default=datetime.now)
    updated_by  = Column(String(100), default="system")


class Carrier(Base):
    __tablename__ = "carriers"
    id                = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name              = Column(String(200), nullable=False)
    email             = Column(String(200))
    sends_proactively = Column(Boolean, default=False)
    uses_ftp          = Column(Boolean, default=False)
    ftp_subfolder     = Column(String(500))
    notes             = Column(Text)
    is_active         = Column(Boolean, default=True)
    created_at        = Column(DateTime, default=datetime.now)
    created_by        = Column(String(200), default='system')
    pod_registries    = relationship("PodRegistry", back_populates="carrier")


class ApiKey(Base):
    __tablename__ = "api_keys"
    id           = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    key_hash     = Column(String(64), unique=True, nullable=False)
    key_prefix   = Column(String(12), nullable=False)
    name         = Column(String(200), nullable=False)
    created_by   = Column(String(200))
    is_active    = Column(Boolean, default=True)
    last_used_at = Column(DateTime)
    created_at   = Column(DateTime, default=datetime.now)


class PodRegistry(Base):
    __tablename__ = "pod_registry"
    id                       = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    delivery_number          = Column(String(200), nullable=False, unique=True, index=True)
    customer_po              = Column(String(200))
    order_id                 = Column(String(36), ForeignKey("orders.id"))
    carrier_id               = Column(String(36), ForeignKey("carriers.id"))
    status                   = Column(String(50), default='pending')
    filename                 = Column(String(500))
    pod_folder_path          = Column(Text)
    requested_at             = Column(DateTime)
    request_email_message_id = Column(Text)
    received_at              = Column(DateTime)
    received_via             = Column(String(50))
    matched_by               = Column(String(50))
    notes                    = Column(Text)
    is_deleted               = Column(Boolean, default=False, nullable=False, server_default='0')
    deleted_at               = Column(DateTime)
    created_at               = Column(DateTime, default=datetime.now)
    carrier                  = relationship("Carrier", back_populates="pod_registries")
    order                    = relationship("Order")
