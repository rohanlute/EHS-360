# apps/induction/management/commands/seed_categories.py
from django.core.management.base import BaseCommand
from django.db import transaction
from apps.induction.models import UserCategory, Language

class Command(BaseCommand):
    help = 'Seed the 3 fixed UserCategory rows and default Language (idempotent).'

    @transaction.atomic
    def handle(self, *args, **options):
        
        # 1. Define the categories exactly as you want them in the DB
        categories_data = [
            {'slug': 'employee',   'name': 'On-roll Employee'},
            {'slug': 'contractor', 'name': 'Contractor / Off-roll'},
            {'slug': 'visitor',    'name': 'Visitor'},
        ]

        for data in categories_data:
            obj, created = UserCategory.objects.update_or_create(
                slug=data['slug'],
                defaults={
                    'name': data['name'],
                    'is_active': True
                }
            )
            
            if created:
                self.stdout.write(self.style.SUCCESS(f'Created category: {obj.name} ({obj.slug})'))
            else:
                self.stdout.write(f'Updated/Verified category: {obj.name} ({obj.slug})')

        # 2. Ensure at least one Language exists so the portal doesn't crash
        lang, lang_created = Language.objects.get_or_create(
            code='en',
            defaults={'name': 'English', 'is_active': True}
        )
        
        if lang_created:
            self.stdout.write(self.style.SUCCESS(f'Created language: {lang.name} ({lang.code})'))
        else:
            self.stdout.write(f'Language already exists: {lang.name} ({lang.code})')

        self.stdout.write(self.style.SUCCESS('\nDatabase seeded successfully!'))