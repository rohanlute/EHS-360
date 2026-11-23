import json
import subprocess
from decimal import Decimal, ROUND_DOWN

from django import forms
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import Q
from django.forms import formset_factory
from django.utils.text import slugify

from apps.organizations.models import Plant
from .languages import portal_languages          # NOT from .views (that is a circular import)
from .models import (
    InductionQuestion, InductionQuestionOption, InductionVideo,
    Questionnaire, QuestionnairePlant, QuestionnaireQuestion,
    UserCategory, Language,
)

MAX_VIDEO_MB = 500
ALLOWED_VIDEO_EXT = ('.mp4', '.webm', '.mov', '.mkv', '.avi')


# =====================================================================
# PARTICIPANT FIELD SCHEMA
# ---------------------------------------------------------------------
# Single source of truth: maps UserCategory.slug -> list of
# (field_name, label, required) tuples.
#
# Consumed by:
#   - views._categories()          -> what fields the portal renders
#   - InductionStartForm.clean()   -> which fields are required
#   - InductionStartForm.participant_data() -> what gets saved
#
# field_name MUST match a field on InductionAttempt.
# =====================================================================
PARTICIPANT_FIELDS = {
    'visitor': [
        ('participant_name',       'Full name',        True),
        ('participant_mobile',     'Mobile',           True),
        ('participant_email',      'Email',            False),
        ('visitor_company',        'Company name',     False),
        ('visitor_purpose',        'Purpose of visit', True),
        ('visitor_person_to_meet', 'Person to meet',   True),
    ],
    'contractor': [
        ('participant_name',   'Full name',     True),
        ('participant_mobile', 'Mobile',        True),
        ('participant_email',  'Email',         False),
        ('contractor_company', 'Company name',  True),
        ('contractor_id',      'Contractor ID', False),
        ('department',         'Department',    True),
    ],
    'employee': [
        ('employee_id',        'Employee ID', True),
        ('participant_name',   'Full name',   True),
        ('participant_email',  'Email',       True),
        ('participant_mobile', 'Mobile',      True),
        ('department',         'Department',  True),
    ],
}


# ---------------------------------------------------------------------
# 1. QUESTION FORM  (question + 4 options + one correct option)
# ---------------------------------------------------------------------
class QuestionForm(forms.ModelForm):
    option_1 = forms.CharField(max_length=300, label="Option 1")
    option_2 = forms.CharField(max_length=300, label="Option 2")
    option_3 = forms.CharField(max_length=300, label="Option 3")
    option_4 = forms.CharField(max_length=300, label="Option 4")
    correct = forms.ChoiceField(
        choices=[('1', 'Option 1'), ('2', 'Option 2'), ('3', 'Option 3'), ('4', 'Option 4')],
        widget=forms.RadioSelect,
        label="Correct option",
        error_messages={'required': 'Select the correct option.'},
    )

    class Meta:
        model = InductionQuestion
        fields = ['question_text', 'is_active']
        widgets = {
            'question_text': forms.TextInput(attrs={'placeholder': 'Type the question'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            for opt in self.instance.options.all():
                if 1 <= opt.sort_order <= 4:
                    self.fields[f'option_{opt.sort_order}'].initial = opt.option_text
                    if opt.is_correct:
                        self.fields['correct'].initial = str(opt.sort_order)

    def clean_question_text(self):
        return self.cleaned_data['question_text'].strip()

    def clean(self):
        cleaned = super().clean()
        options = [(cleaned.get(f'option_{i}') or '').strip() for i in range(1, 5)]
        if all(options):
            lowered = [o.lower() for o in options]
            if len(set(lowered)) != 4:
                raise forms.ValidationError("All 4 options must be different.")
        return cleaned

    @transaction.atomic
    def save(self, commit=True, user=None):
        question = super().save(commit=False)
        if not question.pk and user:
            question.created_by = user
        question.save()

        texts = [self.cleaned_data[f'option_{i}'].strip() for i in range(1, 5)]
        correct = int(self.cleaned_data['correct'])
        existing = {o.sort_order: o for o in question.options.all()}

        question.options.update(is_correct=False)
        for i, text in enumerate(texts, start=1):
            opt = existing.get(i) or InductionQuestionOption(question=question, sort_order=i)
            opt.option_text = text
            opt.is_correct = (i == correct)
            opt.save()
        return question


QuestionFormSet = formset_factory(
    QuestionForm, extra=1, min_num=1, validate_min=True, can_delete=False
)


# ---------------------------------------------------------------------
# 2. VIDEO FORM  (no plant — video is global, one per language)
# ---------------------------------------------------------------------
def probe_duration(path):
    """Read video duration (seconds) with ffprobe. Returns None if ffprobe is not installed."""
    try:
        out = subprocess.check_output(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', path],
            timeout=30,
        )
        return int(float(out.strip()))
    except Exception:
        return None


class InductionVideoForm(forms.ModelForm):
    # filled by JS on the client (video.onloadedmetadata) - server falls back to ffprobe
    duration_seconds = forms.IntegerField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = InductionVideo
        fields = ['language', 'file', 'duration_seconds']
        widgets = {'file': forms.ClearableFileInput(attrs={'accept': 'video/*'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # languages that already have a video are NOT offered...
        used = InductionVideo.objects.values_list('language_id', flat=True)
        if self.instance.pk:
            used = used.exclude(pk=self.instance.pk)      # ...except the one being edited
        used_ids = [i for i in used if i is not None]

        # (these lines must sit OUTSIDE the `if` so they also run on "Add")
        self.fields['language'].queryset = portal_languages().exclude(pk__in=used_ids)
        self.fields['language'].empty_label = "Select language"
        self.fields['language'].error_messages['invalid_choice'] = \
            "A video already exists for this language."

        self.fields['file'].required = not self.instance.pk   # optional when editing

    def clean_file(self):
        f = self.cleaned_data.get('file')
        if isinstance(f, UploadedFile):      # a new upload (not the existing file)
            if not f.name.lower().endswith(ALLOWED_VIDEO_EXT):
                raise forms.ValidationError(
                    "Upload a video file (%s)." % ", ".join(ALLOWED_VIDEO_EXT))
            if f.size > MAX_VIDEO_MB * 1024 * 1024:
                raise forms.ValidationError(f"Video must be smaller than {MAX_VIDEO_MB} MB.")
        return f

    def save(self, commit=True, user=None):
        video = super().save(commit=False)
        if user and not video.pk:
            video.uploaded_by = user
        video.save()                        # model.save() sets file_name + file_size
        if not video.duration_seconds:
            video.duration_seconds = probe_duration(video.file.path)
            if video.duration_seconds:
                video.save(update_fields=['duration_seconds'])
        return video


# ---------------------------------------------------------------------
# 3. QUESTIONNAIRE FORM  (Assign Que)
#    plants + questions + drag & drop order + auto/manual marks + pass %
# ---------------------------------------------------------------------
class QuestionnaireForm(forms.ModelForm):
    plants = forms.ModelMultipleChoiceField(
        queryset=Plant.objects.all(),
        widget=forms.CheckboxSelectMultiple,
        error_messages={'required': 'Select at least one plant.'},
    )
    questions = forms.ModelMultipleChoiceField(
        queryset=InductionQuestion.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        error_messages={'required': 'Select at least one question.'},
    )
    question_order = forms.CharField(required=False, widget=forms.HiddenInput)   # "5,2,9"
    marks_json = forms.CharField(required=False, widget=forms.HiddenInput)       # {"5":"30","2":"70"}

    class Meta:
        model = Questionnaire
        fields = ['scoring_type', 'passing_percentage']
        widgets = {
            'scoring_type': forms.RadioSelect,
            'passing_percentage': forms.NumberInput(attrs={'min': 0, 'max': 100}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        selected_ids = []
        if self.instance.pk:
            selected_ids = list(self.instance.items.values_list('question_id', flat=True))
        self.fields['questions'].queryset = InductionQuestion.objects.filter(
            Q(is_active=True) | Q(pk__in=selected_ids))
        self.fields['plants'].queryset = Plant.objects.all()
        self.fields['scoring_type'].initial = 'AUTO'

        if self.instance.pk and not self.is_bound:
            items = list(self.instance.items.all())
            self.fields['plants'].initial = list(
                self.instance.plant_links.values_list('plant_id', flat=True))
            self.fields['questions'].initial = [i.question_id for i in items]
            self.fields['question_order'].initial = ",".join(str(i.question_id) for i in items)
            self.fields['marks_json'].initial = json.dumps(
                {str(i.question_id): str(i.marks) for i in items})

    def clean(self):
        cleaned = super().clean()
        plants = cleaned.get('plants')
        questions = cleaned.get('questions')
        if not plants or not questions:
            return cleaned

        # 1) a plant can have only one questionnaire
        clash = QuestionnairePlant.objects.filter(plant__in=plants)
        if self.instance.pk:
            clash = clash.exclude(questionnaire=self.instance)
        if clash.exists():
            names = ", ".join(sorted({c.plant.name for c in clash.select_related('plant')}))
            raise forms.ValidationError(f"Already assigned to another questionnaire: {names}")

        # 2) sequence = drag & drop order
        selected = [q.pk for q in questions]
        try:
            order = [int(x) for x in (cleaned.get('question_order') or '').split(',') if x.strip()]
        except ValueError:
            order = []
        self.ordered_ids = [i for i in order if i in selected] + [i for i in selected if i not in order]

        # 3) marks
        n = len(self.ordered_ids)
        total = Decimal('100')
        if cleaned.get('scoring_type') == 'MANUAL':
            try:
                raw = json.loads(cleaned.get('marks_json') or '{}')
                marks = [Decimal(str(raw.get(str(i), '0') or '0')) for i in self.ordered_ids]
            except Exception:
                raise forms.ValidationError("Invalid marks.")
            if any(m < 0 for m in marks):
                raise forms.ValidationError("Marks cannot be negative.")
            if sum(marks) != total:
                raise forms.ValidationError(
                    f"Total marks must be exactly 100 (currently {sum(marks)}).")
        else:
            each = (total / n).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
            marks = [each] * (n - 1) + [total - each * (n - 1)]
        self.marks_list = marks
        return cleaned

    @transaction.atomic
    def save(self, commit=True, user=None):
        q = super().save(commit=False)
        if not q.pk and user:
            q.created_by = user
        q.total_marks = Decimal('100')
        q.save()

        q.plant_links.all().delete()
        QuestionnairePlant.objects.bulk_create(
            [QuestionnairePlant(questionnaire=q, plant=p) for p in self.cleaned_data['plants']])

        q.items.all().delete()
        QuestionnaireQuestion.objects.bulk_create([
            QuestionnaireQuestion(questionnaire=q, question_id=qid, sequence_no=seq, marks=m)
            for seq, (qid, m) in enumerate(zip(self.ordered_ids, self.marks_list), start=1)
        ])
        return q


# ---------------------------------------------------------------------
# 4. QR DOWNLOAD FORM  (select plant -> QR changes)
# ---------------------------------------------------------------------
class QRDownloadForm(forms.Form):
    plant = forms.ModelChoiceField(queryset=Plant.objects.all(), empty_label="Select plant")


# ---------------------------------------------------------------------
# 5. PUBLIC QR PAGE FORM
#    user_category + language + category-specific participant details
# ---------------------------------------------------------------------
class InductionStartForm(forms.Form):
    user_category = forms.ModelChoiceField(queryset=UserCategory.objects.filter(is_active=True))
    language      = forms.ModelChoiceField(queryset=Language.objects.filter(is_active=True))

    # ---- Common identity (all categories) ----
    participant_name   = forms.CharField(max_length=150, required=False)
    participant_mobile = forms.RegexField(
        regex=r'^\+?\d{8,15}$', max_length=20, required=False,
        error_messages={'invalid': 'Enter a valid mobile number.'})
    participant_email  = forms.EmailField(required=False)

    # ---- Visitor ----
    visitor_company        = forms.CharField(max_length=150, required=False)
    visitor_purpose        = forms.CharField(max_length=255, required=False)
    visitor_person_to_meet = forms.CharField(max_length=150, required=False)

    # ---- Contractor ----
    contractor_company = forms.CharField(max_length=150, required=False)
    contractor_id      = forms.CharField(max_length=50, required=False)

    # ---- Employee ----
    employee_id = forms.CharField(max_length=50, required=False)
    department  = forms.CharField(max_length=100, required=False)

    device_token = forms.CharField(max_length=64, required=False)

    def clean(self):
        cleaned = super().clean()
        category = cleaned.get('user_category')
        if not category:
            return cleaned

        slug = category.slug or slugify(category.name)
        required = [name for name, _, req in PARTICIPANT_FIELDS.get(slug, []) if req]

        errors = {}
        for field in required:
            if not cleaned.get(field):
                errors[field] = self.fields[field].error_messages.get(
                    'required', 'This field is required.')

        if errors:
            raise forms.ValidationError(errors)

        return cleaned

    def participant_data(self):
        """
        Return only the fields that belong to the chosen category,
        ready to splat into InductionAttempt.objects.create(...).
        """
        cat = self.cleaned_data.get('user_category')
        slug = (cat.slug or slugify(cat.name)) if cat else ''
        fields = [name for name, _, _ in PARTICIPANT_FIELDS.get(slug, [])]
        return {name: self.cleaned_data.get(name, '') for name in fields}