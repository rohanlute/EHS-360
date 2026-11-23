# apps/induction/languages.py
"""
ONE source of truth for the languages used by:
  - the public portal language / translation dropdown
  - the Upload Video language dropdown
  - the "missing video" counts on the dashboard and video page

Edit PORTAL_LANGUAGES to match your portal's translation dropdown.
Codes are the same codes Google Translate uses.
"""
from .models import Language

PORTAL_LANGUAGES = [
    ('as', 'Assamese'),
    ('bn', 'Bengali'),
    ('brx', 'Bodo'),
    ('doi', 'Dogri'),
    ('en', 'English'),
    ('gu', 'Gujarati'),
    ('hi', 'Hindi'),
    ('kn', 'Kannada'),
    ('ks', 'Kashmiri'),
    ('kok', 'Konkani'),
    ('mai', 'Maithili'),
    ('ml', 'Malayalam'),
    ('mni', 'Manipuri'),
    ('mr', 'Marathi'),
    ('ne', 'Nepali'),
    ('or', 'Odia'),
    ('pa', 'Punjabi'),
    ('sa', 'Sanskrit'),
    ('sat', 'Santhali'),
    ('sd', 'Sindhi'),
    ('ta', 'Tamil'),
    ('te', 'Telugu'),
    ('ur', 'Urdu'),
]


def portal_languages():
    """
    Active Language rows for every language in PORTAL_LANGUAGES.
    Missing rows are created automatically, so the dropdown always matches
    the portal. An existing row such as 'hi-IN' is reused instead of duplicated.
    """
    existing = list(Language.objects.all())

    def find(code):
        code = code.lower()
        for lang in existing:
            c = (lang.code or '').lower()
            if c == code or c.split('-')[0] == code:
                return lang
        return None

    for code, name in PORTAL_LANGUAGES:
        if not find(code):
            Language.objects.create(code=code, name=name, is_active=True)

    codes = [c for c, _ in PORTAL_LANGUAGES]
    wanted = {c.lower() for c in codes}
    ids = [l.pk for l in Language.objects.filter(is_active=True)
           if (l.code or '').lower().split('-')[0] in wanted]
    return Language.objects.filter(pk__in=ids).order_by('name')