import base64
import io
import json
import mimetypes
import os
import tempfile
import zipfile
from datetime import timedelta
from urllib.parse import urlencode

import qrcode
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.staticfiles import finders
from django.db import transaction
from django.db.models import Avg, Count, Max, ProtectedError, Q, Sum
from django.db.models.functions import ExtractHour, TruncDate
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import get_template
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.decorators import method_decorator
from django.utils.text import slugify
from django.views import View
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET
from django.views.generic import (
    CreateView, DetailView, FormView, ListView, TemplateView, UpdateView,
)
from html2image import Html2Image
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from apps.organizations.models import Plant
from .forms import (
    PARTICIPANT_FIELDS, InductionStartForm, InductionVideoForm, QRDownloadForm,
    QuestionForm, QuestionFormSet, QuestionnaireForm,
)
from .languages import portal_languages
from .models import (
    InductionAttempt, InductionAttemptAnswer, InductionQuestion,
    InductionQuestionOption, InductionVideo, Language, PlantQRCode,
    Questionnaire, QuestionnaireQuestion, UserCategory,
)


# =====================================================================
# Helpers
# =====================================================================
def _departments(plant):
    """
    Real department names from the system, for the portal dropdown.
    Adjust the import if your Department model lives somewhere else.
    """
    try:
        from apps.organizations.models import Department
    except ImportError:
        return []

    names = {f.name for f in Department._meta.get_fields()}
    qs = Department.objects.all()
    if 'is_active' in names:
        qs = qs.filter(is_active=True)

    scoped = qs
    if 'plant' in names:                 # department belongs to one plant
        scoped = qs.filter(plant=plant)
    elif 'plants' in names:              # many-to-many with plants
        scoped = qs.filter(plants=plant)

    # plant has no departments of its own -> fall back to all active departments
    rows = scoped if scoped.exists() else qs
    return list(rows.order_by('name').values_list('name', flat=True).distinct())


def _is_ajax(request):
    return request.headers.get('x-requested-with') == 'XMLHttpRequest'


def _fmt(d):
    """Decimal(100.00) -> '100', Decimal(33.33) -> '33.33'"""
    return format(d.normalize(), 'f') if d is not None else '0'


def _payload(request):
    """JSON body or normal POST"""
    if request.content_type == 'application/json':
        try:
            return json.loads(request.body or '{}')
        except ValueError:
            return {}
    return request.POST


def _plant_questionnaire(plant):
    return (Questionnaire.objects
            .filter(plant_links__plant=plant, is_active=True)
            .prefetch_related('items').first())


# =====================================================================
# Mixins
# =====================================================================
class InductionLoginMixin(LoginRequiredMixin):
    """Every admin-side page needs a logged-in user (the public QR portal does not)."""


class AjaxFormMixin:
    """
    For Create / Update / Form views.
    Modal (AJAX)  -> JSON  {ok, message} or {ok: False, errors}
    Normal form   -> flash message + redirect, or re-render with errors
    """
    success_message = ''

    def save_form(self, form):
        return form.save(user=self.request.user)

    def get_errors(self, form):
        return form.errors.get_json_data()

    def form_valid(self, form):
        self.object = self.save_form(form)
        if _is_ajax(self.request):
            return JsonResponse({'ok': True, 'message': self.success_message})
        messages.success(self.request, self.success_message)
        return redirect(self.get_success_url())

    def form_invalid(self, form):
        if _is_ajax(self.request):
            return JsonResponse({'ok': False, 'errors': self.get_errors(form)}, status=400)
        return super().form_invalid(form)


class SafeDeleteView(InductionLoginMixin, View):
    """POST-only delete that survives PROTECTed foreign keys."""
    model = None
    success_url = None
    success_message = "Deleted."
    protected_message = "This record is in use and cannot be deleted."
    http_method_names = ['post']

    def delete_object(self, obj):
        obj.delete()

    def on_protected(self, obj):
        messages.error(self.request, self.protected_message)

    def post(self, request, pk):
        obj = get_object_or_404(self.model, pk=pk)
        try:
            self.delete_object(obj)
            messages.success(request, self.success_message)
        except ProtectedError:
            self.on_protected(obj)
        return redirect(self.success_url)


# =====================================================================
# 1. DASHBOARD
# =====================================================================
def _pct_change(cur, prev):
    """+12 / -5 / None (no previous data to compare with)."""
    if not prev:
        return None
    return round((cur - prev) / prev * 100)


def _vs(v, unit):
    """Plain-English delta used in the downloadable card descriptions."""
    if v is None:
        return "there is no earlier data to compare with"
    if v == 0:
        return "unchanged from the previous period"
    return f"{'up' if v > 0 else 'down'} {abs(v)}{unit} from the previous period"


class DashboardView(InductionLoginMixin, TemplateView):
    template_name = 'induction/dashboard.html'
    RANGES = [('today', 'Today'), ('7d', '7 days'), ('30d', '30 days'), ('all', 'All time')]
    SCORE_BANDS = ['0-19%', '20-39%', '40-59%', '60-79%', '80-100%']

    def _window(self, key):
        """(start, previous_start, previous_end) for the chosen range."""
        now = timezone.now()
        if key == 'today':
            start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
            return start, start - timedelta(days=1), start
        if key in ('7d', '30d'):
            days = 7 if key == '7d' else 30
            start = now - timedelta(days=days)
            return start, start - timedelta(days=days), start
        return None, None, None

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        key = self.request.GET.get('range', '30d')
        if key not in dict(self.RANGES):
            key = '30d'
        # ---------- plant filter: ?plant=1&plant=2 (none = all plants) ----------
        all_plants = list(Plant.objects.all().order_by('name'))
        sel_ids = {int(i) for i in self.request.GET.getlist('plant') if i.isdigit()}
        sel_ids &= {pl.id for pl in all_plants}
        sel_names = [pl.name for pl in all_plants if pl.id in sel_ids]
        scope = (', '.join(sel_names) if 0 < len(sel_names) <= 3
                 else f"{len(sel_names)} plants" if sel_names else '')
        rng_label = dict(self.RANGES)[key]
        label = f"{rng_label}, {scope}" if scope else rng_label
        start, prev_start, prev_end = self._window(key)

        submitted = InductionAttempt.objects.filter(status='SUBMITTED')
        if sel_ids:
            submitted = submitted.filter(plant_id__in=sel_ids)
        cur = submitted.filter(submitted_at__gte=start) if start else submitted
        prev = (submitted.filter(submitted_at__gte=prev_start, submitted_at__lt=prev_end)
                if prev_start else submitted.none())

        # ---------- KPI tiles ----------
        def summary(qs):
            a = qs.aggregate(n=Count('id'), ok=Count('id', filter=Q(result='PASS')),
                             avg=Avg('score_percentage'))
            n, ok = a['n'] or 0, a['ok'] or 0
            return {'n': n, 'ok': ok, 'failed': n - ok,
                    'rate': round(ok / n * 100) if n else 0,
                    'avg': round(a['avg'] or 0)}

        c, p = summary(cur), summary(prev)

        fields = {f.name for f in InductionAttempt._meta.get_fields()}
        started = next((n for n in ('started_at', 'created_at') if n in fields), None)
        open_qs = InductionAttempt.objects.filter(status='IN_PROGRESS')
        if sel_ids:
            open_qs = open_qs.filter(plant_id__in=sel_ids)
        if started:
            open_qs = open_qs.filter(**{f'{started}__gte': timezone.now() - timedelta(hours=24)})
        in_progress = open_qs.count()

        # ---------- plant readiness board ----------
        plants = [pl for pl in all_plants if not sel_ids or pl.id in sel_ids]
        qmap = {}
        for q in (Questionnaire.objects.filter(is_active=True)
                  .prefetch_related('plant_links').annotate(question_count=Count('items', distinct=True))):
            for link in q.plant_links.all():
                qmap[link.plant_id] = q
        qrmap = {r.plant_id: r for r in PlantQRCode.objects.filter(is_active=True)}
        stats = {r['plant_id']: r for r in cur.values('plant_id').annotate(
            n=Count('id'), ok=Count('id', filter=Q(result='PASS')),
            avg=Avg('score_percentage'), last=Max('submitted_at'))}

        plant_rows, no_questionnaire, no_qr = [], [], []
        for pl in plants:
            q, qr, s = qmap.get(pl.id), qrmap.get(pl.id), stats.get(pl.id)
            n = s['n'] if s else 0
            missing = []
            if not q:
                missing.append('Questionnaire'); no_questionnaire.append(pl.name)
            if not qr:
                missing.append('QR code'); no_qr.append(pl.name)
            plant_rows.append({
                'name': pl.name,
                'questions': q.question_count if q else 0,
                'has_qr': bool(qr),
                'downloaded': bool(qr and qr.last_downloaded_at),
                'count': n,
                'ok': s['ok'] if s else 0,
                'rate': round(s['ok'] / n * 100) if n else 0,
                'avg': round(s['avg'] or 0) if n else 0,
                'last': s['last'] if s else None,
                'missing': missing,
                'state': 'setup' if missing else ('live' if n else 'ready'),
            })
        order = {'setup': 0, 'ready': 1, 'live': 2}
        plant_rows.sort(key=lambda r: (order[r['state']], -r['count'], r['name']))
        live_plants = sum(1 for r in plant_rows if r['state'] == 'live')
        state_counts = {s: sum(1 for r in plant_rows if r['state'] == s) for s in order}

        # ---------- who / how ----------
        categories = []
        for r in cur.values('user_category__name').annotate(n=Count('id'),
                                                            ok=Count('id', filter=Q(result='PASS'))).order_by('-n'):
            categories.append({'name': r['user_category__name'] or 'Unknown', 'n': r['n'],
                               'share': round(r['n'] / c['n'] * 100) if c['n'] else 0,
                               'rate': round(r['ok'] / r['n'] * 100) if r['n'] else 0})
        languages = [{'name': r['language__name'] or 'Unknown', 'n': r['n']}
                     for r in cur.values('language__name').annotate(n=Count('id')).order_by('-n')[:8]]

        # ---------- 14-day activity strip ----------
        today = timezone.localdate()
        first = today - timedelta(days=13)
        per_day = {r['day']: r['n'] for r in submitted.filter(submitted_at__date__gte=first)
                   .annotate(day=TruncDate('submitted_at')).values('day').annotate(n=Count('id'))}
        peak = max(per_day.values(), default=0)
        heat = []
        for i in range(14):
            d = first + timedelta(days=i)
            n = per_day.get(d, 0)
            heat.append({'label': d.strftime('%d %b'), 'dow': d.strftime('%a')[0], 'n': n,
                         'level': 0 if not n else 1 + min(3, int(n / peak * 3.999)) if peak else 0})

        # ---------- latest 3 PEOPLE (only each person's most recent attempt) ----------
        latest_people, seen = [], set()
        recent = (submitted.select_related('plant', 'user_category', 'language')
                  .order_by('-submitted_at')[:300])
        for a in recent:
            k = _person_key(a)            # defined further down (same helper the scorecard uses)
            if k in seen:
                continue
            seen.add(k)
            latest_people.append(a)
            if len(latest_people) == 3:
                break

        # ---------- content health ----------
        active_langs = list(portal_languages())
        with_video = set(InductionVideo.objects.values_list('language_id', flat=True))
        missing_video = [l.name for l in active_langs if l.id not in with_video]

        # =============================================================
        # CHART DATA (+ plain-English description for every card)
        # =============================================================
        tdays = {'today': 14, '7d': 7}.get(key, 30)
        t_first = today - timedelta(days=tdays - 1)
        t_map = {r['day']: r for r in submitted.filter(submitted_at__date__gte=t_first)
                 .annotate(day=TruncDate('submitted_at')).values('day')
                 .annotate(n=Count('id'), ok=Count('id', filter=Q(result='PASS')))}
        trend = []
        for i in range(tdays):
            d = t_first + timedelta(days=i)
            r = t_map.get(d)
            n, ok = (r['n'], r['ok']) if r else (0, 0)
            trend.append({'label': d.strftime('%d %b'), 'ok': ok, 'fail': n - ok})

        # plants: top 10 by volume
        top_plants = sorted([r for r in plant_rows if r['count']], key=lambda r: -r['count'])[:10]

        # score distribution (5 bands)
        scores = [0] * 5
        for s in cur.values_list('score_percentage', flat=True):
            scores[min(4, int((s or 0) // 20))] += 1

        # submissions by hour of day
        hours = [0] * 24
        for r in cur.annotate(h=ExtractHour('submitted_at')).values('h').annotate(n=Count('id')):
            if r['h'] is not None:
                hours[r['h']] = r['n']

        charts = {
            'trend': {'labels': [t['label'] for t in trend],
                      'ok': [t['ok'] for t in trend], 'fail': [t['fail'] for t in trend]},
            'result': [c['ok'], c['failed']], 'rate': c['rate'],
            'plants': {'labels': [r['name'] for r in top_plants],
                       'ok': [r['ok'] for r in top_plants],
                       'fail': [r['count'] - r['ok'] for r in top_plants]},
            'categories': {'labels': [x['name'] for x in categories], 'data': [x['n'] for x in categories]},
            'languages': {'labels': [x['name'] for x in languages], 'data': [x['n'] for x in languages]},
            'scores': {'labels': self.SCORE_BANDS, 'data': scores},
            'hours': {'labels': [f"{h:02d}:00" for h in range(24)], 'data': hours},
        }

        # ---- descriptions (printed under the card when the image is downloaded) ----
        t_total = sum(t['ok'] + t['fail'] for t in trend)
        pk_day = max(trend, key=lambda t: t['ok'] + t['fail'])
        top_cat = categories[0] if categories else None
        top_lang = languages[0] if languages else None
        top_pl = top_plants[0] if top_plants else None
        band_i = max(range(5), key=lambda i: scores[i]) if any(scores) else None
        hr_i = max(range(24), key=lambda i: hours[i]) if any(hours) else None

        desc = {
            'k_completed': f"Inductions fully submitted in the selected range ({label}). "
                           f"Currently {c['n']}, {_vs(_pct_change(c['n'], p['n']), '%')}.",
            'k_rate': f"Share of submitted inductions that passed ({label}). {c['ok']} of {c['n']} passed "
                      f"({c['rate']}%), {_vs((c['rate'] - p['rate']) if p['n'] else None, ' points')}.",
            'k_avg': f"Average assessment score across all submitted inductions ({label}): {c['avg']}%, "
                     f"{_vs((c['avg'] - p['avg']) if p['n'] else None, ' points')}.",
            'k_failed': f"Attempts that did not reach the passing percentage ({label}): {c['failed']}. "
                        f"These people must retake the assessment.",
            'k_progress': f"Inductions started in the last 24 hours that have not been submitted yet: {in_progress}.",
            'k_plants': f"Plants that received at least one induction in {label}: {live_plants} of {len(plants)}.",
            'trend': (f"Daily submitted inductions for the last {tdays} days, split into passed and failed. "
                      + (f"{t_total} inductions in total; busiest day was {pk_day['label']} with "
                         f"{pk_day['ok'] + pk_day['fail']}." if t_total else "No inductions were submitted in this period.")),
            'result': f"Passed versus failed attempts ({label}): {c['ok']} passed and {c['failed']} failed, "
                      f"a pass rate of {c['rate']}%.",
            'plants': ((f"Inductions per plant ({label}), passed vs failed, top {len(top_plants)} plants. "
                        f"{top_pl['name']} leads with {top_pl['count']} inductions.") if top_pl
                       else f"No plant has inductions in {label}."),
            'scores': ((f"How participants scored ({label}), grouped in 20% bands. "
                        f"Most common band: {self.SCORE_BANDS[band_i]} ({scores[band_i]} people).") if band_i is not None
                       else f"No scores in {label}."),
            'hours': ((f"Time of day when inductions are submitted ({label}). "
                       f"Peak hour: {hr_i:02d}:00 with {hours[hr_i]} submissions.") if hr_i is not None
                      else f"No submissions in {label}."),
            'categories': ((f"Who completes the induction ({label}). Largest group: {top_cat['name']} "
                            f"({top_cat['n']} people, {top_cat['share']}% of total).") if top_cat
                           else f"No inductions in {label}."),
            'languages': ((f"Languages chosen by participants ({label}). Most used: {top_lang['name']} "
                           f"({top_lang['n']}).") if top_lang else f"No inductions in {label}."),
            'board': (f"Setup and performance of every plant. {state_counts['live']} live, "
                      f"{state_counts['ready']} ready (set up, no inductions in range) and "
                      f"{state_counts['setup']} needing setup (missing questionnaire or QR code)."),
            'heat': f"Inductions per day over the last 14 days. Darker squares mean more inductions; "
                    f"{sum(h['n'] for h in heat)} in total.",
            'attention': (f"Open items: {len(no_questionnaire)} plant(s) without a questionnaire, "
                          f"{len(no_qr)} without a QR code, {len(missing_video)} language(s) without a video "
                          f"and {c['failed']} failed attempt(s) in {label}."),
        }

        ctx.update({
            'range': key, 'ranges': self.RANGES, 'range_label': label,
            'all_plants': all_plants, 'sel_ids': sorted(sel_ids),
            'plant_qs': ''.join(f'&plant={i}' for i in sorted(sel_ids)), 'now': timezone.localtime(),
            'kpi': {
                'completed': c['n'], 'completed_delta': _pct_change(c['n'], p['n']),
                'rate': c['rate'], 'rate_delta': (c['rate'] - p['rate']) if p['n'] else None,
                'avg': c['avg'], 'avg_delta': (c['avg'] - p['avg']) if p['n'] else None,
                'failed': c['failed'], 'in_progress': in_progress,
                'plants_total': len(plants), 'plants_live': live_plants,
            },
            'plant_rows': plant_rows, 'state_counts': state_counts,
            'categories': categories, 'languages': languages, 'heat': heat,
            'charts': charts, 'desc': desc,
            'activity': latest_people,
            'attention': {
                'no_questionnaire': no_questionnaire[:6], 'no_questionnaire_n': len(no_questionnaire),
                'no_qr': no_qr[:6], 'no_qr_n': len(no_qr),
                'missing_video': missing_video[:6], 'missing_video_n': len(missing_video),
                'failed': c['failed'],
            },
            'content': {
                'questions': InductionQuestion.objects.count(),
                'questions_active': InductionQuestion.objects.filter(is_active=True).count(),
                'videos': InductionVideo.objects.count(),
                'languages_total': len(active_langs),
                'languages_covered': len(active_langs) - len(missing_video),
                'questionnaires': Questionnaire.objects.count(),
                'questionnaires_active': Questionnaire.objects.filter(is_active=True).count(),
            },
        })
        ctx['attention_total'] = (ctx['attention']['no_questionnaire_n'] + ctx['attention']['no_qr_n']
                                  + ctx['attention']['missing_video_n'])
        return ctx


# =====================================================================
# 2. INDUCTION QUE
# =====================================================================
class QuestionListView(InductionLoginMixin, ListView):
    model = InductionQuestion
    template_name = 'induction/question_list.html'
    paginate_by = 10

    # ---- read ?q= ?status= ----
    def _read_filters(self):
        g = self.request.GET
        self.q = g.get('q', '').strip()
        self.status = g.get('status', '')
        if self.status not in ('active', 'inactive', 'used'):
            self.status = ''

    def _used_ids(self):
        """Question ids that sit inside at least one questionnaire."""
        return QuestionnaireQuestion.objects.values('question_id')

    def _base(self):
        """Search only (NO status filter) -> used for the stat tiles too."""
        qs = InductionQuestion.objects.all()
        if self.q:
            qs = qs.filter(question_text__icontains=self.q)
        return qs

    def get_queryset(self):
        self._read_filters()
        self.base = self._base()
        qs = self.base
        if self.status == 'active':
            qs = qs.filter(is_active=True)
        elif self.status == 'inactive':
            qs = qs.filter(is_active=False)
        elif self.status == 'used':
            qs = qs.filter(id__in=self._used_ids())
        return qs.prefetch_related('options')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        page = ctx['page_obj']

        used = dict(QuestionnaireQuestion.objects
                    .order_by()
                    .values_list('question_id')
                    .annotate(c=Count('id')))
        for obj in page:
            obj.used_in = used.get(obj.id, 0)
            obj.correct = next((o for o in obj.options.all() if o.is_correct), None)

        # ---- stat tiles: single aggregate, always consistent ----
        agg = self.base.aggregate(
            total=Count('id'),
            active=Count('id', filter=Q(is_active=True)),
            inactive=Count('id', filter=Q(is_active=False)),
            used=Count('id', filter=Q(id__in=self._used_ids())),
        )

        params = [('q', self.q)] if self.q else []

        ctx.update(
            page=page, q=self.q, status=self.status,
            total_count=agg['total'] or 0,
            active_count=agg['active'] or 0,
            inactive_count=agg['inactive'] or 0,
            used_count=agg['used'] or 0,
            base_qs=urlencode(params),
            form=QuestionForm(), bulk_formset=QuestionFormSet(),
        )
        return ctx


class QuestionCreateView(InductionLoginMixin, AjaxFormMixin, CreateView):
    model = InductionQuestion
    form_class = QuestionForm
    template_name = 'induction/question_form.html'
    context_object_name = 'question'
    success_url = reverse_lazy('induction:question_list')
    success_message = "Question added."


class QuestionBulkCreateView(InductionLoginMixin, AjaxFormMixin, FormView):
    """Add Bulk Que - several question blocks saved together"""
    form_class = QuestionFormSet
    template_name = 'induction/question_bulk_form.html'
    success_url = reverse_lazy('induction:question_list')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['formset'] = ctx['form']
        return ctx

    def save_form(self, formset):
        count = 0
        with transaction.atomic():
            for form in formset:
                if form.has_changed():
                    form.save(user=self.request.user)
                    count += 1
        self.success_message = f"{count} question(s) added."

    def get_errors(self, formset):
        return {'forms': [f.errors.get_json_data() for f in formset.forms],
                'non_form': formset.non_form_errors()}


class QuestionUpdateView(InductionLoginMixin, AjaxFormMixin, UpdateView):
    model = InductionQuestion
    form_class = QuestionForm
    template_name = 'induction/question_form.html'
    context_object_name = 'question'
    success_url = reverse_lazy('induction:question_list')
    success_message = "Question updated."


class QuestionDetailView(InductionLoginMixin, DetailView):
    """View icon - options with the correct one marked"""
    queryset = InductionQuestion.objects.prefetch_related('options')
    template_name = 'induction/question_detail.html'
    context_object_name = 'question'


class QuestionDeleteView(SafeDeleteView):
    model = InductionQuestion
    success_url = reverse_lazy('induction:question_list')
    success_message = "Question deleted."
    protected_message = ("This question is used in a questionnaire. "
                         "Remove it from the questionnaire or make it inactive.")


# =====================================================================
# 3. INDUCTION VIDEO
# =====================================================================
class VideoListView(InductionLoginMixin, ListView):
    model = InductionVideo
    template_name = 'induction/video_list.html'
    paginate_by = 10

    def get_queryset(self):
        qs = InductionVideo.objects.select_related('language')
        self.q = self.request.GET.get('q', '').strip()
        if self.q:
            qs = qs.filter(
                Q(language__name__icontains=self.q) |
                Q(language__code__icontains=self.q) |
                Q(file_name__icontains=self.q)
            )
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        # ---- stat tiles: always reflect the full library, not the search ----
        all_videos = InductionVideo.objects.all()
        total_count = all_videos.count()

        language_count = (all_videos
                          .values('language_id')
                          .distinct()
                          .count())

        # file_size stored in bytes -> show MB (1 decimal, safe if null)
        total_bytes = all_videos.aggregate(s=Sum('file_size'))['s'] or 0
        total_size_mb = round(total_bytes / 1048576, 1)

        # "Missing" = portal languages that have no video yet
        active_lang_ids = list(portal_languages().values_list('id', flat=True))
        with_video_ids = set(all_videos.values_list('language_id', flat=True))
        missing_count = sum(1 for lid in active_lang_ids if lid not in with_video_ids)

        ctx.update(
            page=ctx['page_obj'],
            q=self.q,
            form=InductionVideoForm(),
            languages=portal_languages(),
            used_language_ids=list(with_video_ids),     # languages hidden from "Add video"
            total_count=total_count,
            language_count=language_count,
            total_size=total_size_mb,
            missing_count=missing_count,
        )
        return ctx


class VideoCreateView(InductionLoginMixin, AjaxFormMixin, CreateView):
    model = InductionVideo
    form_class = InductionVideoForm
    template_name = 'induction/video_form.html'
    context_object_name = 'video'
    success_url = reverse_lazy('induction:video_list')
    success_message = "Video uploaded."


class VideoUpdateView(InductionLoginMixin, AjaxFormMixin, UpdateView):
    model = InductionVideo
    form_class = InductionVideoForm
    template_name = 'induction/video_form.html'
    context_object_name = 'video'
    success_url = reverse_lazy('induction:video_list')
    success_message = "Video updated."


class VideoDeleteView(SafeDeleteView):
    model = InductionVideo
    success_url = reverse_lazy('induction:video_list')
    success_message = "Video deleted."

    def delete_object(self, video):
        video.file.delete(save=False)      # remove the file from storage too
        video.delete()


# =====================================================================
# 4. ASSIGN QUE
# =====================================================================
class AssignListView(InductionLoginMixin, ListView):
    model = Questionnaire
    template_name = 'induction/assign_list.html'
    paginate_by = 10

    def get_queryset(self):
        qs = (Questionnaire.objects
              .prefetch_related('plant_links__plant', 'items')
              .annotate(question_count=Count('items', distinct=True)))

        self.q = self.request.GET.get('q', '').strip()
        self.status = self.request.GET.get('status', '')

        if self.q:
            qs = qs.filter(plant_links__plant__name__icontains=self.q).distinct()

        if self.status == 'active':
            qs = qs.filter(is_active=True)
        elif self.status == 'inactive':
            qs = qs.filter(is_active=False)

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        # ---- stat tiles: whole library, independent of search/status ----
        all_q = Questionnaire.objects.all()

        total_q = all_q.count()
        active_q = all_q.filter(is_active=True).count()

        # Distinct questions linked anywhere (avoids double-count if the same
        # question is in more than one questionnaire)
        total_questions = (QuestionnaireQuestion.objects
                           .values('question_id')
                           .distinct()
                           .count())

        # Distinct plants linked to at least one questionnaire
        covered_plants = (all_q
                          .filter(plant_links__isnull=False)
                          .values('plant_links__plant_id')
                          .distinct()
                          .count())

        ctx.update(
            page=ctx['page_obj'],
            q=self.q,
            status=self.status,
            qr_form=QRDownloadForm(),

            total_q=total_q,
            active_count=active_q,
            total_questions=total_questions,
            covered_plants=covered_plants,
        )
        return ctx


class AssignFormMixin:
    """
    Assign form shown in a modal (AJAX -> partial) or as a full page.
    Also supplies the question rows (code, options, selected) for the template,
    plus the saved sequence + marks so Edit can restore them.
    """
    template_name = 'induction/assign_form.html'
    partial_template_name = 'induction/assign_form.html'

    def get_template_names(self):
        return [self.partial_template_name if _is_ajax(self.request) else self.template_name]

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        form = ctx['form']

        # Determine selected IDs (handles both GET initial and POST error re-render)
        val = form['questions'].value()
        selected = {str(v) for v in val} if val else set()

        queryset = form.fields['questions'].queryset.prefetch_related('options')

        ctx['question_rows'] = [{
            'id': q.id,
            'code': q.code,
            'text': q.question_text,
            'options': len(q.options.all()),
            'selected': str(q.id) in selected,
        } for q in queryset]

        # Ensure questionnaire instance is available for the template
        questionnaire = getattr(self, 'object', None)
        ctx['questionnaire'] = questionnaire

        # Saved sequence + marks (empty on Add, filled on Edit)
        items = []
        if questionnaire is not None and questionnaire.pk:
            items = list(questionnaire.items.order_by('sequence_no'))
        ctx['existing_order'] = [i.question_id for i in items]
        ctx['existing_marks'] = {str(i.question_id): float(i.marks) for i in items}
        return ctx


class AssignCreateView(InductionLoginMixin, AssignFormMixin, AjaxFormMixin, CreateView):
    model = Questionnaire
    form_class = QuestionnaireForm
    template_name = 'induction/assign_form.html'
    context_object_name = 'questionnaire'
    success_url = reverse_lazy('induction:assign_list')
    success_message = "Questionnaire assigned."
    # QuestionnaireForm.save() already writes the order + marks


class AssignUpdateView(InductionLoginMixin, AssignFormMixin, AjaxFormMixin, UpdateView):
    model = Questionnaire
    form_class = QuestionnaireForm
    template_name = 'induction/assign_form.html'
    context_object_name = 'questionnaire'
    success_url = reverse_lazy('induction:assign_list')
    success_message = "Questionnaire updated."


class AssignDetailView(InductionLoginMixin, DetailView):
    """View icon - full detail page"""
    queryset = Questionnaire.objects.prefetch_related('plant_links__plant')
    template_name = 'induction/assign_detail.html'
    context_object_name = 'questionnaire'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        items = list(self.object.items.select_related('question')
                     .prefetch_related('question__options'))
        ctx.update(
            items=items,
            plants=[l.plant for l in self.object.plant_links.all()],
            total_marks=sum(i.marks for i in items),
        )
        return ctx


class AssignDeleteView(SafeDeleteView):
    model = Questionnaire
    success_url = reverse_lazy('induction:assign_list')
    success_message = "Questionnaire deleted."

    def on_protected(self, questionnaire):
        # Has submitted attempts: keep history, switch off, release its plants.
        questionnaire.deactivate()
        messages.warning(self.request, "It has submitted attempts, so it was made inactive "
                                       "and its plants were released.")


# ---------------------------------------------------------------------
# QR code (modal: select plant -> QR changes -> download poster)
# One PlantQRCode (own token) per plant, created on first use.
# ---------------------------------------------------------------------
POSTER_SIZE = (1200, 1280)


def _active_qr(plant, user):
    """The plant's active QR record; created automatically if the plant has none."""
    qr = PlantQRCode.objects.filter(plant=plant, is_active=True).first()
    return qr or PlantQRCode.objects.create(plant=plant, generated_by=user)


POSTER_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'poster_assets')


def _asset_data_uri(filename, size=None, fit='cover'):
    """
    Inline an image into the poster HTML (Chrome then never needs a URL).
    Looks in <app>/poster_assets/ first, then falls back to Django static files.
    `size` normalises the picture to the exact box the poster expects, so even a
    huge or differently shaped file still renders correctly.
    """
    path = os.path.join(POSTER_ASSETS, filename)
    if not os.path.exists(path):
        path = finders.find('images/' + filename)
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"Poster image missing: {filename} "
                                f"(put it in {POSTER_ASSETS})")

    if size:
        from PIL import Image, ImageOps
        img = Image.open(path)
        if fit == 'cover':
            img = ImageOps.fit(img.convert('RGB'), size, Image.LANCZOS)
            fmt, mime = 'JPEG', 'image/jpeg'
        else:                                    # 'contain' - keep transparency
            img = img.convert('RGBA')
            img.thumbnail(size, Image.LANCZOS)
            fmt, mime = 'PNG', 'image/png'
        buf = io.BytesIO()
        img.save(buf, fmt, **({'quality': 95} if fmt == 'JPEG' else {}))
        return f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode()

    mime = mimetypes.guess_type(path)[0] or 'image/png'
    with open(path, 'rb') as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


def _build_poster_png(request, plant):
    """Render the poster for ONE plant (its own QR token) and return PNG bytes."""
    qr_record = _active_qr(plant, request.user)
    portal_url = request.build_absolute_uri(
        reverse('induction:portal', args=[qr_record.token]))

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_Q,
                       box_size=20, border=2)
    qr.add_data(portal_url)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="#112d4e", back_color="white").save(buf, format="PNG")
    qr_data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    html = get_template('induction/qr_poster.html').render({
        'plant': plant,
        'qr_data_uri': qr_data_uri,
        'building_image_url': _asset_data_uri('everest-building.jpeg', size=(1000, 760), fit='cover'),
    })

    # private temp dir per call -> concurrent downloads never overwrite each other
    with tempfile.TemporaryDirectory() as tmp:
        hti = Html2Image(output_path=tmp,
                         custom_flags=['--hide-scrollbars', '--no-sandbox', '--disable-gpu'])
        hti.screenshot(html_str=html, size=POSTER_SIZE, save_as='poster.png')
        with open(os.path.join(tmp, 'poster.png'), 'rb') as f:
            png = f.read()

    qr_record.last_downloaded_at = timezone.now()
    qr_record.save(update_fields=['last_downloaded_at'])
    return png


class QRInfoView(InductionLoginMixin, View):
    """AJAX - called every time the plant dropdown changes"""
    http_method_names = ['get']

    def get(self, request):
        form = QRDownloadForm(request.GET)
        if not form.is_valid():
            return JsonResponse({'ok': False, 'errors': form.errors.get_json_data()}, status=400)
        plant = form.cleaned_data['plant']
        qr = _active_qr(plant, request.user)
        questionnaire = _plant_questionnaire(plant)
        return JsonResponse({
            'ok': True,
            'plant': plant.name,
            'assigned': bool(questionnaire),
            'questions': questionnaire.items.count() if questionnaire else 0,
            'portal_url': request.build_absolute_uri(reverse('induction:portal', args=[qr.token])),
            'image_url': reverse('induction:qr_image', args=[plant.pk]),
        })


class QRImageView(InductionLoginMixin, View):
    """Plain QR PNG for the modal preview. Add ?download=1 to force a file download."""
    http_method_names = ['get']

    def get(self, request, plant_id):
        plant = get_object_or_404(Plant, pk=plant_id)
        qr = _active_qr(plant, request.user)
        url = request.build_absolute_uri(reverse('induction:portal', args=[qr.token]))

        buf = io.BytesIO()
        qrcode.make(url, box_size=10, border=2).save(buf, 'PNG')

        response = HttpResponse(buf.getvalue(), content_type='image/png')
        if request.GET.get('download'):
            qr.last_downloaded_at = timezone.now()
            qr.save(update_fields=['last_downloaded_at'])
            filename = f"QR-{slugify(plant.name)}.png"
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


class QRPosterDownloadView(InductionLoginMixin, View):
    """Poster PNG for the selected plant (HTML/CSS template -> PNG)."""
    http_method_names = ['get']

    def get(self, request, plant_id):
        plant = get_object_or_404(Plant, pk=plant_id)
        try:
            png = _build_poster_png(request, plant)
        except Exception as e:
            return HttpResponse(f"Error: {e}. Make sure Chrome or Edge is installed.", status=500)

        response = HttpResponse(png, content_type='image/png')
        response['Content-Disposition'] = (
            f'attachment; filename="Induction-Poster-{slugify(plant.name)}.png"')
        return response


class QRPosterZipView(InductionLoginMixin, View):
    """One poster per plant, all in a single ZIP. Optional ?plants=1,2,3 to limit."""
    http_method_names = ['get']

    def get(self, request):
        plants = Plant.objects.all().order_by('name')
        ids = [i for i in request.GET.get('plants', '').split(',') if i.isdigit()]
        if ids:
            plants = plants.filter(pk__in=ids)

        zbuf = io.BytesIO()
        try:
            with zipfile.ZipFile(zbuf, 'w', zipfile.ZIP_DEFLATED) as zf:
                for plant in plants:
                    zf.writestr(f"Induction-Poster-{slugify(plant.name)}.png",
                                _build_poster_png(request, plant))
        except Exception as e:
            return HttpResponse(f"Error: {e}. Make sure Chrome or Edge is installed.", status=500)

        response = HttpResponse(zbuf.getvalue(), content_type='application/zip')
        response['Content-Disposition'] = 'attachment; filename="Induction-Posters-All-Plants.zip"'
        return response


# =====================================================================
# 5. SCORECARD  (data submitted from the QR page)
#    List = ONE row per participant (their latest submitted attempt).
# =====================================================================
def _person_key(a):
    """Same person = same plant + category + mobile / employee id / contractor id / name."""
    ident = (a.participant_mobile or a.employee_id or a.contractor_id
             or (a.participant_name or '').strip().lower())
    return (a.plant_id, a.user_category_id, ident)


def _same_person_q(a):
    """Query filter matching every attempt made by the same person."""
    if a.participant_mobile:
        return Q(participant_mobile=a.participant_mobile)
    if a.employee_id:
        return Q(employee_id=a.employee_id)
    if a.contractor_id:
        return Q(contractor_id=a.contractor_id)
    return Q(participant_name__iexact=a.participant_name)


class ScorecardRowsMixin:
    """
    One row per participant = their LATEST submitted attempt.
    Supports multi-select plants/categories + single-date OR date-range filters.
    """

    def get_queryset(self):
        g = self.request.GET
        qs = (InductionAttempt.objects
              .filter(status='SUBMITTED')
              .select_related('plant', 'user_category', 'language')
              .order_by('-submitted_at'))

        # ---------- Multi-select: plants / categories ----------
        plant_ids    = [i for i in g.getlist('plant')    if i.isdigit()]
        category_ids = [i for i in g.getlist('category') if i.isdigit()]

        if category_ids:
            qs = qs.filter(user_category_id__in=category_ids)
        if plant_ids:
            qs = qs.filter(plant_id__in=plant_ids)

        # ---------- Date filter: single day OR range ----------
        single = parse_date(g.get('date') or '')
        d_from = parse_date(g.get('date_from') or '')
        d_to   = parse_date(g.get('date_to') or '')

        if single and not (d_from or d_to):
            qs = qs.filter(submitted_at__date=single)
        elif d_from or d_to:
            if d_from:
                qs = qs.filter(submitted_at__date__gte=d_from)
            if d_to:
                qs = qs.filter(submitted_at__date__lte=d_to)

        # ---------- Text search ----------
        q = g.get('q', '').strip()
        if q:
            qs = qs.filter(
                Q(participant_name__icontains=q) |
                Q(participant_mobile__icontains=q) |
                Q(employee_id__icontains=q) |
                Q(contractor_id__icontains=q) |
                Q(visitor_company__icontains=q) |
                Q(contractor_company__icontains=q)
            )
        return qs

    def get_rows(self):
        """One row per participant (their latest attempt). Used by page + Excel."""
        people = {}
        for a in self.get_queryset():                    # newest first
            key = _person_key(a)
            if key in people:                            # older attempt of same person
                people[key]['attempt_count'] += 1
                continue
            people[key] = {
                'id':            a.pk,                   # latest attempt -> View button
                'name':          a.participant_name or '—',
                'mobile':        a.participant_mobile or '',
                'employee_id':   a.employee_id or '',
                'contractor_id': a.contractor_id or '',
                'company':       a.visitor_company or a.contractor_company or '',
                'category':      a.user_category.name if a.user_category_id else '',
                'plant':         a.plant.name if a.plant_id else '',
                'score':         a.score_percentage,
                'result':        a.result,
                'attempt_no':    a.attempt_no,
                'attempt_count': 1,
                'submitted_at':  a.submitted_at,
            }

        rows = list(people.values())

        result = self.request.GET.get('result')
        if result in ('PASS', 'FAIL'):
            rows = [r for r in rows if r['result'] == result]
        return rows


class ScorecardListView(InductionLoginMixin, ScorecardRowsMixin, TemplateView):
    template_name = 'induction/scorecard_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        rows = self.get_rows()

        total  = len(rows)
        passed = sum(1 for r in rows if r['result'] == 'PASS')

        g = self.request.GET
        selected_plant_ids    = [int(i) for i in g.getlist('plant')    if i.isdigit()]
        selected_category_ids = [int(i) for i in g.getlist('category') if i.isdigit()]

        ctx.update(
            rows=rows,
            categories=UserCategory.objects.filter(is_active=True),
            plants=Plant.objects.all(),

            selected_plants=selected_plant_ids,
            selected_categories=selected_category_ids,
            selected_plant_objs=Plant.objects.filter(id__in=selected_plant_ids),
            selected_category_objs=UserCategory.objects.filter(id__in=selected_category_ids),

            filters=g,
            stats={
                'total': total,
                'passed': passed,
                'failed': total - passed,
                'rate': round(passed / total * 100) if total else 0,
            },
        )
        return ctx


class ScorecardDetailView(InductionLoginMixin, TemplateView):
    """View button - every attempt of this person with each question and the answer given."""
    template_name = 'induction/scorecard_detail.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        latest = get_object_or_404(
            InductionAttempt.objects.select_related('plant', 'user_category'),
            pk=self.kwargs['pk'], status='SUBMITTED')

        attempts = list(
            InductionAttempt.objects
            .filter(status='SUBMITTED', plant_id=latest.plant_id,
                    user_category_id=latest.user_category_id)
            .filter(_same_person_q(latest))
            .select_related('language')
            .order_by('-submitted_at'))

        answers = (InductionAttemptAnswer.objects
                   .filter(attempt__in=attempts)
                   .select_related('question', 'selected_option')
                   .prefetch_related('question__options')
                   .order_by('attempt_id', 'sequence_no'))

        by_attempt = {}
        for ans in answers:
            correct = next((o for o in ans.question.options.all() if o.is_correct), None)
            is_ok = bool(ans.selected_option_id and ans.selected_option.is_correct)
            by_attempt.setdefault(ans.attempt_id, []).append({
                'no':       ans.sequence_no,
                'question': ans.question.question_text,
                'selected': ans.selected_option.option_text if ans.selected_option_id else '',
                'correct':  correct.option_text if correct else '',
                'is_ok':    is_ok,
                'marks':    ans.max_marks if is_ok else 0,
                'max':      ans.max_marks,
            })

        ctx.update(
            person=latest,
            attempts=[{
                'attempt': a,
                'answers': by_attempt.get(a.id, []),
                'right': sum(1 for x in by_attempt.get(a.id, []) if x['is_ok']),
            } for a in attempts],
        )
        return ctx


class ScorecardExportView(InductionLoginMixin, ScorecardRowsMixin, View):
    http_method_names = ['get']

    def get(self, request):
        wb = Workbook()
        ws = wb.active
        ws.title = 'Participants'

        headers = ['Participant', 'Mobile', 'Employee ID', 'Contractor ID',
                   'Company', 'Category', 'Plant', 'Score %', 'Result',
                   'Attempt', 'Submitted']
        ws.append(headers)
        for col in range(1, len(headers) + 1):
            c = ws.cell(row=1, column=col)
            c.font = Font(bold=True, color='FFFFFF')
            c.fill = PatternFill('solid', fgColor='0F7F6F')
            c.alignment = Alignment(horizontal='center')

        for r in self.get_rows():
            last = r['submitted_at']
            ws.append([
                r['name'], r['mobile'], r['employee_id'], r['contractor_id'],
                r['company'], r['category'], r['plant'],
                f"{r['score']}%" if r['score'] is not None else '',
                'Passed' if r['result'] == 'PASS' else 'Failed',
                r['attempt_no'],
                timezone.localtime(last).strftime('%d %b %Y %H:%M') if last else '',
            ])

        for i, width in enumerate([22, 15, 15, 15, 22, 16, 22, 10, 10, 10, 20], start=1):
            ws.column_dimensions[get_column_letter(i)].width = width

        response = HttpResponse(
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = 'attachment; filename="scorecard.xlsx"'
        wb.save(response)
        return response


# =====================================================================
# 6. PUBLIC QR PORTAL  (no login - opened after scanning the QR)
#    flow: portal -> content(lang) -> start -> submit
# =====================================================================
def _get_language(code):
    langs = Language.objects.filter(is_active=True)
    return ((langs.filter(code=code).first() if code else None)
            or langs.filter(code__istartswith='en').first()
            or langs.first())


def _pick_video(language):
    """
    Chosen language first, then English fallback.
    Returns None if neither exists - the portal already handles that.
    Deliberately NOT an arbitrary video (would show the wrong language).
    """
    base = InductionVideo.objects.select_related('language')
    return (base.filter(language=language).first()
            or base.filter(language__code='en').first())


def _translate(obj, language, field):
    t = next((x for x in obj.translations.all() if x.language_id == language.id), None)
    return getattr(t, field) if t else getattr(obj, field)


def _categories(language):
    """Categories with translated names + the detail fields each one must fill"""
    out = []
    for c in UserCategory.objects.filter(is_active=True).prefetch_related('translations'):
        slug = c.slug or slugify(c.name)
        out.append({
            'id': c.id,
            'slug': slug,
            'name': _translate(c, language, 'name'),
            'fields': [{'name': n, 'label': label, 'required': required}
                       for n, label, required in PARTICIPANT_FIELDS.get(slug, [])],
        })
    return out


class PortalTokenMixin:
    """Resolves the QR token in the URL to its plant"""

    def get_qr(self):
        return get_object_or_404(PlantQRCode.objects.select_related('plant'),
                                 token=self.kwargs['token'], is_active=True)


@method_decorator(ensure_csrf_cookie, name='dispatch')
class PortalView(PortalTokenMixin, TemplateView):
    template_name = 'induction/portal.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        qr = self.get_qr()

        language = _get_language(self.request.GET.get('lang'))

        ctx.update({
            'plant': qr.plant,
            'token': self.kwargs['token'],
            'language': language,                       # the single selected language
            'categories': _categories(language),
            'languages': portal_languages(),            # the dropdown list
            'ready': _plant_questionnaire(qr.plant) is not None,

            # Google Translate configuration
            'google_translate_enabled': True,
        })

        return ctx


class PortalContentView(PortalTokenMixin, View):
    """Everything for the selected language: category names, video, questions."""
    http_method_names = ['get']

    def get(self, request, token):
        plant = self.get_qr().plant
        language = _get_language(request.GET.get('lang'))
        questionnaire = _plant_questionnaire(plant)

        # Video language = the one picked in the video dropdown (?video_lang=hi),
        # otherwise the page language. _pick_video() already falls back to English.
        video_lang_code = request.GET.get('video_lang')
        video_language = _get_language(video_lang_code) if video_lang_code else language
        video = _pick_video(video_language)

        # ONLY languages that really have a video stored (this feeds the dropdown)
        video_languages = [
            {'code': r['language__code'], 'name': r['language__name']}
            for r in (InductionVideo.objects
                      .filter(language__is_active=True)
                      .values('language__code', 'language__name')
                      .distinct()
                      .order_by('language__name'))
        ]

        questions = []
        if questionnaire:
            items = (questionnaire.items.select_related('question')
                     .prefetch_related('question__options'))
            for item in items:
                q = item.question
                options = [{'id': o.id, 'text': o.option_text} for o in q.options.all()]
                questions.append({
                    'id': q.id,
                    'text': q.question_text,
                    'marks': str(item.marks),
                    'options': options,
                })

        categories = []
        for c in UserCategory.objects.filter(is_active=True):
            categories.append({
                'id': c.id,
                'slug': c.slug,
                'name': c.name,
                'fields': [{'name': n, 'label': label, 'required': required}
                           for n, label, required in PARTICIPANT_FIELDS.get(c.slug, [])],
            })

        return JsonResponse({
            'plant': plant.name,
            'language': language.code,
            'categories': categories,
            'departments': _departments(plant),
            'video': {
                'title': video.title, 'url': video.file.url,
                'duration': video.duration_seconds,
                'language': video.language.code,
            } if video else None,
            'video_languages': video_languages,
            'questions': questions,
            'passing_percentage': str(questionnaire.passing_percentage) if questionnaire else None,
        })


class PortalStartView(PortalTokenMixin, View):
    """User chose category + language and filled the details of that category."""
    http_method_names = ['post']

    def post(self, request, token):
        qr = self.get_qr()
        questionnaire = _plant_questionnaire(qr.plant)
        if not questionnaire:
            return JsonResponse(
                {'ok': False, 'error': 'No questionnaire is assigned to this plant.'}, status=400)

        form = InductionStartForm(_payload(request))
        if not form.is_valid():
            return JsonResponse({'ok': False, 'errors': form.errors.get_json_data()}, status=400)

        d = form.cleaned_data
        attempt = InductionAttempt.objects.create(
            plant=qr.plant, qr_code=qr, questionnaire=questionnaire,
            user_category=d['user_category'], language=d['language'],
            video=_pick_video(d['language']),
            **form.participant_data(),
        )
        return JsonResponse({'ok': True, 'attempt_uuid': str(attempt.attempt_uuid),
                             'attempt_no': attempt.attempt_no})


class PortalSubmitView(PortalTokenMixin, View):
    """
    Submit assessment and calculate the score on the server.
    Handles both first submission and reattempts.
    """
    http_method_names = ['post']

    def post(self, request, token):
        data = _payload(request)

        attempt_uuid = data.get('attempt_uuid')

        # Validate attempt UUID
        if not attempt_uuid:
            return JsonResponse({
                'ok': False,
                'error': 'Attempt ID is missing. Please restart the assessment.'
            }, status=400)

        # Check whether the attempt exists for this QR token
        attempt = InductionAttempt.objects.filter(
            attempt_uuid=attempt_uuid,
            qr_code__token=token
        ).select_related('questionnaire').first()

        if not attempt:
            return JsonResponse({
                'ok': False,
                'error': 'Assessment session not found. Please restart the induction.'
            }, status=404)

        # Only allow submission if the attempt is still in progress
        if attempt.status != 'IN_PROGRESS':
            return JsonResponse({
                'ok': False,
                'error': 'This assessment has already been submitted.',
                'already_submitted': True
            }, status=400)

        with transaction.atomic():
            answers = data.get('answers') or {}

            # Remove any previous answers for this attempt (safety for retries)
            InductionAttemptAnswer.objects.filter(attempt=attempt).delete()

            for item in attempt.questionnaire.items.select_related('question'):
                option = None
                selected = answers.get(str(item.question_id))

                if selected:
                    option = InductionQuestionOption.objects.filter(
                        pk=selected,
                        question_id=item.question_id
                    ).first()

                InductionAttemptAnswer.objects.create(
                    attempt=attempt,
                    question=item.question,
                    sequence_no=item.sequence_no,
                    selected_option=option,
                    max_marks=item.marks
                )

            attempt.video_completed = bool(data.get('video_completed'))
            attempt.finalize()

        return JsonResponse({
            'ok': True,
            'obtained_marks': str(attempt.obtained_marks),
            'total_marks': str(attempt.total_marks),
            'percentage': str(attempt.score_percentage),
            'passing_percentage': str(attempt.passing_percentage),
            'result': attempt.result,
            'attempt_no': attempt.attempt_no,
        })


@require_GET
def portal_employee_lookup(request, token):
    """
    Look up a registered employee by employee_id and return their details
    for auto-fill on the induction portal.

    Auth: same QR token that unlocks the portal.
    Returns name, email, mobile, department.
    """
    # Validate the QR token (same pattern as PortalTokenMixin)
    qr = get_object_or_404(
        PlantQRCode.objects.select_related('plant'),
        token=token,
        is_active=True,
    )

    emp_id = (request.GET.get('employee_id') or '').strip()
    if not emp_id:
        return JsonResponse({'ok': False, 'error': 'employee_id is required'}, status=400)

    User = get_user_model()

    base = User.objects.select_related('department').filter(
        employee_id__iexact=emp_id,
        is_active_employee=True,
    )

    # Prefer users tied to this QR's plant (primary or assigned);
    # fall back to a global match if none found for the plant.
    user = (
        base.filter(Q(plant=qr.plant) | Q(assigned_plants=qr.plant)).distinct().first()
        or base.first()
    )

    if not user:
        return JsonResponse({'ok': True, 'found': False})

    return JsonResponse({
        'ok': True,
        'found': True,
        'data': {
            'participant_name':   user.get_full_name(),
            'participant_email':  user.email or '',
            'participant_mobile': user.phone or '',
            'department':         user.department.name if user.department else '',
            'employee_id':        user.employee_id,
        },
    })