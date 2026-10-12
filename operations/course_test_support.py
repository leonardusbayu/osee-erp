from django.contrib.auth import get_user_model
from django.utils import timezone

from core.models import Membership, Organization
from finance.models import Party, Product
from operations import course_services, services


class CourseTestSupport:
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic course operations")
        self.actor = get_user_model().objects.create_user(username="course-ops-finance")
        Membership.objects.create(
            organization=self.org, user=self.actor, role="finance"
        )
        self.buyer = Party.objects.create(
            organization=self.org, name="Synthetic course buyer", kind="customer"
        )
        self.course = Product.objects.create(
            organization=self.org,
            code="COURSE",
            name="English course",
            kind="course",
            default_price="1200000",
        )
        self.itp = Product.objects.create(
            organization=self.org,
            code="ITP",
            name="TOEFL ITP",
            kind="itp",
            default_price="510000",
        )
        self.order = services.create_order(
            organization=self.org,
            actor=self.actor,
            reference="COURSE-ORDER-1",
            buyer=self.buyer,
            date=timezone.localdate(),
        )
        self.line = services.add_order_line(
            organization=self.org,
            actor=self.actor,
            order=self.order,
            product=self.course,
            quantity=2,
        )
        self.first = services.register_participant(
            organization=self.org,
            actor=self.actor,
            line=self.line,
            name="Synthetic learner one",
            source="test",
            external_id="COURSE-R-1",
        )
        self.second = services.register_participant(
            organization=self.org,
            actor=self.actor,
            line=self.line,
            name="Synthetic learner two",
            source="test",
            external_id="COURSE-R-2",
        )

    def record_course_attendance(self, **values):
        command_values = {
            name: values.pop(name)
            for name in ("expected_version", "attended", "evidence")
        }
        return course_services.record_course_attendance(
            **values,
            command=course_services.CourseAttendanceCommand(**command_values),
        )
