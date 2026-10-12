"""Buyer, participant and fulfillment identities, kept separate from bank facts."""

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from director.models import PlanningRecord


class OperationRecord(PlanningRecord):
    immutable_fields = ()

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        self._unchanged(self._old(), self.immutable_fields)


class Order(OperationRecord):
    reference = models.CharField(max_length=100)
    buyer = models.ForeignKey(
        "finance.Party", on_delete=models.PROTECT, related_name="operational_orders"
    )
    date = models.DateField()
    channel = models.CharField(max_length=30, blank=True)
    lead = models.OneToOneField(
        "marketing.Lead",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="operational_order",
    )
    version = models.PositiveIntegerField(default=1)
    scoped_references = ("buyer", "lead")
    immutable_fields = ("reference", "buyer_id", "date", "channel")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "reference"], name="operations_order_reference"
            )
        ]
        ordering = ["-date", "-pk"]

    def __str__(self):
        return self.reference


class OrderLine(OperationRecord):
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="lines")
    reference = models.CharField(max_length=100)
    product = models.ForeignKey("finance.Product", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(0)])
    invoice = models.OneToOneField(
        "finance.Invoice",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="order_line",
    )
    scoped_references = ("order", "product", "invoice")
    immutable_fields = ("order_id", "reference", "product_id")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["order", "reference"], name="operations_order_line_reference"
            )
        ]

    def clean(self):
        super().clean()
        old = self._old()
        if old and old.invoice_id:
            self._unchanged(old, ("invoice_id", "quantity"))


class Participant(OperationRecord):
    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    immutable_fields = ("name", "email", "phone")

    def __str__(self):
        return self.name


class TestSession(OperationRecord):
    reference = models.CharField(max_length=100)
    product = models.ForeignKey("finance.Product", on_delete=models.PROTECT)
    starts_at = models.DateTimeField()
    capacity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    version = models.PositiveIntegerField(default=1)
    scoped_references = ("product",)
    immutable_fields = ("reference", "product_id", "starts_at", "capacity")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "reference"],
                name="operations_session_reference",
            )
        ]
        ordering = ["starts_at", "pk"]

    def __str__(self):
        return self.reference


class CoursePlan(OperationRecord):
    line = models.OneToOneField(
        OrderLine, on_delete=models.PROTECT, related_name="course_plan"
    )
    total_sessions = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(200)]
    )
    version = models.PositiveIntegerField(default=1)
    scoped_references = ("line",)
    immutable_fields = ("line_id",)

    class Meta:
        ordering = ["pk"]


class CourseMeeting(OperationRecord):
    plan = models.ForeignKey(
        CoursePlan, on_delete=models.PROTECT, related_name="meetings"
    )
    number = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(200)]
    )
    starts_at = models.DateTimeField()
    version = models.PositiveIntegerField(default=1)
    scoped_references = ("plan",)
    immutable_fields = ("plan_id", "number")

    class Meta:
        ordering = ["number", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "number"], name="operations_course_meeting_number"
            )
        ]


class Registration(OperationRecord):
    class Status(models.TextChoices):
        REGISTERED = "registered", "Terdaftar"
        SCHEDULED = "scheduled", "Terjadwal"
        ATTENDED = "attended", "Hadir"
        ABSENT = "absent", "Tidak hadir"
        COMPLETED = "completed", "Layanan selesai"
        CANCELLED = "cancelled", "Dibatalkan"

    line = models.ForeignKey(
        OrderLine, on_delete=models.PROTECT, related_name="registrations"
    )
    participant = models.ForeignKey(Participant, on_delete=models.PROTECT)
    source = models.CharField(max_length=100)
    external_id = models.CharField(max_length=160)
    planned_date = models.DateField(null=True, blank=True)
    session = models.ForeignKey(
        TestSession,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="registrations",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.REGISTERED
    )
    version = models.PositiveIntegerField(default=1)
    delivery = models.OneToOneField(
        "finance.InvoiceDelivery", on_delete=models.PROTECT, null=True, blank=True
    )
    scoped_references = ("line", "participant", "session", "delivery")
    immutable_fields = (
        "line_id",
        "participant_id",
        "source",
        "external_id",
        "planned_date",
    )

    def clean(self):
        if not getattr(self, "_allow_participant_identity_correction", False):
            return super().clean()
        PlanningRecord.clean(self)
        self._unchanged(
            self._old(),
            tuple(
                field for field in self.immutable_fields if field != "participant_id"
            ),
        )

    def save(self, *args, **kwargs):
        try:
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_allow_participant_identity_correction", None)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source", "external_id"],
                name="operations_registration_source",
            )
        ]


class CourseAttendance(OperationRecord):
    class Status(models.TextChoices):
        ATTENDED = "attended", "Hadir"
        ABSENT = "absent", "Tidak hadir"

    registration = models.ForeignKey(
        Registration, on_delete=models.PROTECT, related_name="course_attendances"
    )
    meeting = models.ForeignKey(
        CourseMeeting, on_delete=models.PROTECT, related_name="attendance_records"
    )
    status = models.CharField(max_length=20, choices=Status.choices)
    evidence = models.TextField(max_length=2000)
    version = models.PositiveIntegerField(default=1)
    scoped_references = ("registration", "meeting")
    immutable_fields = ("registration_id", "meeting_id")

    class Meta:
        ordering = ["meeting__number", "registration_id", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["registration", "meeting"],
                name="operations_course_attendance_once",
            )
        ]


class OperationEvent(OperationRecord):
    object_type = models.CharField(max_length=40)
    object_id = models.PositiveIntegerField()
    action = models.CharField(max_length=60)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    reason = models.TextField(max_length=2000)

    def clean(self):
        super().clean()
        self._unchanged(
            self._old(),
            tuple(
                field.attname
                for field in self._meta.fields
                if field.name not in {"id", "created_at"}
            ),
        )


class IntakeBatch(OperationRecord):
    source = models.CharField(max_length=100)
    filename = models.CharField(max_length=200)
    digest = models.CharField(max_length=64)
    headers = models.JSONField()
    mapping = models.JSONField(default=dict, blank=True)
    version = models.PositiveIntegerField(default=1)
    immutable_fields = ("source", "filename", "digest", "headers")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source", "digest"],
                name="operations_intake_digest",
            )
        ]


class IntakeRow(OperationRecord):
    batch = models.ForeignKey(
        IntakeBatch, on_delete=models.PROTECT, related_name="rows"
    )
    row_number = models.PositiveIntegerField()
    raw = models.JSONField()
    normalized = models.JSONField(default=dict, blank=True)
    issues = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=20,
        default="pending",
        choices=[
            ("pending", "Perlu diperiksa"),
            ("accepted", "Diterima"),
            ("rejected", "Tidak digunakan"),
        ],
    )
    registration = models.ForeignKey(
        Registration, null=True, blank=True, on_delete=models.PROTECT
    )
    scoped_references = ("batch", "registration")
    immutable_fields = ("batch_id", "row_number", "raw")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "row_number"], name="operations_intake_row"
            )
        ]
        ordering = ["row_number"]

    def clean(self):
        super().clean()
        old = self._old()
        if old and old.status != "pending":
            self._unchanged(old, ("normalized", "issues", "status", "registration_id"))
