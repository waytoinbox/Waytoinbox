"""
Phase 1 (content-scoring implementation plan) — regression tests for the
EXISTING score_email() behavior and its POST endpoint, written before any
behavior change so later phases can prove they didn't break what's here.

Covers services/so_content_score.py::score_email() rule-by-rule, plus the
so_content_score view (Sales-Outreach/sender/content-score/).
"""
import json

from django.test import TestCase, Client

from Email_validate_app.models import UserTable
from Email_validate_app.services.so_content_score import score_email

ACTION_URL = '/Sales-Outreach/sender/content-score/'


def make_user(email):
    return UserTable.objects.create_user(
        user_name='Score Test', user_email=email, password='StrongPass123!')


# A subject/body pair with nothing that should trip any current rule:
# 44-char normal-case subject, no '!', no re/fwd prefix; a >20/<400-word
# body containing a merge tag and the word "unsubscribe", no spam phrases,
# no links, no images.
CLEAN_SUBJECT = 'Quick question about your onboarding process'
CLEAN_BODY = (
    '<p>Hi {{first_name}}, I wanted to reach out because I noticed your team has '
    'been growing quickly and I thought this might be a good time to connect about '
    'how we could help streamline your onboarding process for new hires.</p>'
    '<p>Let me know if you would be open to a quick call this week.</p>'
    '<p>If you would like to stop receiving these emails, you can unsubscribe at '
    'any time.</p>'
)


def _reason_texts(result):
    return [r['text'] for r in result['reasons']]


class ScoreEmailSubjectRuleTests(TestCase):
    """Rules 1-6: subject-only checks."""

    def test_empty_subject(self):
        r = score_email('', CLEAN_BODY)
        self.assertIn('Subject line is empty.', _reason_texts(r))
        warn = next(x for x in r['reasons'] if x['text'] == 'Subject line is empty.')
        self.assertEqual(warn['severity'], 'warn')

    def test_subject_too_long(self):
        long_subject = 'word ' * 20  # 100 chars, all lowercase, no '!' — isolates length only
        r = score_email(long_subject, CLEAN_BODY)
        self.assertTrue(any('aim for under 70' in t for t in _reason_texts(r)))

    def test_subject_too_short(self):
        r = score_email('Hi', CLEAN_BODY)
        self.assertTrue(any('short subjects can look terse' in t for t in _reason_texts(r)))

    def test_subject_mostly_uppercase(self):
        r = score_email('BUY OUR PRODUCT TODAY', CLEAN_BODY)
        self.assertIn('Subject is mostly capital letters, a common spam signal.', _reason_texts(r))

    def test_subject_multiple_exclamation_marks(self):
        r = score_email('Great news for you!!', CLEAN_BODY)
        self.assertIn('Subject has multiple exclamation marks.', _reason_texts(r))

    def test_subject_fake_reply_prefix(self):
        r = score_email('Re: your inquiry about pricing', CLEAN_BODY)
        self.assertTrue(any('fakes a reply/forward prefix' in t for t in _reason_texts(r)))

    def test_subject_fake_forward_prefix(self):
        r = score_email('Fwd: your inquiry about pricing', CLEAN_BODY)
        self.assertTrue(any('fakes a reply/forward prefix' in t for t in _reason_texts(r)))


class ScoreEmailBodyRuleTests(TestCase):
    """Rules 7-11: body presence/length + exclamation-heavy body."""

    def test_empty_body(self):
        r = score_email(CLEAN_SUBJECT, '')
        self.assertIn('Email body is empty.', _reason_texts(r))

    def test_short_body(self):
        html = '<p>Hi {{first_name}}, just checking in quickly today.</p>'
        r = score_email(CLEAN_SUBJECT, html)
        self.assertTrue(any('very short emails can read as low-effort' in t for t in _reason_texts(r)))

    def test_long_body(self):
        html = '<p>' + ('word ' * 401) + '</p>'
        r = score_email(CLEAN_SUBJECT, html)
        self.assertTrue(any('long cold emails tend to get skimmed' in t for t in _reason_texts(r)))

    def test_heavy_exclamation_marks_in_body(self):
        html = '<p>Hi there! Great news! Thanks! Bye!</p>'
        r = score_email(CLEAN_SUBJECT, html)
        self.assertIn('Heavy use of exclamation marks throughout the email.', _reason_texts(r))


class ScoreEmailSpamVocabularyTests(TestCase):
    """Rule 10: spam trigger phrase detection."""

    def test_spam_trigger_phrases_detected(self):
        html = '<p>Act now and get a free trial before it expires.</p>'
        r = score_email(CLEAN_SUBJECT, html)
        hit = next((x for x in r['reasons'] if x['text'].startswith('Contains spam trigger wording:')), None)
        self.assertIsNotNone(hit)
        self.assertEqual(hit['severity'], 'warn')
        self.assertIn('act now', hit['text'])
        self.assertIn('free trial', hit['text'])

    def test_no_spam_phrases_in_clean_email(self):
        r = score_email(CLEAN_SUBJECT, CLEAN_BODY)
        self.assertFalse(any(t.startswith('Contains spam trigger wording:') for t in _reason_texts(r)))


class ScoreEmailLinksAndImagesTests(TestCase):
    """Rules 12-15: link count and image count/ratio."""

    def test_too_many_links(self):
        html = '<p>' + ''.join(f'<a href="https://example.com/{i}">link</a> ' for i in range(9)) + '</p>'
        r = score_email(CLEAN_SUBJECT, html)
        self.assertTrue(any('cold outreach with many links is filtered' in t for t in _reason_texts(r)))

    def test_several_links(self):
        html = '<p>' + ''.join(f'<a href="https://example.com/{i}">link</a> ' for i in range(6)) + '</p>'
        r = score_email(CLEAN_SUBJECT, html)
        self.assertTrue(any('consider trimming to one clear call to action' in t for t in _reason_texts(r)))

    def test_image_heavy_little_text(self):
        html = '<img src="banner.jpg">'
        r = score_email(CLEAN_SUBJECT, html)
        self.assertIn('Mostly images with little text — a classic spam pattern.', _reason_texts(r))

    def test_many_images_with_sufficient_text(self):
        words = 'word ' * 45  # >=40 words so the image-heavy-little-text branch is NOT taken
        html = '<p>' + words + '</p>' + ('<img src="a.jpg">' * 5)
        r = score_email(CLEAN_SUBJECT, html)
        self.assertTrue(any('image-heavy mail often loads blocked by default' in t for t in _reason_texts(r)))


class ScoreEmailPersonalizationAndComplianceTests(TestCase):
    """Rules 16-17: merge-tag presence and unsubscribe-link presence."""

    def test_no_personalization_tag(self):
        html = '<p>Hi there, just checking in — hope things are well. You can unsubscribe anytime.</p>'
        r = score_email(CLEAN_SUBJECT, html)
        self.assertTrue(any('No personalization tag used' in t for t in _reason_texts(r)))

    def test_personalization_tag_present_no_such_reason(self):
        r = score_email(CLEAN_SUBJECT, CLEAN_BODY)
        self.assertFalse(any('No personalization tag used' in t for t in _reason_texts(r)))

    def test_known_personalization_tags_are_not_flagged_as_unknown(self):
        """Phase 3: every tag so_smtp.py's substitute_tags() actually
        substitutes at send time must pass silently."""
        html = (
            '<p>Hi {{first_name}} {{last_name}} ({{full_name}}) at {{company}}, '
            'reach me at {{phone}} or {{email}}. You can unsubscribe at '
            '{{unsubscribe_url}}.</p>'
        )
        r = score_email(CLEAN_SUBJECT, html)
        self.assertFalse(any('Unknown personalization tag' in t for t in _reason_texts(r)))

    def test_unknown_personalization_tag_is_flagged(self):
        """Phase 3: a typo'd/unknown tag would reach the recipient
        unsubstituted — must be flagged distinctly from 'no tag used'."""
        html = '<p>Hi {{frist_name}}, hope you are well. You can unsubscribe anytime.</p>'
        r = score_email(CLEAN_SUBJECT, html)
        hit = next((x for x in r['reasons'] if x['text'].startswith('Unknown personalization tag(s):')), None)
        self.assertIsNotNone(hit)
        self.assertEqual(hit['severity'], 'warn')
        self.assertIn('{{frist_name}}', hit['text'])
        # Must not ALSO claim "no personalization tag used" -- one was used, it's just wrong.
        self.assertFalse(any('No personalization tag used' in t for t in _reason_texts(r)))

    def test_mixed_valid_and_unknown_tags_only_flags_the_unknown_one(self):
        html = '<p>Hi {{first_name}}, from {{unknown_field}}. You can unsubscribe anytime.</p>'
        r = score_email(CLEAN_SUBJECT, html)
        hit = next((x for x in r['reasons'] if x['text'].startswith('Unknown personalization tag(s):')), None)
        self.assertIsNotNone(hit)
        self.assertIn('{{unknown_field}}', hit['text'])
        self.assertNotIn('{{first_name}}', hit['text'])

    def test_no_unsubscribe_link(self):
        html = '<p>Hi {{first_name}}, just checking in — hope things are well today for you.</p>'
        r = score_email(CLEAN_SUBJECT, html)
        hit = next((x for x in r['reasons'] if 'No unsubscribe link' in x['text']), None)
        self.assertIsNotNone(hit)
        self.assertEqual(hit['severity'], 'warn')

    def test_unsubscribe_link_present_no_such_reason(self):
        r = score_email(CLEAN_SUBJECT, CLEAN_BODY)
        self.assertFalse(any('No unsubscribe link' in t for t in _reason_texts(r)))


class ScoreEmailCleanAndSchemaTests(TestCase):
    """Rule 18 (clean email) + rules 19-22 (response schema)."""

    def test_clean_email_no_issues(self):
        r = score_email(CLEAN_SUBJECT, CLEAN_BODY)
        self.assertEqual(r['score'], 0)
        self.assertEqual(r['label'], 'Good')
        self.assertEqual(len(r['reasons']), 1)
        self.assertEqual(r['reasons'][0]['text'], 'No issues detected. This email looks good to send.')
        self.assertEqual(r['reasons'][0]['severity'], 'info')

    def test_score_is_numeric(self):
        r = score_email(CLEAN_SUBJECT, CLEAN_BODY)
        self.assertIsInstance(r['score'], int)

    def test_label_is_one_of_good_fair_poor(self):
        for subject, html in [
            (CLEAN_SUBJECT, CLEAN_BODY),
            ('', ''),
            ('Re: BUY NOW!!', '<img src="a.jpg">'),
        ]:
            r = score_email(subject, html)
            self.assertIn(r['label'], ('Good', 'Fair', 'Poor'))

    def test_reasons_structure(self):
        r = score_email('', '')
        self.assertIsInstance(r['reasons'], list)
        self.assertGreater(len(r['reasons']), 0)
        for reason in r['reasons']:
            self.assertIn('severity', reason)
            self.assertIn('text', reason)
            self.assertIn(reason['severity'], ('warn', 'info'))
            self.assertIsInstance(reason['text'], str)

    def test_stats_structure(self):
        r = score_email(CLEAN_SUBJECT, CLEAN_BODY)
        stats = r['stats']
        for key in ('subject_length', 'word_count', 'link_count', 'image_count'):
            self.assertIn(key, stats)
            self.assertIsInstance(stats[key], int)

    def test_good_fair_poor_thresholds(self):
        # Good: <=3, Fair: <=7, Poor: >7 -- exercised via the label already
        # returned for known-score inputs above; this test locks the exact
        # boundary values themselves so a future change to the thresholds
        # must touch this test deliberately.
        from Email_validate_app.services.so_content_score import _GOOD_MAX, _FAIR_MAX
        self.assertEqual(_GOOD_MAX, 3)
        self.assertEqual(_FAIR_MAX, 7)


class SoContentScoreEndpointTests(TestCase):
    """POST Sales-Outreach/sender/content-score/"""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')
        self.user = make_user('score_endpoint@example.com')

    def _login(self):
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def test_unauthenticated_request_returns_403(self):
        r = self.client.post(ACTION_URL, data=json.dumps({'subject': 'x', 'html_body': 'y'}),
                              content_type='application/json')
        self.assertEqual(r.status_code, 403)

    def test_get_request_returns_405(self):
        self._login()
        r = self.client.get(ACTION_URL)
        self.assertEqual(r.status_code, 405)

    def test_malformed_json_returns_400(self):
        self._login()
        r = self.client.post(ACTION_URL, data='not json', content_type='application/json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['status'], 'error')

    def test_valid_json_returns_200_with_full_shape(self):
        self._login()
        r = self.client.post(
            ACTION_URL,
            data=json.dumps({'subject': CLEAN_SUBJECT, 'html_body': CLEAN_BODY}),
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 200)
        d = r.json()
        for key in ('status', 'score', 'label', 'reasons', 'stats'):
            self.assertIn(key, d)
        self.assertEqual(d['status'], 'ok')
        self.assertEqual(d['label'], 'Good')

    def test_missing_subject_and_body_does_not_crash(self):
        self._login()
        r = self.client.post(ACTION_URL, data=json.dumps({}), content_type='application/json')
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d['status'], 'ok')
        # empty subject (+3) + empty body (+3) + no personalization tag (+1)
        # + no unsubscribe link (+2) = 9, which is > _FAIR_MAX (7) => Poor.
        self.assertEqual(d['label'], 'Poor')


# ── Phase 4: new deterministic checks ───────────────────────────────────────

class ScoreEmailSubjectPhase4Tests(TestCase):
    def test_excessive_punctuation_in_subject(self):
        r = score_email('Are you interested??? Let us know', CLEAN_BODY)
        self.assertTrue(any('repeated punctuation' in t for t in _reason_texts(r)))

    def test_excessive_emoji_in_subject(self):
        r = score_email('Big news \U0001F600\U0001F389\U0001F680 for you', CLEAN_BODY)
        self.assertTrue(any('emoji' in t for t in _reason_texts(r)))

    def test_repeated_word_in_subject(self):
        r = score_email('Let us us know your thoughts today', CLEAN_BODY)
        self.assertTrue(any('repeats the same word' in t for t in _reason_texts(r)))

    def test_clean_subject_has_none_of_the_new_flags(self):
        r = score_email(CLEAN_SUBJECT, CLEAN_BODY)
        texts = _reason_texts(r)
        self.assertFalse(any('repeated punctuation' in t for t in texts))
        self.assertFalse(any('emoji' in t for t in texts))
        self.assertFalse(any('repeats the same word' in t for t in texts))


class ScoreEmailContentPhase4Tests(TestCase):
    def test_allcaps_words_in_body(self):
        html = '<p>THIS IS AN IMPORTANT UPDATE ABOUT YOUR ACCOUNT STATUS TODAY PLEASE READ CAREFULLY NOW OK.</p>'
        r = score_email(CLEAN_SUBJECT, html)
        self.assertTrue(any('ALL-CAPS words' in t for t in _reason_texts(r)))

    def test_excessive_bold_tags(self):
        html = '<p>' + ''.join(f'<strong>word{i}</strong> ' for i in range(12)) + '</p>'
        r = score_email(CLEAN_SUBJECT, html)
        self.assertTrue(any('bold/strong tags' in t for t in _reason_texts(r)))

    def test_long_average_sentence_length(self):
        html = '<p>' + ('word ' * 60) + '.</p>'  # one 60-word sentence
        r = score_email(CLEAN_SUBJECT, html)
        self.assertTrue(any('Average sentence length' in t for t in _reason_texts(r)))

    def test_no_cta_detected(self):
        html = '<p>Hi {{first_name}}, hope things are going well this month. You can unsubscribe anytime.</p>'
        r = score_email(CLEAN_SUBJECT, html)
        self.assertTrue(any('No clear call to action' in t for t in _reason_texts(r)))

    def test_cta_repeated_too_often(self):
        html = (
            '<p>Let me know if you are interested. Let me know if this week works. '
            'Let me know if you have questions. You can unsubscribe anytime.</p>'
        )
        r = score_email(CLEAN_SUBJECT, html)
        self.assertTrue(any('repeated' in t and 'call to action' in t for t in _reason_texts(r)))

    def test_clean_body_has_none_of_the_new_content_flags(self):
        r = score_email(CLEAN_SUBJECT, CLEAN_BODY)
        texts = _reason_texts(r)
        self.assertFalse(any('ALL-CAPS words' in t for t in texts))
        self.assertFalse(any('bold/strong tags' in t for t in texts))
        self.assertFalse(any('Average sentence length' in t for t in texts))
        self.assertFalse(any('No clear call to action' in t for t in texts))


class ScoreEmailLinksPhase4Tests(TestCase):
    def test_many_unique_link_domains(self):
        html = '<p>' + ''.join(
            f'<a href="https://site{i}.example.com/page">link</a> ' for i in range(5)
        ) + '</p>'
        r = score_email(CLEAN_SUBJECT, html)
        self.assertTrue(any('different domains' in t for t in _reason_texts(r)))

    def test_duplicate_link_detected(self):
        html = '<p><a href="https://example.com/offer">a</a> <a href="https://example.com/offer">b</a></p>'
        r = score_email(CLEAN_SUBJECT, html)
        self.assertTrue(any('same link is repeated' in t for t in _reason_texts(r)))

    def test_http_link_flagged(self):
        html = '<p><a href="http://example.com/offer">click</a></p>'
        r = score_email(CLEAN_SUBJECT, html)
        hit = next((x for x in r['reasons'] if 'insecure http://' in x['text']), None)
        self.assertIsNotNone(hit)
        self.assertEqual(hit['severity'], 'warn')

    def test_malformed_link_flagged(self):
        html = '<p><a href="http://">click here</a></p>'
        r = score_email(CLEAN_SUBJECT, html)
        self.assertTrue(any('look malformed' in t for t in _reason_texts(r)))

    def test_clean_body_has_no_link_flags(self):
        r = score_email(CLEAN_SUBJECT, CLEAN_BODY)  # CLEAN_BODY has zero links
        texts = _reason_texts(r)
        self.assertFalse(any('different domains' in t for t in texts))
        self.assertFalse(any('same link is repeated' in t for t in texts))
        self.assertFalse(any('insecure http://' in t for t in texts))
        self.assertFalse(any('look malformed' in t for t in texts))


class ScoreEmailHtmlPhase4Tests(TestCase):
    def test_image_missing_alt_text(self):
        html = '<p>' + ('word ' * 45) + '</p><img src="banner.jpg">'
        r = score_email(CLEAN_SUBJECT, html)
        self.assertTrue(any('missing alt text' in t for t in _reason_texts(r)))

    def test_image_with_alt_text_not_flagged(self):
        html = '<p>' + ('word ' * 45) + '</p><img src="banner.jpg" alt="Product banner">'
        r = score_email(CLEAN_SUBJECT, html)
        self.assertFalse(any('missing alt text' in t for t in _reason_texts(r)))

    def test_full_document_tags_flagged(self):
        html = '<html><body><p>' + ('word ' * 25) + '</p></body></html>'
        r = score_email(CLEAN_SUBJECT, html)
        self.assertTrue(any('meant to be a fragment' in t for t in _reason_texts(r)))

    def test_clean_body_has_no_html_structure_flags(self):
        r = score_email(CLEAN_SUBJECT, CLEAN_BODY)
        texts = _reason_texts(r)
        self.assertFalse(any('missing alt text' in t for t in texts))
        self.assertFalse(any('meant to be a fragment' in t for t in texts))


class ScoreEmailPreheaderTests(TestCase):
    """Phase 4: preheader is optional (default ''); only scored if passed."""

    def test_no_preheader_arg_is_backward_compatible(self):
        r = score_email(CLEAN_SUBJECT, CLEAN_BODY)  # no preheader kwarg at all
        self.assertEqual(r['label'], 'Good')

    def test_long_preheader_flagged(self):
        r = score_email(CLEAN_SUBJECT, CLEAN_BODY, preheader='word ' * 40)
        self.assertTrue(any('Preheader is' in t for t in _reason_texts(r)))

    def test_short_preheader_not_flagged(self):
        r = score_email(CLEAN_SUBJECT, CLEAN_BODY, preheader='A quick note before you open this.')
        self.assertFalse(any('Preheader is' in t for t in _reason_texts(r)))


class ScoreEmailCategoriesTests(TestCase):
    """Phase 5: category grouping, additive response fields."""

    def test_categories_key_present_with_fixed_set(self):
        from Email_validate_app.services.so_content_score import CATEGORIES
        r = score_email(CLEAN_SUBJECT, CLEAN_BODY)
        self.assertIn('categories', r)
        self.assertEqual(set(r['categories'].keys()), set(CATEGORIES))

    def test_clean_email_categories_are_all_empty(self):
        r = score_email(CLEAN_SUBJECT, CLEAN_BODY)
        for cat, findings in r['categories'].items():
            self.assertEqual(findings, [], f'expected {cat} to have no findings for a clean email')

    def test_dirty_email_findings_land_in_expected_categories(self):
        r = score_email('', '')  # empty subject + empty body + no tag + no unsubscribe
        self.assertTrue(any('Subject line is empty.' == f['text'] for f in r['categories']['Subject']))
        self.assertTrue(any('Email body is empty.' == f['text'] for f in r['categories']['Content']))
        self.assertTrue(any('No personalization tag used' in f['text'] for f in r['categories']['Personalization']))
        self.assertTrue(any('No unsubscribe link' in f['text'] for f in r['categories']['Compliance']))

    def test_flat_reasons_still_has_severity_and_text_backward_compat(self):
        """Old frontend code reading reasons[i].severity/.text must keep working."""
        r = score_email('', '')
        for reason in r['reasons']:
            self.assertIn('severity', reason)
            self.assertIn('text', reason)

    def test_flat_reasons_now_also_has_category_and_points(self):
        r = score_email('', '')
        for reason in r['reasons']:
            self.assertIn('category', reason)
            self.assertIn('points', reason)

    def test_clean_email_flat_reasons_fallback_unchanged(self):
        r = score_email(CLEAN_SUBJECT, CLEAN_BODY)
        self.assertEqual(len(r['reasons']), 1)
        self.assertEqual(r['reasons'][0]['text'], 'No issues detected. This email looks good to send.')

    def test_score_equals_sum_of_reason_points(self):
        r = score_email('', '')
        real_points = sum(f['points'] for cat in r['categories'].values() for f in cat)
        self.assertEqual(r['score'], real_points)


# ── Phase 8: rate limiting / input safety ───────────────────────────────────

class SoContentScoreSafetyTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.client = Client(SERVER_NAME='127.0.0.1')
        self.user = make_user('score_safety@example.com')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def tearDown(self):
        from django.core.cache import cache
        cache.clear()

    def test_non_string_field_rejected(self):
        r = self.client.post(ACTION_URL, data=json.dumps({'subject': 123, 'html_body': 'ok'}),
                              content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_oversized_subject_rejected(self):
        r = self.client.post(
            ACTION_URL,
            data=json.dumps({'subject': 'x' * 501, 'html_body': CLEAN_BODY}),
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 400)

    def test_oversized_body_rejected(self):
        r = self.client.post(
            ACTION_URL,
            data=json.dumps({'subject': CLEAN_SUBJECT, 'html_body': 'x' * 500_001}),
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 400)

    def test_within_limits_still_succeeds(self):
        r = self.client.post(
            ACTION_URL,
            data=json.dumps({'subject': CLEAN_SUBJECT, 'html_body': CLEAN_BODY}),
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 200)

    def test_rate_limit_blocks_after_threshold(self):
        """Reuses the existing views/auth.py cache-based rate-limit helpers
        (no new dependency) — patch the threshold down so the test doesn't
        need 60 real requests."""
        from unittest.mock import patch
        with patch('Email_validate_app.views.so_sender._SCORE_RATE_MAX', 3):
            payload = json.dumps({'subject': CLEAN_SUBJECT, 'html_body': CLEAN_BODY})
            statuses = []
            for _ in range(4):
                r = self.client.post(ACTION_URL, data=payload, content_type='application/json')
                statuses.append(r.status_code)
            self.assertEqual(statuses[:3], [200, 200, 200])
            self.assertEqual(statuses[3], 429)
