from django.contrib.auth import authenticate
from django.test import TestCase

from Email_validate_app.models import UserTable


class AdminAuthFlagSyncTests(TestCase):
    def test_admin_user_is_flagged_as_staff_on_save(self):
        user = UserTable.objects.create_user(
            user_name='Admin User',
            user_email='admin@example.com',
            password='StrongPass123!'
        )

        user.is_admin = True
        user.save()

        self.assertTrue(user.is_staff)
        authenticated = authenticate(username='admin@example.com', password='StrongPass123!')
        self.assertIsNotNone(authenticated)
        self.assertTrue(authenticated.is_staff)
