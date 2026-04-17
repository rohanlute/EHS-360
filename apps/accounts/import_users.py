import pandas as pd
from django.contrib.auth import get_user_model
from apps.organizations.models import Plant
from apps.accounts.models import Role

User = get_user_model()


def import_users_from_excel(file_path):
    df = pd.read_excel(file_path)

    # ✅ Get HO plant (ID first, fallback by name)
    plant = Plant.objects.filter(id=12).first()
    if not plant:
        plant = Plant.objects.filter(name__iexact="Head Office").first()

    if not plant:
        print("❌ HO Plant not found")
        return

    # ✅ Get Role (ID = 3)
    role = Role.objects.filter(id=3).first()
    if not role:
        print("❌ Role with ID 3 not found")
        return

    success_count = 0
    failed_count = 0

    for index, row in df.iterrows():
        try:
            # ✅ Excel column mapping
            full_name = str(row.get('Full Name', '')).strip()
            email = str(row.get('Official Email Id', '')).strip()
            employee_id = str(row.get('Employee Id', '')).strip()
            phone = str(row.get('Personal Mobile Number', '')).strip()

            # ✅ Email validation
            if not email or email.lower() in ['nan', 'none']:
                print(f"❌ Skipped (invalid email) row {index}")
                continue

            # ✅ Skip duplicates
            if User.objects.filter(email=email).exists():
                print(f"⚠️ Skipped (exists): {email}")
                continue

            # ✅ Split full name
            name_parts = full_name.split()
            first_name = name_parts[0] if name_parts else ''
            last_name = ' '.join(name_parts[1:]) if len(name_parts) > 1 else ''

            # ✅ Create user
            user = User.objects.create_user(
                username=email,
                email=email,
                first_name=first_name,
                last_name=last_name,
                phone=phone,
                employee_id=employee_id,
                password="Default@123"
            )

            # ✅ Set additional fields
            user.job_title = ''
            user.employment_type = 'FULL_TIME'
            user.department = None
            user.plant = plant
            user.role = role

            user.save()

            # ✅ Assign plant (M2M)
            user.assigned_plants.set([plant.id])

            # ✅ Sync permissions from role
            user.sync_permissions_to_flags()

            success_count += 1
            print(f"✅ Created: {email}")

        except Exception as e:
            failed_count += 1
            print(f"❌ Error on row {index}: {e}")

    print("\n=== SUMMARY ===")
    print(f"Created: {success_count}")
    print(f"Failed: {failed_count}")