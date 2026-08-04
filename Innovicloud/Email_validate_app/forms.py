from django import forms
from django.core.exceptions import ValidationError
from Email_validate_app.models import UserTable

class CustomSignupForm(forms.ModelForm):
    confirm_password = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'inputsi password-field', 'id': 'confirm-password'}),
        required=True
    )

    class Meta:
        model = UserTable
        fields = ['user_name', 'user_email', 'password']  # Use 'password' instead of 'user_password'

        widgets = {
            'user_name': forms.TextInput(attrs={'class': 'inputsi', 'id': 'username'}),
            'user_email': forms.EmailInput(attrs={'class': 'inputsi', 'id': 'email'}),
            'password': forms.PasswordInput(attrs={'class': 'inputsi password-field', 'id': 'password'}),
        }

    def validate_unique(self):
        exclude = self._get_validation_exclusions()
        exclude.add('user_name')
        exclude.add('user_email')
        try:
            self.instance.validate_unique(exclude=exclude)
        except ValidationError as e:
            self._update_errors(e)

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm_password = cleaned_data.get("confirm_password")

        if password and confirm_password and password != confirm_password:
            self.add_error('confirm_password', "Passwords do not match!")
            


     
            
