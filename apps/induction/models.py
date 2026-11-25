import os
import uuid
import secrets
from decimal import Decimal

from django.db import models
from django.db.models import Count, Max, Q, Case, When, Value, CharField
from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils.text import slugify
from django.urls import reverse

User = get_user_model()


# ---------------------------------------------------------------------
# 1. MASTERS
# ---------------------------------------------------------------------
class UserCategory(models.Model):
    """
    Fixed category of the person scanning the QR: employee, contractor, visitor.
    Per-language display names live in UserCategoryTranslation.
    """
    class Slug(models.TextChoices):
        EMPLOYEE   = 'employee',   'On-roll Employee'
        CONTRACTOR = 'contractor', 'Contractor / Off-roll'
        VISITOR    = 'visitor',    'Visitor'

    name = models.CharField(max_length=50, blank=True)  # <--- ADDED THIS
    slug = models.CharField(
        max_length=20,
        choices=Slug.choices,
        unique=True,
        default=Slug.EMPLOYEE,
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "User Category"
        verbose_name_plural = "User Categories"
        ordering = ['id']

    def save(self, *args, **kwargs):
        if self.slug:
            self.slug = self.slug.lower()
        # Auto-fill name if it is empty
        if not self.name and self.slug:
            self.name = self.get_slug_display()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name or self.get_slug_display()


class UserCategoryTranslation(models.Model):
    """
    Translated name of a UserCategory for the QR page.
    """
    category = models.ForeignKey(UserCategory, on_delete=models.CASCADE, related_name='translations')
    language = models.ForeignKey('Language', on_delete=models.CASCADE)
    name = models.CharField(max_length=50)

    class Meta:
        unique_together = ('category', 'language')
        verbose_name = "User Category Translation"
        verbose_name_plural = "User Category Translations"

    def __str__(self):
        return f"{self.category.name} [{self.language.code}]"


class Language(models.Model):
    """
    Languages available for videos and the QR page.
    Keep rows in sync with settings.LANGUAGES.
    """
    code = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=50)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------
# 2. QUESTION BANK
# ---------------------------------------------------------------------
class InductionQuestion(models.Model):
    question_text = models.CharField(max_length=500)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['id']

    @property
    def code(self):
        return f"Q-{self.id:03d}"

    @property
    def correct_option(self):
        return self.options.filter(is_correct=True).first()

    def __str__(self):
        return f"{self.code} - {self.question_text}"


class InductionQuestionOption(models.Model):
    question = models.ForeignKey(InductionQuestion, on_delete=models.CASCADE, related_name='options')
    option_text = models.CharField(max_length=300)
    is_correct = models.BooleanField(default=False)
    sort_order = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ['sort_order', 'id']
        unique_together = ('question', 'sort_order')
        constraints = [
            models.UniqueConstraint(
                fields=['question'],
                condition=Q(is_correct=True),
                name='uq_one_correct_option_per_question',
            )
        ]

    def __str__(self):
        return f"{self.question.code} - {self.option_text}"


class InductionQuestionTranslation(models.Model):
    question = models.ForeignKey(InductionQuestion, on_delete=models.CASCADE, related_name='translations')
    language = models.ForeignKey(Language, on_delete=models.CASCADE)
    question_text = models.CharField(max_length=500)

    class Meta:
        unique_together = ('question', 'language')
        verbose_name = "Question Translation"
        verbose_name_plural = "Question Translations"

    def __str__(self):
        return f"{self.question.code} [{self.language.code}]"


class InductionOptionTranslation(models.Model):
    option = models.ForeignKey(InductionQuestionOption, on_delete=models.CASCADE, related_name='translations')
    language = models.ForeignKey(Language, on_delete=models.CASCADE)
    option_text = models.CharField(max_length=300)

    class Meta:
        unique_together = ('option', 'language')
        verbose_name = "Option Translation"
        verbose_name_plural = "Option Translations"

    def __str__(self):
        return f"{self.option_id} [{self.language.code}]"


# ---------------------------------------------------------------------
# 3. VIDEOS  (one per language, no plant)
# ---------------------------------------------------------------------
def video_upload_path(instance, filename):
    """
    induction/videos/<lang_code>/Safety-Induction-Video-<lang_code><ext>
    Uses lang_code (ASCII) so Devanagari names don't collapse to "language".
    """
    ext = os.path.splitext(filename)[1].lower() or '.mp4'
    lang_code = slugify(instance.language.code) or 'xx'
    return f"induction/videos/{lang_code}/Safety-Induction-Video-{lang_code}{ext}"


class InductionVideo(models.Model):
    language = models.ForeignKey(Language, on_delete=models.PROTECT, related_name='videos')
    file = models.FileField(upload_to=video_upload_path, max_length=255)
    file_name = models.CharField(max_length=255, blank=True)
    file_size = models.BigIntegerField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['language__id', '-uploaded_at']
        constraints = [
            models.UniqueConstraint(fields=['language'], name='uq_one_video_per_language'),
        ]

    @property
    def title(self):
        return f"Safety Induction Video - {self.language.name}"

    def save(self, *args, **kwargs):
        if self.pk:
            old = InductionVideo.objects.filter(pk=self.pk).first()
            if old and old.file and self.file and old.file.name != self.file.name:
                old.file.delete(save=False)

        if self.language_id:
            ext = os.path.splitext(self.file.name)[1].lower() if self.file else '.mp4'
            lang_code = self.language.code or 'xx'
            self.file_name = f"Safety-Induction-Video-{lang_code}{ext}"

        if self.file and not self.file_size:
            try:
                self.file_size = self.file.size
            except Exception:
                pass

        super().save(*args, **kwargs)

    def __str__(self):
        return self.title


# ---------------------------------------------------------------------
# 4. ASSIGNMENT
# ---------------------------------------------------------------------
class Questionnaire(models.Model):
    SCORING_CHOICES = [
        ('AUTO', 'Automatic scoring'),
        ('MANUAL', 'Manual scoring'),
    ]

    scoring_type = models.CharField(max_length=10, choices=SCORING_CHOICES, default='AUTO')
    total_marks = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('100'))
    passing_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('80'),
        validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    plants = models.ManyToManyField(
        'organizations.Plant', through='QuestionnairePlant', related_name='induction_questionnaires'
    )
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def apply_auto_marks(self):
        items = list(self.items.all())
        if items:
            each = (self.total_marks / len(items)).quantize(Decimal('0.01'))
            for item in items:
                item.marks = each
                item.save(update_fields=['marks'])

    def manual_marks_valid(self):
        total = sum((i.marks for i in self.items.all()), Decimal('0'))
        return total == self.total_marks

    def deactivate(self):
        """
        Make inactive AND release the plants, so they can be assigned again.
        Called from the delete view when protected by existing attempts.
        """
        self.plant_links.all().delete()
        self.is_active = False
        self.save(update_fields=['is_active'])

    def __str__(self):
        return f"Questionnaire #{self.id} ({self.get_scoring_type_display()})"


class QuestionnairePlant(models.Model):
    questionnaire = models.ForeignKey(Questionnaire, on_delete=models.CASCADE, related_name='plant_links')
    plant = models.ForeignKey('organizations.Plant', on_delete=models.CASCADE, related_name='+')

    class Meta:
        unique_together = ('questionnaire', 'plant')
        constraints = [
            models.UniqueConstraint(fields=['plant'], name='uq_one_questionnaire_per_plant')
        ]


class QuestionnaireQuestion(models.Model):
    questionnaire = models.ForeignKey(Questionnaire, on_delete=models.CASCADE, related_name='items')
    question = models.ForeignKey(InductionQuestion, on_delete=models.PROTECT, related_name='+')
    sequence_no = models.PositiveSmallIntegerField()
    marks = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0'))

    class Meta:
        ordering = ['sequence_no']
        unique_together = [('questionnaire', 'question'), ('questionnaire', 'sequence_no')]

    def __str__(self):
        return f"{self.sequence_no}. {self.question.code} ({self.marks})"


# ---------------------------------------------------------------------
# 5. QR CODES
# ---------------------------------------------------------------------
def generate_qr_token():
    return secrets.token_urlsafe(24)


class PlantQRCode(models.Model):
    plant = models.ForeignKey('organizations.Plant', on_delete=models.CASCADE, related_name='induction_qr_codes')
    token = models.CharField(max_length=64, unique=True, default=generate_qr_token)
    is_active = models.BooleanField(default=True)
    generated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    generated_at = models.DateTimeField(auto_now_add=True)
    last_downloaded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-generated_at']
        constraints = [
            models.UniqueConstraint(
                fields=['plant'], condition=Q(is_active=True), name='uq_active_qr_per_plant'
            )
        ]

    @property
    def qr_url(self):
        return reverse('induction:portal', args=[self.token])

    def __str__(self):
        return f"QR - {self.plant}"


# ---------------------------------------------------------------------
# 6. QR SUBMISSIONS
# ---------------------------------------------------------------------
class InductionAttemptQuerySet(models.QuerySet):
    def scorecard(self):
        return (
            self.filter(status='SUBMITTED')
                .values('user_category_id', 'user_category__slug',
                        'plant_id', 'plant__name')
                .annotate(
                    best_marks=Max('obtained_marks'),
                    total_marks_max=Max('total_marks'),
                    best_percentage=Max('score_percentage'),
                    attempts=Count('id'),
                    last_attempt=Max('submitted_at'),
                    passed_count=Count('id', filter=Q(result='PASS')),
                )
                .annotate(
                    result_final=Case(
                        When(passed_count__gt=0, then=Value('PASS')),
                        default=Value('FAIL'),
                        output_field=CharField(max_length=10),
                    )
                )
                .order_by('user_category__slug', 'plant__name')
        )


class InductionAttempt(models.Model):
    RESULT_CHOICES = [('PASS', 'Passed'), ('FAIL', 'Failed')]
    STATUS_CHOICES = [
        ('IN_PROGRESS', 'In progress'),
        ('SUBMITTED', 'Submitted'),
        ('ABANDONED', 'Abandoned'),
    ]

    attempt_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    plant = models.ForeignKey('organizations.Plant', on_delete=models.CASCADE, related_name='induction_attempts')
    qr_code = models.ForeignKey(PlantQRCode, on_delete=models.SET_NULL, null=True, blank=True, related_name='attempts')
    questionnaire = models.ForeignKey(Questionnaire, on_delete=models.PROTECT, related_name='attempts')
    user_category = models.ForeignKey(UserCategory, on_delete=models.PROTECT, related_name='attempts')
    language = models.ForeignKey(Language, on_delete=models.PROTECT, related_name='+')
    video = models.ForeignKey(InductionVideo, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    # Common identity
    participant_name   = models.CharField(max_length=150, blank=True)
    participant_mobile = models.CharField(max_length=20, blank=True)
    participant_email  = models.EmailField(blank=True)

    # Visitor
    visitor_company        = models.CharField(max_length=150, blank=True)
    visitor_purpose        = models.CharField(max_length=255, blank=True)
    visitor_person_to_meet = models.CharField(max_length=150, blank=True)

    # Contractor
    contractor_company = models.CharField(max_length=150, blank=True)
    contractor_id      = models.CharField(max_length=50, blank=True)

    # Employee
    employee_id = models.CharField(max_length=50, blank=True)
    department  = models.CharField(max_length=100, blank=True)

    # Misc
    participant_ref = models.CharField(max_length=50, blank=True)
    device_token    = models.CharField(max_length=64, blank=True)

    attempt_no = models.PositiveIntegerField(default=1)
    video_completed = models.BooleanField(default=False)
    started_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    total_marks = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    obtained_marks = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    score_percentage = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    passing_percentage = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    result = models.CharField(max_length=10, choices=RESULT_CHOICES, blank=True)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='IN_PROGRESS')

    objects = InductionAttemptQuerySet.as_manager()

    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['user_category', 'plant', 'submitted_at']),
            models.Index(fields=['plant', 'participant_mobile', 'device_token']),
            models.Index(fields=['employee_id']),
            models.Index(fields=['contractor_id']),
        ]

    def save(self, *args, **kwargs):
        if not self.pk:
            # Count only SUBMITTED attempts of the same person at this plant,
            # matched by the strongest identifier available.
            prev = InductionAttempt.objects.filter(plant=self.plant, status='SUBMITTED')
            for field in ('employee_id', 'contractor_id', 'participant_mobile', 'device_token'):
                value = getattr(self, field)
                if value:
                    prev = prev.filter(**{field: value})
                    break
            else:
                prev = prev.none()
            self.attempt_no = prev.count() + 1
        super().save(*args, **kwargs)

    def finalize(self):
        from django.utils import timezone
        answers = self.answers.all()
        total = sum((a.max_marks for a in answers), Decimal('0'))
        got = sum((a.marks_awarded for a in answers), Decimal('0'))
        pct = (got / total * 100).quantize(Decimal('0.01')) if total else Decimal('0')
        self.total_marks = total
        self.obtained_marks = got
        self.score_percentage = pct
        self.passing_percentage = self.questionnaire.passing_percentage
        self.result = 'PASS' if pct >= self.passing_percentage else 'FAIL'
        self.status = 'SUBMITTED'
        self.submitted_at = timezone.now()
        self.save()

    def __str__(self):
        return f"{self.plant} - {self.user_category} - attempt {self.attempt_no} ({self.result or self.status})"


class InductionAttemptAnswer(models.Model):
    attempt = models.ForeignKey(InductionAttempt, on_delete=models.CASCADE, related_name='answers')
    question = models.ForeignKey(InductionQuestion, on_delete=models.PROTECT, related_name='+')
    sequence_no = models.PositiveSmallIntegerField()
    selected_option = models.ForeignKey(
        InductionQuestionOption, on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )
    is_correct = models.BooleanField(default=False)
    max_marks = models.DecimalField(max_digits=6, decimal_places=2)
    marks_awarded = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0'))

    class Meta:
        ordering = ['sequence_no']
        unique_together = ('attempt', 'question')

    def save(self, *args, **kwargs):
        # Defensive check: the selected option must belong to this question.
        if self.selected_option and self.selected_option.question_id != self.question_id:
            self.selected_option = None
        self.is_correct = bool(self.selected_option and self.selected_option.is_correct)
        self.marks_awarded = self.max_marks if self.is_correct else Decimal('0')
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.attempt_id} - {self.question.code} - {'correct' if self.is_correct else 'wrong'}"