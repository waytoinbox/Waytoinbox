import Email_validate_app.models
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0088_campaign_sent_via'),
    ]

    operations = [
        migrations.CreateModel(
            name='TemplateImage',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('file', models.ImageField(upload_to=Email_validate_app.models._template_image_path)),
                ('original_name', models.CharField(max_length=255)),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='Email_validate_app.usertable')),
            ],
            options={
                'db_table': 'template_images',
            },
        ),
    ]
